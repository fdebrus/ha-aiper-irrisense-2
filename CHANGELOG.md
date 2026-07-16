# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] — Detect all Irrisense serial families

### Fixed

- **Irrisense units with a `WRZ` (and other newer) serial prefix were not
  detected** — the config flow reported "No Irrisense devices found on this
  account" even though login succeeded. The device-list filter only accepted
  the exact `WRX` / `WGX` SKU prefixes. It now matches the 2-letter serial
  *family* (`WR` / `WG` / `WC` / `WL`), so new batch letters are covered
  automatically without needing a release. `get_devices` also logs (at debug)
  any serial it drops, so a genuinely-new family is diagnosable instead of
  vanishing silently.

## [0.4.0] — Async transport rewrite, typed errors, test harness + CI

> **Requires Home Assistant 2024.11 or newer.** The MQTT layer now uses
> paho-mqtt 2.x, which ships with HA 2024.11+.

### Changed

- **MQTT transport rewritten** — replaced the deprecated `AWSIoTPythonSDK` with
  **paho-mqtt 2.x over a SigV4-signed WebSocket**. The process-wide
  `threading.excepthook` crash shield is gone: paho 2.x reports socket teardown
  cleanly via `on_disconnect`, and a small reconnect supervisor re-signs a fresh
  SigV4 URL on each attempt. Publish topics / payloads / QoS are unchanged.
- **REST device-data calls migrated from `requests` to `aiohttp`** on Home
  Assistant's shared session. The auth / AWS-credential chain stays synchronous
  (the MQTT bootstrap threads need it that way); a single shared request builder
  keeps the encrypted wire bytes identical across the sync and async paths.
- **Minimum Home Assistant version raised to 2024.11** (for paho-mqtt 2.x).

### Added

- **Typed `InvalidAuth` / `CannotConnect` exceptions** replacing exception-message
  string matching. The config flow now surfaces `cannot_connect` distinctly from
  `invalid_auth`.
- **Test harness** (`pytest-homeassistant-custom-component`) with regression
  tests pinning the AES/RSA crypto envelope, the `const.py` dose/label presets,
  the coordinator's MQTT frame parsing (spike filter + duration latch), the async
  REST path, the AWS SigV4 signer, and the typed exceptions.
- **GitHub Actions CI** — hassfest, HACS validation (advisory), and pytest on a
  Python 3.11 + 3.13 matrix.
- **`advanced_diagnostics` option** gating the raw `debug_publish` service
  (off by default).

### Fixed

- MQTT publish logging is gated behind the existing `mqtt_debug` option instead
  of always logging at INFO on the start/stop path.
- `get_watering_history` caches the request body-shape the backend accepted per
  device instead of re-brute-forcing all four shapes on every refresh.

## [0.3.0] — Bug fixes, point-zone watchdog, robust setup

### Added

- **Point-zone overrun watchdog** (#6, #18 by @Patch76). HA-side stop at
  `point_time + 30s` grace when V3.8.7+ firmware mistracks point-zone
  duration. Auto-cancels on a clean device stop or a manual Stop.
- **Skip disabled devices** (#10, #14 by @Patch76). Devices disabled in HA's
  device registry are excluded from setup, MQTT subscribe, and coordinator
  refresh.
- **Integration icon** (#8 by @CtznSniiips).

### Changed

- **Bounded setup latency** (#11, #19 by @Patch76). Login and device discovery
  are wrapped in 15s timeouts that raise `ConfigEntryNotReady` /
  `ConfigEntryAuthFailed` for proper Home Assistant retry, and the MQTT
  connect moved to an entry-bound background task so a slow AWS IoT handshake
  can't push setup past HA's 60s bootstrap window.
- **Water totals now reported in gallons** (#22 by @tiloman). The backend
  reports gallons; the sensors were mislabelled as liters and Home Assistant
  converts for metric users. **Note:** existing history for the water-total
  sensors will shift to the corrected unit.

### Fixed

- **`binary_sensor.*_watering` stuck `off`** during active runs (#4, #15 by
  @Patch76). Now reads `is_running` from the coordinator's `active_zone_state()`
  rather than walking a non-existent nested MQTT `data` wrapper.
- **`water_pressure` permanently `unknown`** (#5, #16 by @Patch76). Removed the
  unreliable sensor and the `water_pressure_kpa` attribute — V3.8.7 firmware
  doesn't publish `waterpress` on progress frames and the fallback scan latched
  stale values from unrelated shadow frames.

## [0.2.2] — US region hostname fix + broader WGX coverage

### Fixed

- **US region login failed** with `Name does not resolve` for
  `apius.aiper.com`. Corrected hostname to `apiamerica.aiper.com` — the
  Aiper cloud's actual US REST endpoint (the EU and Asia endpoints were
  already correct and are unchanged).

### Changed

- Broadened the WGX serial-prefix handling started in 0.2.1 so the rest
  of the integration's user-facing surface no longer says "WRX only":
  - `IRRISENSE_SERIAL_PREFIXES` constant updated to `("WRX", "WGX")`.
  - Config-flow description and `no_devices` error message (English +
    translation) now reference both prefixes.
  - "No devices found" warning log and `NoIrrisenseDevices` docstring
    updated to match.

  `WRX` is the original / online-store SKU; `WGX` is the big-box-retail
  variant (e.g. Costco). Both speak the same wire protocol.

## [0.2.1] — WGX serial-prefix support

### Fixed

- Device-list filter rejected Irrisense units with a `WGX` serial
  prefix (sold via big-box retail) because it only matched `WRX`. The
  filter in `api.get_devices` now accepts both prefixes.
  (Thanks to [@n0k0m3](https://github.com/n0k0m3) — PR #1.)

## [0.2.0] — Initial public release

First public release. The integration has been iterated on privately; this
snapshot is the cleaned-up baseline from which future changes will be tracked.

### Features

- Cloud-polled control of Aiper Irrisense 2 devices via MQTT over AWS IoT.
- Per-device entities: active zone, progress %, coverage passes, elapsed /
  remaining seconds, water pressure, rain-sensing state, firmware versions,
  Wi-Fi signal, lifetime water totals.
- Start / stop watering buttons plus a shape-shifting Dose / Duration select
  that adapts to the currently-selected zone's region type (Area / Line /
  Point).
- Progress-spike filter in the coordinator to suppress transient 0→100→low
  blips from the device's `realTimeProgress` stream.
- Three example Lovelace dashboards (single-device, dual-device, and a
  side-by-side alternative) under `examples/`.
