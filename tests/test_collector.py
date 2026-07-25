from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from sigenergy_exporter.collector import (
    SigenergyCollector,
    ScrapeTimeoutError,
    Target,
    TargetCoordinator,
    collect_target,
    decode_register_value,
    parse_target,
)
from sigenergy_exporter.config import load_exporter_config
from tests.helpers import ClientFactory, FakeResponse, REPOSITORY_CONFIG, valid_module


class FakeClock:
    def __init__(self, value: float = 0) -> None:
        self.value = value
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.value += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class TargetTests(unittest.TestCase):
    def test_target_forms(self) -> None:
        self.assertEqual(parse_target("plant.example"), Target("plant.example", 502))
        self.assertEqual(
            parse_target("plant.example:1502"), Target("plant.example", 1502)
        )
        self.assertEqual(parse_target("[2001:db8::1]:502"), Target("2001:db8::1", 502))

    def test_rejects_urls_and_unbracketed_ipv6(self) -> None:
        with self.assertRaises(ValueError):
            parse_target("tcp://plant.example:502")
        with self.assertRaisesRegex(ValueError, "bracketed"):
            parse_target("2001:db8::1")


class DecodeTests(unittest.TestCase):
    def test_decodes_all_integer_widths_high_word_first(self) -> None:
        self.assertEqual(decode_register_value([0x1234], 0, "u16"), 0x1234)
        self.assertEqual(decode_register_value([0xFFFF], 0, "s16"), -1)
        self.assertEqual(
            decode_register_value([0x1234, 0x5678], 0, "u32"),
            0x12345678,
        )
        self.assertEqual(decode_register_value([0xFFFF, 0xFFFE], 0, "s32"), -2)
        self.assertEqual(
            decode_register_value([1, 2, 3, 4], 0, "u64"),
            0x0001000200030004,
        )
        self.assertEqual(
            decode_register_value([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF], 0, "s64"),
            -1,
        )

    def test_rejects_invalid_or_short_register_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "beyond"):
            decode_register_value([1], 0, "u32")
        with self.assertRaisesRegex(ValueError, "16-bit"):
            decode_register_value([65536], 0, "u16")


class CoordinatorTests(unittest.TestCase):
    def test_pacing_is_preserved_across_sessions(self) -> None:
        clock = FakeClock(10)
        coordinator = TargetCoordinator(monotonic=clock.monotonic, sleep=clock.sleep)
        target = Target("192.0.2.1")
        with coordinator.session(target) as state:
            coordinator.wait_for_request_slot(state, 1)
        clock.advance(0.25)
        with coordinator.session(target) as state:
            coordinator.wait_for_request_slot(state, 1)
        self.assertEqual(clock.sleeps, [0.75])

    def test_same_target_sessions_are_serialized(self) -> None:
        coordinator = TargetCoordinator()
        target = Target("192.0.2.1")
        entered = threading.Event()

        def contender() -> None:
            with coordinator.session(target):
                entered.set()

        with coordinator.session(target):
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertFalse(entered.wait(0.05))
        self.assertTrue(entered.wait(1))
        thread.join(1)

    def test_different_targets_do_not_share_a_lock(self) -> None:
        coordinator = TargetCoordinator()
        entered = threading.Event()

        def contender() -> None:
            with coordinator.session(Target("192.0.2.2")):
                entered.set()

        with coordinator.session(Target("192.0.2.1")):
            thread = threading.Thread(target=contender)
            thread.start()
            self.assertTrue(entered.wait(1))
        thread.join(1)

    def test_deadline_rejects_request_that_cannot_fit_pacing_and_timeout(
        self,
    ) -> None:
        clock = FakeClock(10)
        coordinator = TargetCoordinator(monotonic=clock.monotonic, sleep=clock.sleep)
        target = Target("192.0.2.1")
        with coordinator.session(target) as state:
            coordinator.wait_for_request_slot(state, 1)
        clock.advance(0.25)
        with coordinator.session(target) as state:
            with self.assertRaises(ScrapeTimeoutError):
                coordinator.wait_for_request_slot(
                    state,
                    1,
                    deadline=13,
                    minimum_time_remaining=3,
                )
        self.assertEqual(clock.sleeps, [])


class CollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = valid_module()
        self.target = Target("192.0.2.1")
        self.clock = FakeClock()
        self.coordinator = TargetCoordinator(
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def render(self, factory: ClientFactory) -> str:
        registry = CollectorRegistry()
        registry.register(
            SigenergyCollector(
                self.target,
                "test_module",
                self.module,
                self.coordinator,
                client_factory=factory,
                monotonic=self.clock.monotonic,
            )
        )
        return generate_latest(registry).decode()

    def test_uses_fc04_blocks_unit_id_scaling_and_static_labels(self) -> None:
        factory = ClientFactory(
            {
                30000: FakeResponse([0, 100, 0xFFFF, 0xFF9C]),
                30010: FakeResponse([750]),
            }
        )
        output = self.render(factory)
        self.assertIn('sigenergy_test_phase_watts{phase="a"} 100.0', output)
        self.assertIn('sigenergy_test_phase_watts{phase="b"} -100.0', output)
        self.assertIn("sigenergy_test_ratio 0.75", output)
        self.assertIn("sigenergy_up 1.0", output)
        self.assertEqual(
            factory.clients[0].calls,
            [(30000, 4, 247), (30010, 1, 247)],
        )
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertTrue(factory.clients[0].closed)
        self.assertEqual(
            factory.clients[0].connection,
            {"host": "192.0.2.1", "port": 502, "timeout": 3.0},
        )

    def test_optional_failure_keeps_up_and_omits_optional_metrics(self) -> None:
        factory = ClientFactory(
            {
                30000: FakeResponse([0, 1, 0, 2]),
                30010: FakeResponse([], error=True),
            }
        )
        with patch("sigenergy_exporter.collector.LOG.warning"):
            output = self.render(factory)
        self.assertIn("sigenergy_up 1.0", output)
        self.assertIn('sigenergy_register_block_up{block="optional"} 0.0', output)
        self.assertNotIn("sigenergy_test_ratio", output)
        self.assertNotIn("NaN", output)

    def test_required_failure_returns_up_zero_and_keeps_optional_data(self) -> None:
        factory = ClientFactory(
            {
                30000: FakeResponse([], error=True),
                30010: FakeResponse([500]),
            }
        )
        with patch("sigenergy_exporter.collector.LOG.warning"):
            output = self.render(factory)
        self.assertIn("sigenergy_up 0.0", output)
        self.assertIn("sigenergy_test_ratio 0.5", output)
        self.assertNotIn("sigenergy_test_phase_watts", output)
        self.assertNotIn("NaN", output)

    def test_connect_failure_closes_client_and_emits_only_scrape_metrics(self) -> None:
        factory = ClientFactory(connect=False)
        with patch("sigenergy_exporter.collector.LOG.warning"):
            output = self.render(factory)
        self.assertIn("sigenergy_up 0.0", output)
        self.assertNotIn("sigenergy_test_phase_watts", output)
        self.assertTrue(factory.clients[0].closed)

    def test_output_is_valid_prometheus_exposition(self) -> None:
        output = self.render(ClientFactory())
        families = list(text_string_to_metric_families(output))
        self.assertTrue(any(family.name == "sigenergy_up" for family in families))

    def test_collect_target_paces_repeated_scrapes_of_same_target(self) -> None:
        factory = ClientFactory()
        collect_target(
            self.target,
            "test_module",
            self.module,
            self.coordinator,
            client_factory=factory,
            monotonic=self.clock.monotonic,
        )
        collect_target(
            self.target,
            "test_module",
            self.module,
            self.coordinator,
            client_factory=factory,
            monotonic=self.clock.monotonic,
        )
        self.assertEqual(self.clock.sleeps, [1.0, 1.0, 1.0])

    def test_deadline_shorter_than_modbus_timeout_skips_connection(self) -> None:
        factory = ClientFactory()
        result = collect_target(
            self.target,
            "test_module",
            self.module,
            self.coordinator,
            client_factory=factory,
            monotonic=self.clock.monotonic,
            scrape_timeout_seconds=2.5,
        )
        self.assertFalse(result.required_success)
        self.assertEqual(factory.clients, [])

    def test_deadline_omits_optional_block_that_cannot_fit(self) -> None:
        factory = ClientFactory()
        result = collect_target(
            self.target,
            "test_module",
            self.module,
            self.coordinator,
            client_factory=factory,
            monotonic=self.clock.monotonic,
            scrape_timeout_seconds=3.5,
        )
        self.assertTrue(result.required_success)
        self.assertEqual(result.block_success, {"required": True, "optional": False})
        self.assertEqual(factory.clients[0].calls, [(30000, 4, 247)])

    def test_repository_module_normalizes_representative_v25_values(self) -> None:
        module = load_exporter_config(REPOSITORY_CONFIG).modules["sigenstor_plant_v2_5"]
        plant = [0] * 70
        details = [0] * 5
        plant[0] = 99

        def set_u32(registers: list[int], offset: int, value: int) -> None:
            registers[offset] = (value >> 16) & 0xFFFF
            registers[offset + 1] = value & 0xFFFF

        set_u32(plant, 2, 400)
        plant[11] = 750
        set_u32(plant, 12, 123)
        set_u32(plant, 14, 0xFFFFFF9C)
        set_u32(plant, 34, 0xFFFFFF38)
        set_u32(plant, 61, 100)
        set_u32(details, 0, 3200)
        details[2:5] = [950, 100, 987]

        registry = CollectorRegistry()
        registry.register(
            SigenergyCollector(
                self.target,
                "sigenstor_plant_v2_5",
                module,
                self.coordinator,
                client_factory=ClientFactory(
                    {
                        30003: FakeResponse(plant),
                        30083: FakeResponse(details),
                    }
                ),
                monotonic=self.clock.monotonic,
            )
        )
        output = generate_latest(registry).decode()
        self.assertIn("sigenergy_grid_active_power_watts 400.0", output)
        self.assertIn("sigenergy_battery_state_of_charge_ratio 0.75", output)
        self.assertIn(
            'sigenergy_plant_phase_active_power_watts{phase="a"} 123.0',
            output,
        )
        self.assertIn(
            'sigenergy_plant_phase_active_power_watts{phase="b"} -100.0',
            output,
        )
        self.assertIn("sigenergy_battery_power_watts -200.0", output)
        self.assertIn(
            "sigenergy_battery_available_charge_energy_joules 3.6e+06",
            output,
        )
        self.assertIn("sigenergy_battery_rated_energy_joules 1.152e+08", output)
        self.assertIn(
            "sigenergy_battery_charge_cutoff_state_of_charge_ratio 0.95",
            output,
        )
        self.assertIn(
            "sigenergy_battery_discharge_cutoff_state_of_charge_ratio 0.1",
            output,
        )
        self.assertIn("sigenergy_battery_state_of_health_ratio 0.987", output)
        self.assertIn('sigenergy_ems_mode{mode="unknown"} 1.0', output)
        self.assertIn('sigenergy_ems_mode{mode="ai_mode"} 0.0', output)
        self.assertIn('sigenergy_grid_mode{mode="on_grid"} 1.0', output)
        self.assertIn('sigenergy_plant_state{state="standby"} 1.0', output)


if __name__ == "__main__":
    unittest.main()
