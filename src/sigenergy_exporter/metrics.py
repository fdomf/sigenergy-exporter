"""Exporter-process metrics, exposed only from /metrics."""

from prometheus_client import Counter, Gauge, Info

from sigenergy_exporter import __version__

CONFIG_LAST_RELOAD_SUCCESSFUL = Gauge(
    "sigenergy_config_last_reload_successful",
    "Whether the last Sigenergy exporter configuration reload succeeded.",
)
CONFIG_LAST_RELOAD_SUCCESS_TIMESTAMP = Gauge(
    "sigenergy_config_last_reload_success_timestamp_seconds",
    "Unix timestamp of the last successful Sigenergy configuration reload.",
)
COLLECTION_INVALID_REQUESTS = Counter(
    "sigenergy_collection_invalid_requests_total",
    "Invalid requests made to the Sigenergy collection endpoint.",
)
COLLECTION_UNKNOWN_MODULES = Counter(
    "sigenergy_collection_unknown_modules_total",
    "Sigenergy collection requests for modules absent from the configuration.",
)
COLLECTION_REJECTIONS = Counter(
    "sigenergy_collection_rejections_total",
    "Sigenergy collections rejected because the concurrency limit was reached.",
)
COLLECTION_INTERNAL_ERRORS = Counter(
    "sigenergy_collection_internal_errors_total",
    "Unhandled internal errors while serving Sigenergy collections.",
)
MODBUS_REQUESTS = Counter(
    "sigenergy_modbus_requests_total",
    "Modbus register-block requests made by the Sigenergy exporter.",
    ["module", "block"],
)
MODBUS_REQUEST_ERRORS = Counter(
    "sigenergy_modbus_request_errors_total",
    "Failed Modbus register-block requests made by the Sigenergy exporter.",
    ["module", "block"],
)
COLLECTIONS_IN_FLIGHT = Gauge(
    "sigenergy_collections_in_flight",
    "Sigenergy target collections currently in flight.",
)
BUILD_INFO = Info(
    "sigenergy_exporter_build",
    "Build information for the Sigenergy exporter.",
)
BUILD_INFO.info({"version": __version__})
