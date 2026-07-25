"""Read-only Modbus collection and request-local Prometheus metrics."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from prometheus_client.core import GaugeMetricFamily
from pymodbus.client import ModbusTcpClient

from sigenergy_exporter.config import (
    TYPE_WIDTHS,
    ModuleConfig,
    RegisterBlock,
    RegisterType,
)
from sigenergy_exporter.metrics import MODBUS_REQUEST_ERRORS, MODBUS_REQUESTS

LOG = logging.getLogger("sigenergy_exporter")
DEFAULT_MODBUS_PORT = 502


class ScrapeTimeoutError(TimeoutError):
    """The Prometheus scrape deadline cannot accommodate more Modbus work."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int = DEFAULT_MODBUS_PORT

    @property
    def key(self) -> tuple[str, int]:
        return (self.host.casefold(), self.port)

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("target port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("target port must be between 1 and 65535")
    return port


def parse_target(value: str) -> Target:
    target = value.strip()
    if not target:
        raise ValueError("target must not be empty")
    if "://" in target or any(char in target for char in "/?#@"):
        raise ValueError("target must be a host or host:port")

    port = DEFAULT_MODBUS_PORT
    if target.startswith("["):
        closing = target.find("]")
        if closing < 2:
            raise ValueError("invalid bracketed IPv6 target")
        host = target[1:closing]
        suffix = target[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:]:
                raise ValueError("invalid target port")
            port = _parse_port(suffix[1:])
    elif target.count(":") == 0:
        host = target
    elif target.count(":") == 1:
        host, raw_port = target.rsplit(":", 1)
        port = _parse_port(raw_port)
    else:
        raise ValueError("IPv6 targets must use bracketed [address]:port syntax")
    if not host or any(char.isspace() for char in host):
        raise ValueError("target host is invalid")
    return Target(host=host, port=port)


def decode_unsigned(registers: Sequence[int], offset: int, width: int) -> int:
    value = 0
    selected = registers[offset : offset + width]
    if len(selected) != width:
        raise ValueError("register value extends beyond response")
    for register in selected:
        if not 0 <= int(register) <= 0xFFFF:
            raise ValueError("register value is outside the 16-bit range")
        value = (value << 16) | int(register)
    return value


def decode_signed(registers: Sequence[int], offset: int, width: int) -> int:
    value = decode_unsigned(registers, offset, width)
    bits = width * 16
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def decode_register_value(
    registers: Sequence[int],
    offset: int,
    register_type: RegisterType,
) -> int:
    width = TYPE_WIDTHS[register_type]
    if register_type.startswith("u"):
        return decode_unsigned(registers, offset, width)
    return decode_signed(registers, offset, width)


def scale_register_value(value: int, multiplier: float) -> float:
    """Apply a decimal configuration scale without noisy binary artifacts."""
    return float(Decimal(value) * Decimal(str(multiplier)))


@dataclass
class _TargetState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_request_at: float | None = None
    retain_until: float = 0.0
    users: int = 0
    last_used_at: float = 0.0


class TargetCoordinator:
    """Serialize each target and preserve request pacing across scrapes."""

    def __init__(
        self,
        *,
        max_targets: int = 1024,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_targets < 1:
            raise ValueError("max_targets must be at least 1")
        self._max_targets = max_targets
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._states: dict[tuple[str, int], _TargetState] = {}

    def _state_for(self, target: Target) -> _TargetState:
        with self._lock:
            state = self._states.get(target.key)
            if state is None:
                if len(self._states) >= self._max_targets:
                    now = self._monotonic()
                    removable = [
                        (key, candidate)
                        for key, candidate in self._states.items()
                        if candidate.users == 0 and candidate.retain_until <= now
                    ]
                    if not removable:
                        raise RuntimeError("target coordination capacity reached")
                    oldest_key, _oldest = min(
                        removable, key=lambda item: item[1].last_used_at
                    )
                    del self._states[oldest_key]
                state = _TargetState(last_used_at=self._monotonic())
                self._states[target.key] = state
            state.users += 1
            return state

    @contextmanager
    def session(
        self,
        target: Target,
        deadline: float | None = None,
    ) -> Iterator[_TargetState]:
        state = self._state_for(target)
        if deadline is None:
            acquired = state.lock.acquire()
        else:
            remaining = deadline - self._monotonic()
            acquired = remaining > 0 and state.lock.acquire(timeout=remaining)
        if not acquired:
            with self._lock:
                state.users -= 1
                state.last_used_at = self._monotonic()
            raise ScrapeTimeoutError("scrape deadline reached waiting for target")
        try:
            yield state
        finally:
            state.lock.release()
            with self._lock:
                state.users -= 1
                state.last_used_at = self._monotonic()

    def wait_for_request_slot(
        self,
        state: _TargetState,
        request_gap_seconds: float,
        deadline: float | None = None,
        minimum_time_remaining: float = 0.0,
    ) -> float:
        now = self._monotonic()
        wait_seconds = 0.0
        if state.last_request_at is not None:
            wait_seconds = max(
                0.0,
                request_gap_seconds - (now - state.last_request_at),
            )
        if (
            deadline is not None
            and wait_seconds + minimum_time_remaining > deadline - now
        ):
            raise ScrapeTimeoutError(
                "scrape deadline cannot accommodate request pacing and timeout"
            )
        if wait_seconds > 0:
            self._sleep(wait_seconds)
        request_at = self._monotonic()
        if deadline is not None and request_at >= deadline:
            raise ScrapeTimeoutError("scrape deadline reached before Modbus request")
        state.last_request_at = request_at
        state.retain_until = request_at + request_gap_seconds
        return request_at


class ModbusClient(Protocol):
    def connect(self) -> bool: ...

    def close(self) -> None: ...

    def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> object: ...


@dataclass(frozen=True)
class CollectionResult:
    duration_seconds: float
    required_success: bool
    block_success: Mapping[str, bool]
    block_data: Mapping[str, Sequence[int]]


def read_input_block(
    client: ModbusClient,
    block: RegisterBlock,
    module_name: str,
    module: ModuleConfig,
    coordinator: TargetCoordinator,
    state: _TargetState,
    deadline: float | None,
) -> list[int]:
    coordinator.wait_for_request_slot(
        state,
        module.request_gap_seconds,
        deadline,
        module.timeout_seconds,
    )
    MODBUS_REQUESTS.labels(module=module_name, block=block.name).inc()
    try:
        response = client.read_input_registers(
            block.address,
            count=block.count,
            device_id=module.unit_id,
        )
        if not hasattr(response, "isError") or response.isError():
            raise RuntimeError(
                f"Modbus exception reading {block.name} "
                f"({block.address}+{block.count}): {response}"
            )
        registers = list(response.registers)
        if len(registers) != block.count:
            raise RuntimeError(
                f"Short Modbus response reading {block.name} "
                f"({block.address}+{block.count}): expected {block.count}, "
                f"got {len(registers)}"
            )
    except Exception:
        MODBUS_REQUEST_ERRORS.labels(module=module_name, block=block.name).inc()
        raise
    return registers


def collect_target(
    target: Target,
    module_name: str,
    module: ModuleConfig,
    coordinator: TargetCoordinator,
    *,
    client_factory: Callable[..., ModbusClient] = ModbusTcpClient,
    monotonic: Callable[[], float] = time.monotonic,
    scrape_timeout_seconds: float | None = None,
) -> CollectionResult:
    started = monotonic()
    deadline = (
        started + scrape_timeout_seconds if scrape_timeout_seconds is not None else None
    )
    block_success = {block.name: False for block in module.blocks}
    block_data: dict[str, list[int]] = {}
    client: ModbusClient | None = None

    try:
        session = coordinator.session(target, deadline)
        with session as state:
            try:
                if (
                    deadline is not None
                    and deadline - monotonic() < module.timeout_seconds
                ):
                    raise ScrapeTimeoutError(
                        "scrape deadline cannot accommodate connection timeout"
                    )
                client = client_factory(
                    host=target.host,
                    port=target.port,
                    timeout=module.timeout_seconds,
                )
                if not client.connect():
                    raise ConnectionError(f"Could not connect to {target.display}")
                for block in module.blocks:
                    try:
                        block_data[block.name] = read_input_block(
                            client,
                            block,
                            module_name,
                            module,
                            coordinator,
                            state,
                            deadline,
                        )
                        block_success[block.name] = True
                    except ScrapeTimeoutError:
                        LOG.info(
                            "Scrape deadline reached before block %s for target %s "
                            "module %s",
                            block.name,
                            target.display,
                            module_name,
                        )
                        break
                    except Exception:
                        LOG.warning(
                            "Block %s failed for target %s module %s",
                            block.name,
                            target.display,
                            module_name,
                            exc_info=True,
                        )
            except ScrapeTimeoutError:
                LOG.info(
                    "Scrape deadline reached for target %s module %s",
                    target.display,
                    module_name,
                )
            except Exception:
                LOG.warning(
                    "Collection failed for target %s module %s",
                    target.display,
                    module_name,
                    exc_info=True,
                )
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        LOG.warning(
                            "Failed to close Modbus client for target %s",
                            target.display,
                            exc_info=True,
                        )
    except ScrapeTimeoutError:
        LOG.info(
            "Scrape deadline reached waiting for target %s module %s",
            target.display,
            module_name,
        )

    required_success = all(
        block_success[block.name] for block in module.blocks if block.required
    )
    return CollectionResult(
        duration_seconds=monotonic() - started,
        required_success=required_success,
        block_success=block_success,
        block_data=block_data,
    )


class SigenergyCollector:
    """One request-local custom collector for one target and module."""

    def __init__(
        self,
        target: Target,
        module_name: str,
        module: ModuleConfig,
        coordinator: TargetCoordinator,
        *,
        client_factory: Callable[..., ModbusClient] = ModbusTcpClient,
        monotonic: Callable[[], float] = time.monotonic,
        scrape_timeout_seconds: float | None = None,
    ) -> None:
        self._target = target
        self._module_name = module_name
        self._module = module
        self._coordinator = coordinator
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._scrape_timeout_seconds = scrape_timeout_seconds

    def collect(self) -> Iterator[GaugeMetricFamily]:
        result = collect_target(
            self._target,
            self._module_name,
            self._module,
            self._coordinator,
            client_factory=self._client_factory,
            monotonic=self._monotonic,
            scrape_timeout_seconds=self._scrape_timeout_seconds,
        )
        yield GaugeMetricFamily(
            "sigenergy_up",
            "Whether all required Sigenergy register blocks were collected.",
            value=1 if result.required_success else 0,
        )
        yield GaugeMetricFamily(
            "sigenergy_scrape_duration_seconds",
            "Duration of the Sigenergy Modbus collection in seconds.",
            value=result.duration_seconds,
        )
        block_up = GaugeMetricFamily(
            "sigenergy_register_block_up",
            "Whether a Sigenergy register block was collected in this scrape.",
            labels=["block"],
        )
        for block in self._module.blocks:
            block_up.add_metric(
                [block.name],
                1 if result.block_success[block.name] else 0,
            )
        yield block_up

        families: dict[str, GaugeMetricFamily] = {}
        for spec in self._module.metrics:
            registers = result.block_data.get(spec.block)
            if registers is None:
                continue
            decoded_value = decode_register_value(
                registers,
                spec.offset,
                spec.register_type,
            )
            family = families.get(spec.name)
            if family is None:
                label_names = list(spec.label_names)
                if spec.states is not None:
                    label_names.append(spec.states.label)
                family = GaugeMetricFamily(
                    spec.name,
                    spec.help,
                    labels=label_names,
                )
                families[spec.name] = family
            if spec.states is None:
                value = scale_register_value(decoded_value, spec.multiplier)
                family.add_metric(list(spec.label_values), value)
                continue
            active_state = spec.states.name_for(decoded_value)
            for state_name in (*spec.states.names, "unknown"):
                family.add_metric(
                    [*spec.label_values, state_name],
                    1 if state_name == active_state else 0,
                )
        yield from families.values()
