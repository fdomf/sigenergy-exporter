"""Strict configuration loading for Sigenergy protocol modules."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml

METRIC_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
LABEL_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
STATE_VALUE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

RegisterType = Literal["u16", "s16", "u32", "s32", "u64", "s64"]
TYPE_WIDTHS: Mapping[str, int] = MappingProxyType(
    {"u16": 1, "s16": 1, "u32": 2, "s32": 2, "u64": 4, "s64": 4}
)

COLLECTOR_METRIC_NAMES = {
    "sigenergy_up",
    "sigenergy_scrape_duration_seconds",
    "sigenergy_register_block_up",
}


class UnknownModuleError(ValueError):
    """The requested module is not present in the active configuration."""


@dataclass(frozen=True)
class RegisterBlock:
    name: str
    address: int
    count: int
    required: bool


@dataclass(frozen=True)
class StateSpec:
    label: str
    values: tuple[tuple[int, str], ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for _value, name in self.values)

    def name_for(self, value: int) -> str:
        return dict(self.values).get(value, "unknown")


@dataclass(frozen=True)
class MetricSpec:
    name: str
    help: str
    block: str
    offset: int
    register_type: RegisterType
    multiplier: float
    labels: tuple[tuple[str, str], ...]
    states: StateSpec | None

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(name for name, _value in self.labels)

    @property
    def label_values(self) -> tuple[str, ...]:
        return tuple(value for _name, value in self.labels)


@dataclass(frozen=True)
class ModuleConfig:
    unit_id: int
    timeout_seconds: float
    request_gap_seconds: float
    blocks: tuple[RegisterBlock, ...]
    metrics: tuple[MetricSpec, ...]


@dataclass(frozen=True)
class ExporterConfig:
    default_module: str
    modules: Mapping[str, ModuleConfig]


def _expect_keys(
    value: Mapping[object, object],
    required: set[str],
    context: str,
    *,
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    keys = set(value)
    unknown = keys - required - optional
    missing = required - keys
    if unknown:
        raise ValueError(f"{context} has unknown keys: {sorted(map(str, unknown))}")
    if missing:
        raise ValueError(f"{context} is missing keys: {sorted(missing)}")


def _require_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} must be an integer")
    return value


def _require_float(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _parse_labels(value: object, context: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping")
    labels: list[tuple[str, str]] = []
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not LABEL_NAME_PATTERN.fullmatch(raw_name):
            raise ValueError(f"{context} contains an invalid label name")
        if raw_name.startswith("__"):
            raise ValueError(f"{context} contains a reserved label name")
        if not isinstance(raw_value, str):
            raise ValueError(f"{context}.{raw_name} must be a string")
        labels.append((raw_name, raw_value))
    return tuple(sorted(labels))


def _parse_states(
    value: object,
    context: str,
    register_type: RegisterType,
) -> StateSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a mapping")
    _expect_keys(value, {"label", "values"}, context)

    label = value["label"]
    if not isinstance(label, str) or not LABEL_NAME_PATTERN.fullmatch(label):
        raise ValueError(f"{context}.label is invalid")
    if label.startswith("__"):
        raise ValueError(f"{context}.label is reserved")

    raw_values = value["values"]
    if not isinstance(raw_values, Mapping) or not raw_values:
        raise ValueError(f"{context}.values must be a non-empty mapping")

    width = TYPE_WIDTHS[register_type]
    bits = width * 16
    if register_type.startswith("u"):
        minimum, maximum = 0, (1 << bits) - 1
    else:
        minimum, maximum = -(1 << (bits - 1)), (1 << (bits - 1)) - 1

    states: list[tuple[int, str]] = []
    names: set[str] = set()
    for raw_code, raw_name in raw_values.items():
        code = _require_int(raw_code, f"{context}.values key")
        if not minimum <= code <= maximum:
            raise ValueError(f"{context}.values code {code} is outside {register_type}")
        if not isinstance(raw_name, str) or not STATE_VALUE_PATTERN.fullmatch(raw_name):
            raise ValueError(f"{context}.values[{code}] is not a snake_case state name")
        if raw_name == "unknown":
            raise ValueError(f"{context}.values reserves the state name 'unknown'")
        if raw_name in names:
            raise ValueError(
                f"{context}.values contains duplicate state name {raw_name}"
            )
        states.append((code, raw_name))
        names.add(raw_name)
    return StateSpec(label=label, values=tuple(sorted(states)))


def _parse_module(module_name: str, document: object) -> ModuleConfig:
    context = f"modules.{module_name}"
    if not isinstance(document, Mapping):
        raise ValueError(f"{context} must be a mapping")
    _expect_keys(
        document,
        {"unit_id", "timeout_seconds", "request_gap_seconds", "blocks", "metrics"},
        context,
    )

    unit_id = _require_int(document["unit_id"], f"{context}.unit_id")
    timeout_seconds = _require_float(
        document["timeout_seconds"], f"{context}.timeout_seconds"
    )
    request_gap_seconds = _require_float(
        document["request_gap_seconds"], f"{context}.request_gap_seconds"
    )
    if not 1 <= unit_id <= 247:
        raise ValueError(f"{context}.unit_id must be between 1 and 247")
    if timeout_seconds <= 0:
        raise ValueError(f"{context}.timeout_seconds must be greater than 0")
    if request_gap_seconds < 1:
        raise ValueError(f"{context}.request_gap_seconds must be at least 1")

    raw_blocks = document["blocks"]
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise ValueError(f"{context}.blocks must be a non-empty list")
    blocks: list[RegisterBlock] = []
    block_names: set[str] = set()
    for index, raw_block in enumerate(raw_blocks):
        block_context = f"{context}.blocks[{index}]"
        if not isinstance(raw_block, Mapping):
            raise ValueError(f"{block_context} must be a mapping")
        _expect_keys(raw_block, {"name", "address", "count", "required"}, block_context)
        name = raw_block["name"]
        if not isinstance(name, str) or not IDENTIFIER_PATTERN.fullmatch(name):
            raise ValueError(f"{block_context}.name is invalid")
        if name in block_names:
            raise ValueError(f"Duplicate register block name in {module_name}: {name}")
        address = _require_int(raw_block["address"], f"{block_context}.address")
        count = _require_int(raw_block["count"], f"{block_context}.count")
        required = raw_block["required"]
        if not 0 <= address <= 65535:
            raise ValueError(f"{block_context}.address must be between 0 and 65535")
        if not 1 <= count <= 124:
            raise ValueError(f"{block_context}.count must be between 1 and 124")
        if address + count > 65536:
            raise ValueError(f"{block_context} extends beyond the Modbus address space")
        if not isinstance(required, bool):
            raise ValueError(f"{block_context}.required must be true or false")
        blocks.append(RegisterBlock(name, address, count, required))
        block_names.add(name)
    if not any(block.required for block in blocks):
        raise ValueError(f"{context} must have at least one required block")

    raw_metrics = document["metrics"]
    if not isinstance(raw_metrics, list) or not raw_metrics:
        raise ValueError(f"{context}.metrics must be a non-empty list")
    blocks_by_name = {block.name: block for block in blocks}
    metrics: list[MetricSpec] = []
    family_contracts: dict[
        str,
        tuple[str, tuple[str, ...], tuple[str, ...] | None],
    ] = {}
    family_samples: set[tuple[str, tuple[str, ...]]] = set()
    blocks_with_metrics: set[str] = set()
    for index, raw_metric in enumerate(raw_metrics):
        metric_context = f"{context}.metrics[{index}]"
        if not isinstance(raw_metric, Mapping):
            raise ValueError(f"{metric_context} must be a mapping")
        _expect_keys(
            raw_metric,
            {
                "name",
                "help",
                "block",
                "offset",
                "register_type",
                "multiplier",
                "labels",
            },
            metric_context,
            optional={"states"},
        )
        name = raw_metric["name"]
        help_text = raw_metric["help"]
        block_name = raw_metric["block"]
        register_type = raw_metric["register_type"]
        if not isinstance(name, str) or not METRIC_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"{metric_context}.name is not a valid Prometheus name")
        if not name.startswith("sigenergy_"):
            raise ValueError(f"{metric_context}.name must start with sigenergy_")
        if name.endswith(("_total", "_count", "_sum", "_bucket")):
            raise ValueError(
                f"{metric_context}.name uses a suffix reserved for non-gauge metrics"
            )
        if name in COLLECTOR_METRIC_NAMES:
            raise ValueError(f"{metric_context}.name conflicts with a collector metric")
        if not isinstance(help_text, str) or not help_text.strip():
            raise ValueError(f"{metric_context}.help must not be empty")
        if not isinstance(block_name, str) or block_name not in blocks_by_name:
            raise ValueError(f"{metric_context}.block references an unknown block")
        if not isinstance(register_type, str) or register_type not in TYPE_WIDTHS:
            raise ValueError(
                f"{metric_context}.register_type must be one of {sorted(TYPE_WIDTHS)}"
            )
        offset = _require_int(raw_metric["offset"], f"{metric_context}.offset")
        multiplier = _require_float(
            raw_metric["multiplier"], f"{metric_context}.multiplier"
        )
        if multiplier == 0:
            raise ValueError(f"{metric_context}.multiplier must not be zero")
        labels = _parse_labels(raw_metric["labels"], f"{metric_context}.labels")
        states = _parse_states(
            raw_metric.get("states"),
            f"{metric_context}.states",
            register_type,  # type: ignore[arg-type]
        )
        if offset < 0:
            raise ValueError(f"{metric_context}.offset must not be negative")
        if offset + TYPE_WIDTHS[register_type] > blocks_by_name[block_name].count:
            raise ValueError(f"{metric_context} extends beyond block {block_name}")

        if states is not None and multiplier != 1:
            raise ValueError(f"{metric_context}.multiplier must be 1 for state metrics")

        label_names = tuple(label for label, _value in labels)
        label_values = tuple(value for _label, value in labels)
        if states is not None:
            if states.label in label_names:
                raise ValueError(
                    f"{metric_context}.states.label conflicts with a static label"
                )
            emitted_label_names = (*label_names, states.label)
            state_names: tuple[str, ...] | None = (*states.names, "unknown")
        else:
            emitted_label_names = label_names
            state_names = None
        contract = (help_text, emitted_label_names, state_names)
        previous_contract = family_contracts.setdefault(name, contract)
        if previous_contract != contract:
            raise ValueError(
                f"Metric family {name} must use one HELP string and label schema"
            )
        emitted_state_names = state_names or (None,)
        for state_name in emitted_state_names:
            emitted_label_values = (
                (*label_values, state_name) if state_name is not None else label_values
            )
            sample_key = (name, emitted_label_values)
            if sample_key in family_samples:
                raise ValueError(
                    f"Duplicate metric sample in {module_name}: {name}{labels}"
                )
            family_samples.add(sample_key)

        metrics.append(
            MetricSpec(
                name=name,
                help=help_text,
                block=block_name,
                offset=offset,
                register_type=register_type,  # type: ignore[arg-type]
                multiplier=multiplier,
                labels=labels,
                states=states,
            )
        )
        blocks_with_metrics.add(block_name)

    empty_blocks = block_names - blocks_with_metrics
    if empty_blocks:
        raise ValueError(
            f"Register blocks without metrics in {module_name}: {sorted(empty_blocks)}"
        )

    return ModuleConfig(
        unit_id=unit_id,
        timeout_seconds=timeout_seconds,
        request_gap_seconds=request_gap_seconds,
        blocks=tuple(blocks),
        metrics=tuple(metrics),
    )


def parse_config_document(document: object) -> ExporterConfig:
    if not isinstance(document, Mapping):
        raise ValueError("Sigenergy configuration must be a YAML mapping")
    _expect_keys(document, {"version", "default_module", "modules"}, "configuration")
    version = _require_int(document["version"], "configuration.version")
    if version != 1:
        raise ValueError("Sigenergy configuration version must be 1")

    default_module = document["default_module"]
    if not isinstance(default_module, str) or not IDENTIFIER_PATTERN.fullmatch(
        default_module
    ):
        raise ValueError("configuration.default_module is invalid")
    raw_modules = document["modules"]
    if not isinstance(raw_modules, Mapping) or not raw_modules:
        raise ValueError("configuration.modules must be a non-empty mapping")

    modules: dict[str, ModuleConfig] = {}
    for name, raw_module in raw_modules.items():
        if not isinstance(name, str) or not IDENTIFIER_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid module name: {name!r}")
        modules[name] = _parse_module(name, raw_module)
    if default_module not in modules:
        raise ValueError("configuration.default_module must reference a module")
    return ExporterConfig(
        default_module=default_module,
        modules=MappingProxyType(modules),
    )


def load_exporter_config(path: str | Path) -> ExporterConfig:
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read Sigenergy configuration {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Invalid YAML in Sigenergy configuration {path}: {exc}"
        ) from exc
    return parse_config_document(document)


class ConfigManager:
    """Atomically reload a validated configuration."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._config = load_exporter_config(self.path)

    def snapshot(self) -> ExporterConfig:
        with self._lock:
            return self._config

    def module(self, name: str | None) -> tuple[str, ModuleConfig]:
        with self._lock:
            selected = name or self._config.default_module
            try:
                return selected, self._config.modules[selected]
            except KeyError as exc:
                raise UnknownModuleError(
                    f"Unknown Sigenergy module: {selected}"
                ) from exc

    def module_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._config.modules))

    def default_module(self) -> str:
        with self._lock:
            return self._config.default_module

    def reload(self) -> None:
        new_config = load_exporter_config(self.path)
        with self._lock:
            self._config = new_config
