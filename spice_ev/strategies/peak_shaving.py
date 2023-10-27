from copy import deepcopy
import datetime as dt

from spice_ev import util, events
from spice_ev.strategy import Strategy


class PeakShaving(Strategy):
    """
    Balance out grid connector power over time.
    """
    def __init__(self, components, start_time, **kwargs):
        self.HORIZON = 24  # look ahead for GC events in hours
        self.perfect_foresight = True  # perfect foresight for grid situation and vehicle events
        super().__init__(components, start_time, **kwargs)
        self.HORIZON = dt.timedelta(hours=self.HORIZON)
        self.description = "Peak Shaving"

        if self.perfect_foresight:
            all_events = self.events.vehicle_events + self.events.grid_operator_signals
            for name, load_list in self.events.fixed_load_lists.items():
                all_events.extend(load_list.get_events(name, events.FixedLoad))
            for name, local_generation in self.events.local_generation_lists.items():
                all_events.extend(local_generation.get_events(name, events.LocalEnergyGeneration))

            # make all events known at least HORIZON hours in advance
            changed = 0
            for event in all_events:
                old_signal_time = event.signal_time
                event.signal_time = min(event.signal_time, event.start_time - self.HORIZON)
                # make sure events don't signal before start
                event.signal_time = max(event.signal_time, start_time)
                changed += event.signal_time < old_signal_time
            if changed:
                print(changed, "events signaled earlier")
            self.events = sorted(all_events, key=lambda ev: ev.start_time)

    def step(self):
        """Calculates charging in each timestep.

        :return: current time and commands of the charging stations
        :rtype: dict
        """
        charging_stations = {}
        for gc_id, gc in self.world_state.grid_connectors.items():
            charging_stations.update(self.step_gc(gc_id, gc))
        return {'current_time': self.current_time, 'commands': charging_stations}

    def step_gc(self, gc_id, gc):
        # ---------- GET NEXT EVENTS ---------- #
        timesteps = []

        # look ahead (limited by horizon)
        # get future events and predict fixed load and cost for each timestep
        event_idx = 0
        timesteps_ahead = int(self.HORIZON / self.interval)

        sim_vehicles = deepcopy(self.world_state.vehicles)
        vehicles_present = {}
        vehicle_arrivals = []
        for vid, v in sim_vehicles.items():
            cs_id = v.connected_charging_station
            if cs_id is None:
                continue
            cs = self.world_state.charging_stations[cs_id]
            if cs.parent == gc_id:
                vehicles_present[vid] = len(vehicle_arrivals)
                depart_idx = None
                if v.estimated_time_of_departure is not None:
                    delta_t = v.estimated_time_of_departure - self.current_time
                    depart_idx = -(-delta_t // self.interval)
                vehicle_arrivals.append({
                    "vid": vid,
                    "vehicle": deepcopy(v),
                    "arrival_idx": 0,
                    "depart_idx": depart_idx  # might be overwritten by departure event
                })

        gc_info = {
            "max_power": gc.cur_max_power,
            "cur_power": 0,  # gc.get_current_load(),
            "vehicles": vehicles_present,
        }
        cur_loads = deepcopy(gc.current_loads)
        cur_time = self.current_time - self.interval
        # perfect foresight: remove past events from event list
        while self.perfect_foresight and len(self.events) > 0:
            if self.events[0].start_time <= self.current_time:
                self.events.pop(0)
            else:
                break

        for timestep_idx in range(timesteps_ahead):
            cur_time += self.interval

            # peek into future events
            while True:
                try:
                    if self.perfect_foresight:
                        event = self.events[event_idx]
                    else:
                        event = self.world_state.future_events[event_idx]
                except IndexError:
                    # no more events
                    break
                if event.start_time > cur_time:
                    # not this timestep
                    break
                event_idx += 1
                if type(event) is events.LocalEnergyGeneration:
                    if event.grid_connector_id != gc_id:
                        continue
                    cur_loads[event.name] = -event.value
                elif type(event) is events.FixedLoad:
                    if event.grid_connector_id != gc_id:
                        continue
                    cur_loads[event.name] = event.value
                elif type(event) is events.GridOperatorSignal:
                    if event.grid_connector_id != gc_id or event.max_power is None:
                        continue
                    gc_info["max_power"] = event.max_power
                elif type(event) is events.VehicleEvent:
                    if event.event_type == "departure":
                        if event.vehicle_id in gc_info["vehicles"]:
                            v_idx = gc_info["vehicles"].pop(event.vehicle_id)
                            vehicle_arrivals[v_idx]["depart_idx"] = timestep_idx
                        # perfect load (up to desired soc if below battery soc)
                        sim_vehicles[event.vehicle_id].battery.soc = max(
                            sim_vehicles[event.vehicle_id].battery.soc,
                            sim_vehicles[event.vehicle_id].desired_soc)
                    else:
                        # arrival
                        cs_id = event.update.get("connected_charging_station")
                        if cs_id is None:
                            continue
                        # update vehicle info
                        vid = event.vehicle_id
                        vehicle = sim_vehicles[vid]
                        vehicle.desired_soc = event.update["desired_soc"]
                        vehicle.battery.soc += event.update["soc_delta"]
                        vehicle.estimated_time_of_departure = event.update[
                            "estimated_time_of_departure"]
                        vehicle.connected_charging_station = cs_id
                        cs = self.world_state.charging_stations[cs_id]
                        if cs.parent == gc_id:
                            assert vid not in gc_info["vehicles"], (
                                f"{vid} already standing at {event.start_time} "
                                f"({gc_info['vehicles'][vid]} / {timestep_idx})")
                            gc_info["vehicles"][vid] = len(vehicle_arrivals)
                            depart_idx = None
                            if vehicle.estimated_time_of_departure is not None:
                                delta_t = vehicle.estimated_time_of_departure - self.current_time
                                depart_idx = -(-delta_t // self.interval)
                            vehicle_arrivals.append({
                                "vid": vid,
                                "vehicle": deepcopy(vehicle),
                                "arrival_idx": timestep_idx,
                                "depart_idx": depart_idx,  # might be changed by departure event
                            })

            gc_info["cur_power"] = sum(cur_loads.values())
            timesteps.append(deepcopy(gc_info))

        charging_stations = {}

        # no depart_idx: ignore vehicle
        vehicles = [v for v in vehicle_arrivals if v["depart_idx"] is not None]

        # order vehicles by standing time => charge those with least standing time first
        vehicles = sorted(vehicles,
                          key=lambda v: min(v["depart_idx"], timesteps_ahead) - v["arrival_idx"])

        # --- ADJUST POWER CURVE --- #
        for v_info in vehicles:
            # get arrival/departure
            arrival_idx = v_info["arrival_idx"]
            depart_idx = v_info["depart_idx"]
            if arrival_idx >= depart_idx:
                continue

            sim_vehicle = v_info["vehicle"]
            energy_needed = sim_vehicle.get_energy_needed()
            # scale energy needed with remaining standing time
            if depart_idx > timesteps_ahead:
                f = (timesteps_ahead - arrival_idx) / (depart_idx - arrival_idx)
                energy_needed *= f
                desired_soc = sim_vehicle.battery.soc + (
                    f*(sim_vehicle.desired_soc - sim_vehicle.battery.soc))
                sim_vehicle.desired_soc = desired_soc
                depart_idx = timesteps_ahead
                v_info["depart_idx"] = depart_idx
            v_info["energy_needed"] = energy_needed

            # apply charging strategy
            sim_vehicle.schedule = self.fast_charge(v_info, timesteps)

        # use surplus for all vehicles currently at charging station, apply power
        for v_info in vehicles:
            if v_info["arrival_idx"] > 0:
                continue
            sim_vehicle = v_info["vehicle"]
            sim_vehicle.schedule -= min(timesteps[0]["cur_power"], 0)
            if sim_vehicle.schedule > 0:
                cs_id = sim_vehicle.connected_charging_station
                avg_power = self.world_state.vehicles[v_info["vid"]].battery.load(
                    self.interval, target_power=sim_vehicle.schedule)["avg_power"]
                charging_stations[cs_id] = gc.add_load(cs_id, avg_power)

        # use batteries to balance power levels
        for b_id, battery in self.world_state.batteries.items():
            if battery.parent != gc_id:
                continue
            timesteps[0]["cur_power"] = gc.get_current_load()
            avg_power = sum([max(ts["cur_power"], 0) for ts in timesteps]) / timesteps_ahead
            avg_power = max(avg_power, 0)
            # try to reach avg_power
            delta_power = avg_power - gc.get_current_load()
            bat_power = 0
            if delta_power >= battery.min_charging_power:
                # below average: charge
                bat_power = battery.load(self.interval, target_power=delta_power)["avg_power"]
            elif delta_power <= -battery.min_charging_power:
                # above average: discharge
                bat_power = -battery.unload(self.interval, target_power=-delta_power)["avg_power"]
            gc.add_load(b_id, bat_power)
        return charging_stations

    def fast_charge(self, v_info, timesteps):
        sim_vehicle = v_info["vehicle"]
        arrival_idx = v_info["arrival_idx"]
        depart_idx = v_info["depart_idx"]
        energy_needed = v_info["energy_needed"]
        if energy_needed <= 0:
            return 0
        cs = self.world_state.charging_stations[sim_vehicle.connected_charging_station]

        # get power over standing time, sort ascending
        power_levels = [(timesteps[i]["cur_power"], i) for i in range(arrival_idx, depart_idx)]
        power_levels = sorted(power_levels)

        # find timesteps with same power level
        idx = 0
        prev_power = power_levels[0][0]
        prev_energy = 0
        power = 0
        eff = sim_vehicle.battery.efficiency
        while idx < len(power_levels) and energy_needed - prev_energy > self.EPS:
            if power_levels[idx][0] - prev_power < self.EPS:
                idx += 1
                continue
            # power levels differ: try to fill up difference
            energy = 0
            power = power_levels[idx][0]
            for info in timesteps[arrival_idx:depart_idx]:
                p = min(power, info["max_power"])  # don't exceed current max power
                p = max(p - info["cur_power"], 0)  # cur_power higher: no power
                energy += p
            energy /= self.ts_per_hour / eff

            if energy - energy_needed > self.EPS:
                # compute fraction of energy needed
                frac = 1 - (energy - energy_needed) / (energy - prev_energy)
                power = prev_power + frac * (power - prev_power)
                prev_energy = energy_needed
                break

            prev_power = power
            prev_energy = energy
        if energy_needed - prev_energy > self.EPS:
            # energy need not satisfied yet: must exceed highest power peak
            # distribute evenly over timesteps (ignore power restrictions)
            power = prev_power + (energy_needed - prev_energy) * self.ts_per_hour / idx / eff

        opt_power = power
        # charge (in order of timesteps)
        delta = 0
        command = 0
        for pl in sorted(power_levels[:idx], key=lambda x: x[1]):
            power = min(opt_power + delta, timesteps[pl[1]]["max_power"]) - pl[0]
            power = util.clamp_power(power, sim_vehicle, cs)
            avg_power = sim_vehicle.battery.load(self.interval, target_power=power)["avg_power"]
            timesteps[pl[1]]["cur_power"] += avg_power
            delta += power - avg_power
            # charged_energy += avg_power / self.ts_per_hour * eff
            if pl[1] == 0:
                command = power
        return command