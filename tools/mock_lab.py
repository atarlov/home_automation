#!/usr/bin/env python3
"""Start the local Netwatch mock lab (no UniFi console, Shelly, or physical board).

Usage:
  python3 tools/mock_lab.py
  make mock

Opens a fake UniFi + Shelly HTTP server and a LilyGO T-Display-S3 AMOLED
web simulator (240×536). Mosquitto is optional — only needed for a live
MQTT demo (status/proposal/decision). Unit tests never need it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "host"))

from dotenv import load_dotenv

from mock.constants import MCU, MOCK_BIND, MOCK_PORT, PANEL_H, PANEL_W, PRODUCT_NAME
from mock.seed import seed_lab_db, shelly_http_target
from mock.server import MockLab


def _ensure_env():
    example = ROOT / ".env.example"
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(example)
        print(f"No .env found — loaded defaults from {example.name}", flush=True)

    # Always aim the host clients at this process's fake UniFi/Shelly HTTP.
    os.environ["UNIFI_URL"] = f"http://{MOCK_BIND}:{MOCK_PORT}"
    if not os.environ.get("UNIFI_READ_KEY"):
        os.environ["UNIFI_READ_KEY"] = "mock-read"
    if not os.environ.get("UNIFI_WRITE_KEY"):
        os.environ["UNIFI_WRITE_KEY"] = "mock-write"
    # Prefer an isolated mock DB so a real netwatch.db is not clobbered.
    if os.environ.get("NETWATCH_DB") in (None, "", "./netwatch.db", "netwatch.db"):
        os.environ["NETWATCH_DB"] = str(ROOT / "mock_netwatch.db")
    os.environ.setdefault("NETWATCH_HMAC_SECRET", "dev-hmac-secret")
    os.environ.setdefault("MQTT_HOST", "127.0.0.1")
    os.environ.setdefault("MQTT_PORT", "1883")
    os.environ.setdefault("MQTT_USER_ESP32", "esp32")
    os.environ.setdefault("MQTT_PASS_ESP32", "dev-esp32-pass")
    os.environ.setdefault("ARRIVAL_ABSENCE_MIN", "20")


def print_banner(mqtt_ok: bool, mqtt_error: str | None, db_path: Path):
    base = f"http://{MOCK_BIND}:{MOCK_PORT}"
    print("", flush=True)
    print("=== Netwatch mock lab ===", flush=True)
    print(f"Display : {PRODUCT_NAME} ({MCU}), panel {PANEL_W}×{PANEL_H}", flush=True)
    print(f"Simulator: {base}/display/", flush=True)
    print(f"Fake UniFi: {os.environ['UNIFI_URL']}", flush=True)
    print(f"Fake Shelly: http://{shelly_http_target('<id>')}/rpc/Switch.GetStatus", flush=True)
    print(f"DB       : {db_path}", flush=True)
    print("", flush=True)
    print("Scenarios (also buttons on the display page):", flush=True)
    print(f"  curl -X POST {base}/mock/scenario/arrive", flush=True)
    print(f"  curl -X POST {base}/mock/scenario/unknown", flush=True)
    print(f"  curl -X POST {base}/mock/scenario/empty_heater", flush=True)
    print(f"  curl -X POST {base}/mock/scenario/reset", flush=True)
    print("", flush=True)
    if mqtt_ok:
        print("MQTT     : connected — display follows netwatch/* ; touch buttons publish decisions.", flush=True)
    else:
        print("MQTT     : optional / not connected.", flush=True)
        if mqtt_error:
            print(f"           ({mqtt_error})", flush=True)
        print("           For a live demo: docker compose up -d   then re-run make mock", flush=True)
        print("           Collector/events still work without a broker.", flush=True)
    print("", flush=True)
    print("Next (separate terminals). Export these so collector hits the fake gear:", flush=True)
    print(f"  export UNIFI_URL={os.environ['UNIFI_URL']}", flush=True)
    print(f"  export UNIFI_READ_KEY={os.environ.get('UNIFI_READ_KEY', 'mock-read')}", flush=True)
    print(f"  export NETWATCH_DB={db_path}", flush=True)
    print("  python3 host/collector.py", flush=True)
    print("  python3 host/api.py          # needs broker for publish", flush=True)
    print("  python3 host/gateway.py      # needs broker", flush=True)
    print("", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=MOCK_BIND)
    parser.add_argument("--port", type=int, default=MOCK_PORT)
    parser.add_argument("--no-seed", action="store_true", help="do not reset/seed the mock DB")
    parser.add_argument("--no-mqtt", action="store_true", help="skip MQTT bridge entirely")
    args = parser.parse_args(argv)

    _ensure_env()
    db_path = Path(os.environ["NETWATCH_DB"])
    if not args.no_seed:
        db_path = seed_lab_db()

    lab = MockLab()
    mqtt_ok = False
    mqtt_error = None
    if not args.no_mqtt:
        lab.start_mqtt_bridge()
        mqtt_ok = lab.mqtt_ok
        mqtt_error = lab.mqtt_error
    else:
        lab.mqtt_error = "disabled via --no-mqtt"

    print_banner(mqtt_ok, mqtt_error or lab.mqtt_error, db_path)
    try:
        lab.serve_forever(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nmock lab stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
