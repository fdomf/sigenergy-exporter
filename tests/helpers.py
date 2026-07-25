from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from sigenergy_exporter.config import ModuleConfig, parse_config_document

REPOSITORY_CONFIG = Path(__file__).parents[1] / "sigenergy.yml"


def valid_config_document() -> dict[str, Any]:
    return {
        "version": 1,
        "default_module": "test_module",
        "modules": {
            "test_module": {
                "unit_id": 247,
                "timeout_seconds": 3,
                "request_gap_seconds": 1,
                "blocks": [
                    {
                        "name": "required",
                        "address": 30000,
                        "count": 4,
                        "required": True,
                    },
                    {
                        "name": "optional",
                        "address": 30010,
                        "count": 1,
                        "required": False,
                    },
                ],
                "metrics": [
                    {
                        "name": "sigenergy_test_phase_watts",
                        "help": "Test power from registers 30000 and 30002.",
                        "block": "required",
                        "offset": 0,
                        "register_type": "s32",
                        "multiplier": 1,
                        "labels": {"phase": "a"},
                    },
                    {
                        "name": "sigenergy_test_phase_watts",
                        "help": "Test power from registers 30000 and 30002.",
                        "block": "required",
                        "offset": 2,
                        "register_type": "s32",
                        "multiplier": 1,
                        "labels": {"phase": "b"},
                    },
                    {
                        "name": "sigenergy_test_ratio",
                        "help": "Test ratio from register 30010.",
                        "block": "optional",
                        "offset": 0,
                        "register_type": "u16",
                        "multiplier": 0.001,
                        "labels": {},
                    },
                ],
            }
        },
    }


def valid_module() -> ModuleConfig:
    return parse_config_document(valid_config_document()).modules["test_module"]


def copied_document() -> dict[str, Any]:
    return copy.deepcopy(valid_config_document())


class FakeResponse:
    def __init__(self, registers: list[int], *, error: bool = False) -> None:
        self.registers = registers
        self._error = error

    def isError(self) -> bool:  # noqa: N802
        return self._error


class FakeClient:
    def __init__(
        self,
        *,
        responses: dict[int, FakeResponse] | None = None,
        connect: bool = True,
        on_read: Any = None,
        **connection: object,
    ) -> None:
        self.connection = connection
        self.responses = responses or {}
        self.connect_result = connect
        self.on_read = on_read
        self.calls: list[tuple[int, int, int]] = []
        self.closed = False

    def connect(self) -> bool:
        return self.connect_result

    def close(self) -> None:
        self.closed = True

    def read_input_registers(
        self,
        address: int,
        *,
        count: int,
        device_id: int,
    ) -> FakeResponse:
        self.calls.append((address, count, device_id))
        if self.on_read is not None:
            self.on_read(address)
        return self.responses.get(address, FakeResponse([0] * count))


class ClientFactory:
    def __init__(
        self,
        responses: dict[int, FakeResponse] | None = None,
        *,
        connect: bool = True,
        on_read: Any = None,
    ) -> None:
        self.responses = responses
        self.connect = connect
        self.on_read = on_read
        self.clients: list[FakeClient] = []

    def __call__(self, **connection: object) -> FakeClient:
        client = FakeClient(
            responses=self.responses,
            connect=self.connect,
            on_read=self.on_read,
            **connection,
        )
        self.clients.append(client)
        return client
