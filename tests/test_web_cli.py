from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml
from prometheus_client import REGISTRY

from sigenergy_exporter.cli import main, parse_listen_address
from sigenergy_exporter.config import ConfigManager
from sigenergy_exporter.web import (
    CollectionBusyError,
    ExporterApplication,
    make_http_handler,
    parse_scrape_timeout,
)
from tests.helpers import ClientFactory, REPOSITORY_CONFIG, valid_config_document


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "sigenergy.yml"
        self.path.write_text(
            yaml.safe_dump(valid_config_document()),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_module_defaults_and_openmetrics_is_negotiated(self) -> None:
        app = ExporterApplication(
            ConfigManager(self.path),
            1,
            client_factory=ClientFactory(),
        )
        body, content_type = app.scrape(
            "target=192.0.2.1",
            "application/openmetrics-text; version=1.0.0",
        )
        self.assertIn("application/openmetrics-text", content_type)
        self.assertTrue(body.endswith(b"# EOF\n"))
        text = body.decode()
        self.assertIn("sigenergy_up 1.0", text)
        self.assertNotIn("process_", text)
        self.assertNotIn("target=", text)

    def test_unknown_or_duplicate_parameters_are_rejected(self) -> None:
        app = ExporterApplication(ConfigManager(self.path), 1)
        for query in (
            "",
            "target=",
            "target=a&target=b",
            "target=a&module=test_module&module=test_module",
            "target=a&unknown=x",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                app.scrape(query, None)

    def test_scrape_timeout_header_is_validated_and_propagated(self) -> None:
        factory = ClientFactory()
        app = ExporterApplication(
            ConfigManager(self.path),
            1,
            client_factory=factory,
        )
        body, _content_type = app.scrape(
            "target=192.0.2.1",
            None,
            "3",
        )
        self.assertIn(b"sigenergy_up 0.0", body)
        self.assertEqual(factory.clients, [])
        for value in ("", "0", "nan", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_scrape_timeout(value)
        self.assertEqual(parse_scrape_timeout("4"), 3.5)

    def test_global_concurrency_limit_fails_fast(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def block(_address: int) -> None:
            entered.set()
            release.wait(2)

        app = ExporterApplication(
            ConfigManager(self.path),
            1,
            client_factory=ClientFactory(on_read=block),
        )
        result: list[object] = []

        def first_scrape() -> None:
            result.append(app.scrape("target=192.0.2.1", None))

        thread = threading.Thread(target=first_scrape)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(CollectionBusyError):
            app.scrape("target=192.0.2.2", None)
        release.set()
        thread.join(3)
        self.assertEqual(len(result), 1)


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "sigenergy.yml"
        path.write_text(yaml.safe_dump(valid_config_document()), encoding="utf-8")
        app = ExporterApplication(
            ConfigManager(path),
            2,
            client_factory=ClientFactory(),
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_http_handler(app),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.directory.cleanup()

    def test_landing_health_metrics_scrape_and_reload(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"<form", response.read())
        with urllib.request.urlopen(
            f"{self.base_url}/-/healthy", timeout=2
        ) as response:
            self.assertEqual(response.read(), b"Healthy\n")
        with urllib.request.urlopen(f"{self.base_url}/metrics", timeout=2) as response:
            metrics = response.read()
            self.assertIn(b"sigenergy_exporter_build_info", metrics)
            self.assertNotIn(b"# HELP sigenergy_up", metrics)
        with urllib.request.urlopen(
            f"{self.base_url}/sigenergy?target=192.0.2.1",
            timeout=4,
        ) as response:
            target_metrics = response.read()
            self.assertIn(b"sigenergy_up 1.0", target_metrics)
            self.assertNotIn(b"sigenergy_exporter_build_info", target_metrics)
        request = urllib.request.Request(
            f"{self.base_url}/-/reload",
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)

    def test_bad_request_and_not_found_statuses(self) -> None:
        invalid_before = REGISTRY.get_sample_value(
            "sigenergy_collection_invalid_requests_total"
        )
        unknown_before = REGISTRY.get_sample_value(
            "sigenergy_collection_unknown_modules_total"
        )
        for url, expected in (
            (f"{self.base_url}/sigenergy", 400),
            (
                f"{self.base_url}/sigenergy?target=192.0.2.1&module=missing",
                400,
            ),
            (f"{self.base_url}/missing", 404),
        ):
            with (
                self.subTest(url=url),
                self.assertRaises(urllib.error.HTTPError) as raised,
            ):
                urllib.request.urlopen(url, timeout=2)
            self.assertEqual(raised.exception.code, expected)
        self.assertEqual(
            REGISTRY.get_sample_value("sigenergy_collection_invalid_requests_total"),
            invalid_before + 1,
        )
        self.assertEqual(
            REGISTRY.get_sample_value("sigenergy_collection_unknown_modules_total"),
            unknown_before + 1,
        )


class CliTests(unittest.TestCase):
    def test_listen_address_parsing(self) -> None:
        self.assertEqual(parse_listen_address(":10047"), ("0.0.0.0", 10047))
        self.assertEqual(parse_listen_address("[::1]:10047"), ("::1", 10047))
        with self.assertRaises(ValueError):
            parse_listen_address("localhost")

    def test_dry_run_validates_repository_configuration(self) -> None:
        result = main(
            [
                f"--config.file={REPOSITORY_CONFIG}",
                "--web.listen-address=127.0.0.1:10047",
                "--dry-run",
            ]
        )
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
