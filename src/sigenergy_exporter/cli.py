"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from http.server import ThreadingHTTPServer

from sigenergy_exporter import __version__
from sigenergy_exporter.config import ConfigManager
from sigenergy_exporter.web import ExporterApplication, make_http_handler

LOG = logging.getLogger("sigenergy_exporter")
DEFAULT_CONFIG_FILE = "sigenergy.yml"
DEFAULT_LISTEN_ADDRESS = "0.0.0.0:10047"


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("web listen port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("web listen port must be between 1 and 65535")
    return port


def parse_listen_address(value: str) -> tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 2 or not value[closing + 1 :].startswith(":"):
            raise ValueError("web listen address must be HOST:PORT")
        host = value[1:closing]
        raw_port = value[closing + 2 :]
    else:
        if ":" not in value:
            raise ValueError("web listen address must be HOST:PORT")
        host, raw_port = value.rsplit(":", 1)
    if host == "":
        host = "0.0.0.0"
    return host, _parse_port(raw_port)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for Sigenergy Modbus TCP targets"
    )
    parser.add_argument(
        "--config.file",
        dest="config_file",
        default=DEFAULT_CONFIG_FILE,
        help="Path to the Sigenergy YAML configuration",
    )
    parser.add_argument(
        "--web.listen-address",
        dest="web_listen_address",
        default=DEFAULT_LISTEN_ADDRESS,
        help="Address on which to expose metrics",
    )
    parser.add_argument(
        "--web.max-concurrency",
        dest="web_max_concurrency",
        type=int,
        default=4,
        help="Maximum concurrent target collections",
    )
    parser.add_argument(
        "--log.level",
        dest="log_level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging level",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration and exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"sigenergy-exporter {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = ConfigManager(args.config_file)
        listen_address = parse_listen_address(args.web_listen_address)
        application = ExporterApplication(config, args.web_max_concurrency)
    except ValueError as exc:
        LOG.error("%s", exc)
        return 1

    if args.dry_run:
        LOG.info(
            "Configuration %s is valid; default_module=%s; modules=%s",
            args.config_file,
            config.default_module(),
            ",".join(config.module_names()),
        )
        return 0

    server = ThreadingHTTPServer(listen_address, make_http_handler(application))
    server.daemon_threads = True

    def request_shutdown(signum: int, _frame: object) -> None:
        LOG.info("Received signal %s; stopping", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    def request_reload(signum: int, _frame: object) -> None:
        try:
            application.reload()
        except Exception:
            LOG.exception("Configuration reload from signal %s failed", signum)
        else:
            LOG.info("Configuration reloaded from signal %s", signum)

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, request_reload)

    LOG.info(
        "Exporter listening on %s:%d; default_module=%s; modules=%s",
        listen_address[0],
        listen_address[1],
        config.default_module(),
        ",".join(config.module_names()),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
