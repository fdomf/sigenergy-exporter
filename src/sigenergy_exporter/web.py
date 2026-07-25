"""HTTP application for the multi-target Sigenergy exporter."""

from __future__ import annotations

import html
import logging
import math
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.exposition import choose_encoder

from sigenergy_exporter.collector import (
    ModbusClient,
    SigenergyCollector,
    TargetCoordinator,
    parse_target,
)
from sigenergy_exporter.config import ConfigManager, UnknownModuleError
from sigenergy_exporter.metrics import (
    COLLECTION_INTERNAL_ERRORS,
    COLLECTION_INVALID_REQUESTS,
    COLLECTION_REJECTIONS,
    COLLECTION_UNKNOWN_MODULES,
    COLLECTIONS_IN_FLIGHT,
    CONFIG_LAST_RELOAD_SUCCESSFUL,
    CONFIG_LAST_RELOAD_SUCCESS_TIMESTAMP,
)

LOG = logging.getLogger("sigenergy_exporter")
SCRAPE_TIMEOUT_OFFSET_SECONDS = 0.5


class CollectionBusyError(RuntimeError):
    pass


def parse_scrape_timeout(value: str | None) -> float | None:
    """Reserve response time from Prometheus' advertised scrape deadline."""
    if value is None:
        return None
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("X-Prometheus-Scrape-Timeout-Seconds must be numeric") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            "X-Prometheus-Scrape-Timeout-Seconds must be finite and greater than 0"
        )
    return max(0.0, timeout - SCRAPE_TIMEOUT_OFFSET_SECONDS)


class ExporterApplication:
    def __init__(
        self,
        config: ConfigManager,
        max_concurrency: int,
        *,
        coordinator: TargetCoordinator | None = None,
        client_factory: Callable[..., ModbusClient] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("web max concurrency must be at least 1")
        self.config = config
        self.coordinator = coordinator or TargetCoordinator()
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._client_factory = client_factory
        CONFIG_LAST_RELOAD_SUCCESSFUL.set(1)
        CONFIG_LAST_RELOAD_SUCCESS_TIMESTAMP.set_to_current_time()

    def scrape(
        self,
        query: str,
        accept_header: str | None,
        scrape_timeout_header: str | None = None,
    ) -> tuple[bytes, str]:
        params = parse_qs(query, keep_blank_values=True)
        unknown = set(params) - {"target", "module"}
        if unknown:
            raise ValueError(f"Unknown query parameters: {sorted(unknown)}")
        targets = params.get("target", [])
        modules = params.get("module", [])
        if len(targets) != 1 or not targets[0]:
            raise ValueError("'target' parameter must be specified once")
        if len(modules) > 1 or (modules and not modules[0]):
            raise ValueError("'module' parameter must be specified at most once")

        target = parse_target(targets[0])
        module_name, module = self.config.module(modules[0] if modules else None)
        scrape_timeout_seconds = parse_scrape_timeout(scrape_timeout_header)
        if not self._semaphore.acquire(blocking=False):
            raise CollectionBusyError("maximum concurrent collections reached")
        COLLECTIONS_IN_FLIGHT.inc()
        try:
            registry = CollectorRegistry()
            collector_kwargs: dict[str, object] = {}
            if self._client_factory is not None:
                collector_kwargs["client_factory"] = self._client_factory
            registry.register(
                SigenergyCollector(
                    target,
                    module_name,
                    module,
                    self.coordinator,
                    scrape_timeout_seconds=scrape_timeout_seconds,
                    **collector_kwargs,
                )
            )
            encoder, content_type = choose_encoder(accept_header or "")
            return encoder(registry), content_type
        finally:
            COLLECTIONS_IN_FLIGHT.dec()
            self._semaphore.release()

    def exporter_metrics(self, accept_header: str | None) -> tuple[bytes, str]:
        encoder, content_type = choose_encoder(accept_header or "")
        return encoder(REGISTRY), content_type

    def reload(self) -> None:
        try:
            self.config.reload()
        except Exception:
            CONFIG_LAST_RELOAD_SUCCESSFUL.set(0)
            raise
        CONFIG_LAST_RELOAD_SUCCESSFUL.set(1)
        CONFIG_LAST_RELOAD_SUCCESS_TIMESTAMP.set_to_current_time()

    def landing_page(self) -> bytes:
        default_module = html.escape(self.config.default_module(), quote=True)
        modules = ", ".join(map(html.escape, self.config.module_names()))
        return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Sigenergy Exporter</title></head>
<body>
<h1>Sigenergy Exporter</h1>
<p>Read-only Prometheus exporter for Sigenergy Modbus TCP targets.</p>
<form action="/sigenergy" method="get">
<label>Target <input name="target" placeholder="192.0.2.10"></label>
<label>Module <input name="module" value="{default_module}"></label>
<button type="submit">Collect</button>
</form>
<p>Configured modules: {modules}</p>
<p><a href="/metrics">Exporter metrics</a></p>
</body>
</html>
""".encode()


def make_http_handler(
    application: ExporterApplication,
) -> type[BaseHTTPRequestHandler]:
    class ExporterHandler(BaseHTTPRequestHandler):
        server_version = "sigenergy-exporter"

        def _write(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str = "text/plain; charset=utf-8",
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                LOG.debug("Client disconnected while response was being written")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/metrics":
                body, content_type = application.exporter_metrics(
                    self.headers.get("Accept")
                )
                self._write(HTTPStatus.OK, body, content_type)
                return
            if parsed.path == "/-/healthy":
                self._write(HTTPStatus.OK, b"Healthy\n")
                return
            if parsed.path == "/sigenergy":
                try:
                    body, content_type = application.scrape(
                        parsed.query,
                        self.headers.get("Accept"),
                        self.headers.get("X-Prometheus-Scrape-Timeout-Seconds"),
                    )
                except UnknownModuleError as exc:
                    COLLECTION_UNKNOWN_MODULES.inc()
                    self._write(HTTPStatus.BAD_REQUEST, f"{exc}\n".encode())
                    return
                except ValueError as exc:
                    COLLECTION_INVALID_REQUESTS.inc()
                    self._write(HTTPStatus.BAD_REQUEST, f"{exc}\n".encode())
                    return
                except CollectionBusyError as exc:
                    COLLECTION_REJECTIONS.inc()
                    self._write(HTTPStatus.SERVICE_UNAVAILABLE, f"{exc}\n".encode())
                    return
                except Exception:
                    COLLECTION_INTERNAL_ERRORS.inc()
                    LOG.exception("Unhandled Sigenergy collection error")
                    self._write(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        b"internal collection error\n",
                    )
                    return
                self._write(HTTPStatus.OK, body, content_type)
                return
            if parsed.path == "/":
                self._write(
                    HTTPStatus.OK,
                    application.landing_page(),
                    "text/html; charset=utf-8",
                )
                return
            self._write(HTTPStatus.NOT_FOUND, b"not found\n")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/-/reload":
                self._write(HTTPStatus.NOT_FOUND, b"not found\n")
                return
            try:
                application.reload()
            except Exception as exc:
                LOG.exception("Configuration reload failed")
                self._write(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"configuration reload failed: {exc}\n".encode(),
                )
                return
            LOG.info("Configuration reloaded")
            self._write(HTTPStatus.OK, b"configuration reloaded\n")

        def log_message(self, format: str, *args: object) -> None:
            LOG.debug("%s - %s", self.client_address[0], format % args)

    return ExporterHandler
