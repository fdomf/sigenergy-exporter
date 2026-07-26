# Sigenergy Prometheus exporter

A multi-target Prometheus exporter for Sigenergy plants and inverters over
Modbus TCP. Prometheus supplies the target address at scrape time and the
exporter synchronously reads the selected protocol module.

The bundled `sigenstor_plant_v2_5` module covers plant and ESS running information
from Sigenergy Modbus Protocol V2.5 (2025-02-19), using plant unit ID `247`.
The bundled `sigenstor_inverter_v2_5` module covers hybrid-inverter, battery,
grid, and PV-string running information using inverter unit ID `1`. Neither
module calls a Modbus write function.

## Run

Python 3.12 or newer is required.

```console
python -m venv .venv
.venv/bin/pip install --constraint constraints.txt .
.venv/bin/sigenergy-exporter --config.file=sigenergy.yml
```

Run the published non-root container:

```console
docker run --rm -p 10047:10047 ghcr.io/fdomf/sigenergy-exporter:0.2.0
```

Or build it locally:

```console
docker build --target runtime -t sigenergy-exporter .
docker run --rm -p 10047:10047 sigenergy-exporter
```

Docker Compose can use the published image without cloning or building this
repository:

```yaml
services:
  sigenergy-exporter:
    image: ghcr.io/fdomf/sigenergy-exporter:0.2.0
    restart: unless-stopped
    ports:
      - "10047:10047"
```

Other services in the same Compose project can reach the exporter at
`http://sigenergy-exporter:10047`.

To maintain a customized module configuration outside the image, add:

```yaml
    volumes:
      - ./sigenergy.yml:/etc/sigenergy-exporter/sigenergy.yml:ro
```

Validate configuration without starting the server:

```console
sigenergy-exporter --config.file=sigenergy.yml --dry-run
```

## Prometheus configuration

The `module` parameter is optional and defaults to `sigenstor_plant_v2_5`.

```yaml
scrape_configs:
  - job_name: sigenergy
    scrape_interval: 15s
    scrape_timeout: 14s
    metrics_path: /sigenergy
    static_configs:
      - targets:
          - 192.0.2.10
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: sigenergy-exporter:10047

  - job_name: sigenergy-inverter
    scrape_interval: 15s
    scrape_timeout: 14s
    metrics_path: /sigenergy
    params:
      module: [sigenstor_inverter_v2_5]
    static_configs:
      - targets:
          - 192.0.2.10
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: sigenergy-exporter:10047

  - job_name: sigenergy-exporter
    static_configs:
      - targets: [sigenergy-exporter:10047]
```

Targets may be a DNS name, IPv4 address, `host:port`, or bracketed IPv6
address. Port `502` is used by default.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `/sigenergy?target=HOST[:PORT]&module=MODULE` | Collect a plant |
| `/metrics` | Exporter and process metrics |
| `/-/healthy` | Process health |
| `/-/reload` | Atomically reload configuration with `POST` |
| `/` | Landing page and manual collection form |

A required Modbus read failure returns HTTP 200 with `sigenergy_up 0`.
Metrics from failed blocks are omitted, while successfully collected blocks
remain available. Exporter HTTP and process metrics are intentionally exposed
only on `/metrics`. The exporter honors Prometheus'
`X-Prometheus-Scrape-Timeout-Seconds` header and reserves 500 ms to encode and
return the response; a block that cannot fit before that deadline is omitted.

Target metrics use Prometheus base-unit conventions:

- state of charge and health are ratios from `0` to `1`;
- energy and capacity use joules;
- active power uses watts and reactive power uses vars;
- electrical measurements use volts, amperes, hertz, and ohms;
- phase measurements use the bounded `phase="a|b|c"` label;
- operating modes and plant state are one-hot gauge families with a bounded
  `mode` or `state` label, including an `unknown` value for undocumented codes.

Exporter instrumentation on `/metrics` includes Modbus connection attempts and
failures, register-block requests and failures, collection concurrency, and
scrape deadline exhaustion. Deadline stages use the bounded values `queue`,
`connect`, and `pacing`; target addresses are never used as metric labels.

## Configuration

`sigenergy.yml` contains reusable protocol modules, not targets. Each module
defines its Modbus unit, read-only function code, timeout, minimum request
period, read blocks, and metric decoding. Function codes `3` and `4` are
supported; no write function code is accepted. The exporter strictly validates
names, types, block bounds, scales, state mappings, static label schemas, and
duplicate samples before accepting a file.
A failed reload leaves the last valid configuration active.

Supported register types are `u16`, `s16`, `u32`, `s32`, `u64`, and `s64`.
Multi-register values are decoded high-word first. The bundled plant module
reads:

- required block `30003-30072`;
- optional ESS detail block `30083-30087`.

The bundled inverter module reads required blocks `30540-30623` and
`31000-31041`. It defaults to inverter unit ID `1`; installations that assign a
different inverter address from `1` through `246` should mount a copy of
`sigenergy.yml` with the module's `unit_id` changed.

Opaque alarm bitfields, reserved registers, and identity strings are
deliberately not exported. AC/DC charger modules remain outside the current
scope.

Reload after editing the mounted file:

```console
curl -fsS -X POST http://127.0.0.1:10047/-/reload
docker kill --signal HUP sigenergy-exporter
```

## Safety

Sigenergy V2.5 specifies a minimum 1000 ms request period. The exporter
serializes collections per target and preserves each target's last request
time across scrapes, while still allowing different plants to be collected
concurrently.

The collection endpoint can connect to a host supplied in the query string.
Run it only on a trusted network and avoid publishing port `10047` to the
public internet.

## Compatibility

The bundled profile implements Sigenergy Modbus Protocol V2.5 dated
2025-02-19:

| Profile | Modbus function | Unit ID | Register ranges | Automated validation |
| --- | --- | ---: | --- | --- |
| `sigenstor_plant_v2_5` | FC04 input registers | 247 | `30003-30072`, `30083-30087` | Decoding, scaling, pacing, failures, exposition |
| `sigenstor_inverter_v2_5` | FC03 read-only registers | 1 (configurable) | `30540-30623`, `31000-31041` | Function code, unit ID, decoding, scaling, states, pacing, exposition |

No specific SigenStor model and firmware combination is claimed as publicly
validated yet.

Published container tags are multi-platform images for `linux/amd64` and
`linux/arm64`.

## Development

```console
python -m pip install --constraint constraints.txt ".[dev]"
ruff check src tests
ruff format --check src tests
python -m unittest discover -v tests
docker build --target test -t sigenergy-exporter:test .
docker run --rm sigenergy-exporter:test
```

The register facts are derived from
[Sigenergy Modbus Protocol V2.5](https://b2b.aprilice.com/se_fi/mpattachment/file/download/id/778/).
The protocol PDF is copyrighted by Sigenergy and is not redistributed here.

## Maintainer

Maintained by [Francesc Domene](https://github.com/fdomf).

Licensed under the Apache License 2.0.
