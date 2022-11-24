import json
import pytest
from pathlib import Path
import subprocess

from src import scenario
import calculate_costs as cc

TEST_REPO_PATH = Path(__file__).parent
supported_strategies = ["greedy", "balanced", "distributed", "balanced_market",
                        "schedule", "flex_window"]


def get_test_json():
    # get minimum working json example
    return {
        "scenario": {
            "start_time": "2020-01-01T00:00:00+02:00",
            "interval": 15,
            "n_intervals": 96
        },
        "constants": {
            "grid_connectors": {
                "GC1": {"max_power": 100, "cost": {"type": "fixed", "value": 0}}
            },
            "charging_stations": {},
            "vehicle_types": {},
            "vehicles": {},
        },
        "events": {
            "external_loads": {},
            "grid_operator_signals": [],
            "vehicle_events": [],
        }
    }


class TestSimulationCosts:
    def test_read_sim_csv(self, tmp_path):
        j = get_test_json()
        s = scenario.Scenario(j)
        save_timeseries = tmp_path / "save_timeseries.csv"
        s.run('greedy', {"save_timeseries": str(save_timeseries)})
        result = cc.read_simulation_csv(str(save_timeseries))

        # check length of result lists
        for k, l in result.items():
            assert len(l) == s.n_intervals, f"list {k} has wrong length"

        # check individual lists
        # timestamps
        assert result["timestamps_list"][0] == s.start_time.replace(tzinfo=None)
        assert result["timestamps_list"][-1] == (s.stop_time - s.interval).replace(tzinfo=None)
        # price: all zeroes
        assert sum(result["price_list"]) == 0
        # grid supply: 0
        assert sum(result["power_grid_supply_list"]) == 0
        # fix load: 0
        assert sum(result["power_fix_load_list"]) == 0
        # charging signal: depends on schedule, should be all None
        assert not any(result["charging_signal_list"])

    def test_calculate_costs_basic(self):
        j = get_test_json()
        s = scenario.Scenario(j)
        s.run('greedy', {"cost_calculation": True})
        timeseries = s.GC1_timeseries
        timeseries_lists = [timeseries.get(k, [0]*s.n_intervals) for k in [
                            "time", "grid power [kW]", "price [EUR/kWh]",
                            "ext.load [kW]", "window"]]
        price_sheet = TEST_REPO_PATH / 'test_data/input_test_cost_calculation/price_sheet.json'

        # test all supported strategies
        for strategy in supported_strategies:
            cc.calculate_costs(strategy, "MV", s.interval, *timeseries_lists,
                               core_standing_time_dict=s.core_standing_time,
                               price_sheet_json=str(price_sheet))

        # test error for non-supported strategy
        with pytest.raises(Exception):
            cc.calculate_costs("strategy", "MV", s.interval, *timeseries_lists,
                               core_standing_time_dict=s.core_standing_time,
                               price_sheet_json=str(price_sheet))

        # check returned values
        result = cc.calculate_costs(supported_strategies[0], "MV", s.interval, *timeseries_lists,
                                    core_standing_time_dict=s.core_standing_time,
                                    price_sheet_json=str(price_sheet))
        assert result["total_costs_per_year"] == 78.18
        assert result["commodity_costs_eur_per_year"] == 0
        assert result["capacity_costs_eur"] == 65.7
        assert result["power_procurement_per_year"] == 0
        assert result["levies_fees_and_taxes_per_year"] == 12.48
        assert result["feed_in_remuneration_per_year"] == 0

    def test_calculate_costs_advanced(self):

        scenarios = {
            "scenario_A.json": [2522.67, 776.54, 65.7, 799.38, 881.06, 0.0],
            "scenario_B.json": [21798.38, 6899.83, 65.7, 7102.77, 7730.09, 0.0],
            "scenario_C1.json": [3045.64, 942.67, 65.7, 970.4, 1066.87, 0.0],
            "scenario_C2.json": [2792.32, 862.2, 65.7, 887.56, 976.87, 0.0],
            "scenario_C3.json": [1887.55, 574.78, 65.7, 591.69, 655.39, 0.0],
            # "bus_scenario_D.json": [0,0,0,0,0,0],  # buggy: can't charge enough
            "scenario_PV_Bat.json": [-2166.41, 0.0, 65.7, 0.0, 12.48, 2244.59],
        }

        for scenario_name, expected in scenarios.items():
            scen_path = TEST_REPO_PATH.joinpath("test_data/input_test_strategies", scenario_name)
            with scen_path.open() as f:
                j = json.load(f)
            s = scenario.Scenario(j, str(scen_path.parent))
            s.run("greedy", {"cost_calculation": True})
            timeseries = s.GC1_timeseries
            timeseries_lists = [timeseries.get(k, [0]*s.n_intervals) for k in [
                            "time", "grid power [kW]", "price [EUR/kWh]",
                            "ext.load [kW]", "window"]]
            price_sheet = TEST_REPO_PATH / 'test_data/input_test_cost_calculation/price_sheet.json'
            pv = sum([pv.nominal_power for pv in s.constants.photovoltaics.values()])
            result = cc.calculate_costs("greedy", "MV", s.interval, *timeseries_lists,
                                        s.core_standing_time, str(price_sheet), None, pv)

            for i, value in enumerate(result.values()):
                assert value == expected[i]


class TestPostSimulationCosts:
    def test_calculate_costs_post_sim(self, tmp_path):
        j = get_test_json()
        s = scenario.Scenario(j)
        save_results = tmp_path / "save_results.json"
        save_timeseries = tmp_path / "save_timeseries.csv"
        price_sheet = TEST_REPO_PATH / 'test_data/input_test_cost_calculation/price_sheet.json'

        s.run("greedy", {
            "save_results": str(save_results),
            "save_timeseries": str(save_timeseries)
        })

        # call calculate cost from shell
        assert subprocess.call([
            "python", TEST_REPO_PATH.parent / "calculate_costs.py",
            "--voltage-level", "MV",
            "--get-results", save_results,
            "--get-timeseries", save_timeseries,
            "--cost-parameters-file", price_sheet
        ]) == 0
        with save_results.open() as f:
            results = json.load(f)
        assert "costs" in results
        assert results["costs"]["electricity costs"]["per year"]["total (gross)"] == 78.18