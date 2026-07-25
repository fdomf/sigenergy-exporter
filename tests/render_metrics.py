"""Render deterministic target metrics for promtool validation."""

from __future__ import annotations

import sys

from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest

from sigenergy_exporter.collector import SigenergyCollector, Target, TargetCoordinator
from sigenergy_exporter.config import load_exporter_config
from tests.helpers import ClientFactory, REPOSITORY_CONFIG

now = 0.0


def monotonic() -> float:
    return now


def sleep(seconds: float) -> None:
    global now
    now += seconds


config = load_exporter_config(REPOSITORY_CONFIG)
module_name = config.default_module
registry = CollectorRegistry()
registry.register(
    SigenergyCollector(
        Target("192.0.2.1"),
        module_name,
        config.modules[module_name],
        TargetCoordinator(monotonic=monotonic, sleep=sleep),
        client_factory=ClientFactory(),
        monotonic=monotonic,
    )
)
sys.stdout.buffer.write(generate_latest(registry))
