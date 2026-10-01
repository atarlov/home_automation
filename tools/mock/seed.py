"""Seed SQLite so arrival / empty-house / comfort / never_switch_off scenarios work."""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import db  # noqa: E402

from .constants import MOCK_BIND, MOCK_PORT, PHONE_MAC


def shelly_http_target(device_id: str) -> str:
    """Path-qualified host so shelly.py keeps using http://{ip}/rpc/... unchanged."""
    return f"{MOCK_BIND}:{MOCK_PORT}/shelly/{device_id}"


def seed_lab_db(absence_min: int | None = None) -> Path:
    """Create people, presence phone, comfort hall, fridge, heater. Returns DB path."""
    db.load_env()
    absence = absence_min if absence_min is not None else int(os.environ.get("ARRIVAL_ABSENCE_MIN", 20))
    db.init_db()
    away = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=absence + 5)).strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        c.execute("DELETE FROM decisions")
        c.execute("DELETE FROM proposals")
        c.execute("DELETE FROM triage")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM audit")
        c.execute("DELETE FROM memory_notes")
        c.execute("DELETE FROM traffic_hourly")
        c.execute("DELETE FROM baselines")
        c.execute("DELETE FROM shelly")
        c.execute("DELETE FROM devices")
        c.execute("DELETE FROM people")
        c.execute("INSERT INTO people(id, name) VALUES(1, 'Assen')")
        c.execute(
            "INSERT INTO devices(mac, friendly_name, vendor, hostname, vlan, expected_vlan, "
            "first_seen, last_seen, online, last_ap, trusted, person_id, presence_device, "
            "last_tx_bytes, last_rx_bytes) VALUES(?,?,?,?,?,?,?,?,0,?,1,1,1,0,0)",
            (
                PHONE_MAC,
                "Assen phone",
                "Apple",
                "assen-phone",
                "LAN",
                "LAN",
                away,
                away,
                "00:11:22:33:44:55",
            ),
        )
        c.execute(
            "INSERT INTO shelly(device_id, name, ip, kind, on_state, power_w, idle_w, "
            "never_switch_off, comfort_auto, updated) VALUES(?,?,?,?,0,0,1,0,1,?)",
            ("hall", "Hall light", shelly_http_target("hall"), "switch", db.now_iso()),
        )
        c.execute(
            "INSERT INTO shelly(device_id, name, ip, kind, on_state, power_w, idle_w, "
            "never_switch_off, comfort_auto, updated) VALUES(?,?,?,?,1,90,40,1,0,?)",
            ("fridge", "Fridge", shelly_http_target("fridge"), "switch", db.now_iso()),
        )
        c.execute(
            "INSERT INTO shelly(device_id, name, ip, kind, on_state, power_w, idle_w, "
            "never_switch_off, comfort_auto, updated) VALUES(?,?,?,?,0,0,1,0,0,?)",
            ("heater", "Heater", shelly_http_target("heater"), "switch", db.now_iso()),
        )
    return Path(db.db_path())
