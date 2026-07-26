from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from sigenergy_exporter.config import (
    ConfigManager,
    load_exporter_config,
    parse_config_document,
)
from tests.helpers import REPOSITORY_CONFIG, copied_document, valid_config_document


class RepositoryConfigTests(unittest.TestCase):
    def test_repository_config_has_default_v25_module(self) -> None:
        config = load_exporter_config(REPOSITORY_CONFIG)
        self.assertEqual(config.default_module, "sigenstor_plant_v2_5")
        module = config.modules["sigenstor_plant_v2_5"]
        self.assertEqual(module.unit_id, 247)
        self.assertEqual(module.function_code, 4)
        self.assertEqual(
            [
                (block.name, block.address, block.count, block.required)
                for block in module.blocks
            ],
            [
                ("plant", 30003, 70, True),
                ("ess_details", 30083, 5, False),
            ],
        )
        names = {metric.name for metric in module.metrics}
        self.assertIn("sigenergy_battery_state_of_charge_ratio", names)
        self.assertIn("sigenergy_battery_rated_energy_joules", names)
        self.assertNotIn("sigenergy_general_alarm", names)
        metrics = {metric.name: metric for metric in module.metrics}
        self.assertEqual(
            metrics["sigenergy_ems_mode"].states.values,
            (
                (0, "max_self_consumption"),
                (1, "ai_mode"),
                (2, "time_of_use"),
                (7, "remote_ems"),
            ),
        )
        self.assertEqual(metrics["sigenergy_grid_mode"].states.label, "mode")
        self.assertEqual(metrics["sigenergy_plant_state"].states.label, "state")

    def test_repository_config_has_v25_inverter_module(self) -> None:
        config = load_exporter_config(REPOSITORY_CONFIG)
        module = config.modules["sigenstor_inverter_v2_5"]
        self.assertEqual(module.unit_id, 1)
        self.assertEqual(module.function_code, 3)
        self.assertEqual(
            [
                (block.name, block.address, block.count, block.required)
                for block in module.blocks
            ],
            [
                ("inverter", 30540, 84, True),
                ("electrical", 31000, 42, True),
            ],
        )
        names = {metric.name for metric in module.metrics}
        self.assertIn("sigenergy_inverter_active_power_watts", names)
        self.assertIn("sigenergy_inverter_pv_string_voltage_volts", names)
        self.assertIn("sigenergy_inverter_battery_state_of_charge_ratio", names)
        self.assertFalse(any("alarm" in name for name in names))
        self.assertFalse(any("30282" in metric.help for metric in module.metrics))
        metrics = {metric.name: metric for metric in module.metrics}
        self.assertEqual(metrics["sigenergy_inverter_state"].states.label, "state")
        self.assertEqual(
            metrics["sigenergy_inverter_output_type"].states.values,
            (
                (0, "line_neutral"),
                (1, "three_phase_three_wire"),
                (2, "three_phase_four_wire"),
                (3, "split_phase"),
            ),
        )


class ValidationTests(unittest.TestCase):
    def test_function_code_defaults_to_fc04(self) -> None:
        config = parse_config_document(copied_document())
        self.assertEqual(config.modules["test_module"].function_code, 4)

    def test_accepts_fc03(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["function_code"] = 3
        config = parse_config_document(document)
        self.assertEqual(config.modules["test_module"].function_code, 3)

    def test_rejects_unsupported_function_code(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["function_code"] = 6
        with self.assertRaisesRegex(ValueError, "must be 3 or 4"):
            parse_config_document(document)

    def test_default_module_must_exist(self) -> None:
        document = copied_document()
        document["default_module"] = "missing"
        with self.assertRaisesRegex(ValueError, "must reference"):
            parse_config_document(document)

    def test_rejects_unknown_keys(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["typo"] = True
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            parse_config_document(document)

    def test_rejects_unsafe_request_gap(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["request_gap_seconds"] = 0.99
        with self.assertRaisesRegex(ValueError, "at least 1"):
            parse_config_document(document)

    def test_rejects_metric_outside_block(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"][0]["offset"] = 3
        with self.assertRaisesRegex(ValueError, "extends beyond"):
            parse_config_document(document)

    def test_rejects_duplicate_metric_sample(self) -> None:
        document = copied_document()
        duplicate = dict(document["modules"]["test_module"]["metrics"][0])
        document["modules"]["test_module"]["metrics"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "Duplicate metric sample"):
            parse_config_document(document)

    def test_rejects_inconsistent_family_help(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"][1]["help"] = "Different."
        with self.assertRaisesRegex(ValueError, "one HELP string"):
            parse_config_document(document)

    def test_rejects_inconsistent_family_labels(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"][1]["labels"] = {"line": "b"}
        with self.assertRaisesRegex(ValueError, "label schema"):
            parse_config_document(document)

    def test_rejects_block_without_metrics(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"] = document["modules"][
            "test_module"
        ]["metrics"][:2]
        with self.assertRaisesRegex(ValueError, "without metrics"):
            parse_config_document(document)

    def test_rejects_counter_suffix_for_gauge_metric(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"][0]["name"] = (
            "sigenergy_test_total"
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            parse_config_document(document)

    def test_rejects_zero_multiplier(self) -> None:
        document = copied_document()
        document["modules"]["test_module"]["metrics"][0]["multiplier"] = 0
        with self.assertRaisesRegex(ValueError, "must not be zero"):
            parse_config_document(document)

    def test_rejects_invalid_state_mapping(self) -> None:
        document = copied_document()
        metric = document["modules"]["test_module"]["metrics"][0]
        metric["states"] = {
            "label": "state",
            "values": {0: "valid", 1: "unknown"},
        }
        with self.assertRaisesRegex(ValueError, "reserves"):
            parse_config_document(document)

    def test_state_label_cannot_conflict_with_static_label(self) -> None:
        document = copied_document()
        metric = document["modules"]["test_module"]["metrics"][0]
        metric["states"] = {
            "label": "phase",
            "values": {0: "off", 1: "on"},
        }
        with self.assertRaisesRegex(ValueError, "conflicts"):
            parse_config_document(document)


class ConfigManagerTests(unittest.TestCase):
    def test_default_module_is_used_when_name_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sigenergy.yml"
            path.write_text(yaml.safe_dump(valid_config_document()), encoding="utf-8")
            manager = ConfigManager(path)
            name, module = manager.module(None)
            self.assertEqual(name, "test_module")
            self.assertEqual(module.unit_id, 247)

    def test_reload_swaps_only_valid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sigenergy.yml"
            path.write_text(yaml.safe_dump(valid_config_document()), encoding="utf-8")
            manager = ConfigManager(path)

            updated = copied_document()
            updated["modules"]["second"] = updated["modules"]["test_module"]
            path.write_text(yaml.safe_dump(updated), encoding="utf-8")
            manager.reload()
            self.assertEqual(manager.module_names(), ("second", "test_module"))

            path.write_text("version: 999\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                manager.reload()
            self.assertEqual(manager.module_names(), ("second", "test_module"))


if __name__ == "__main__":
    unittest.main()
