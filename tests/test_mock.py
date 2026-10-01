"""Mock lab tests — no Docker, broker, or hardware required."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
sys.path.insert(0, str(ROOT / "tools"))

import collector  # noqa: E402
import db  # noqa: E402
from mock.constants import MCU, PANEL_H, PANEL_W, PRODUCT_NAME  # noqa: E402
from mock.display_state import DisplayReducer  # noqa: E402
from mock.seed import seed_lab_db, shelly_http_target  # noqa: E402
from mock.signing import sign_decision  # noqa: E402
from mock.sources import LabSources  # noqa: E402


class SigningTests(unittest.TestCase):
    def test_matches_fake_button_contract(self):
        body = sign_decision("prp_abc", "approve", "dev-hmac-secret", ts=1_700_000_000)
        expected = hmac.new(
            b"dev-hmac-secret",
            b"prp_abc|approve|1700000000",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(body["hmac"], expected)
        self.assertEqual(body["id"], "prp_abc")
        self.assertEqual(body["decision"], "approve")
        self.assertEqual(body["ts"], 1_700_000_000)

    def test_rejects_bad_decision(self):
        with self.assertRaises(ValueError):
            sign_decision("prp_1", "maybe", "secret")


class DisplayStateTests(unittest.TestCase):
    def test_panel_constants_from_lilygo_page(self):
        self.assertEqual(PANEL_W, 240)
        self.assertEqual(PANEL_H, 536)
        self.assertEqual(PRODUCT_NAME, "T-Display S3 AMOLED")
        self.assertEqual(MCU, "ESP32-S3R8")

    def test_offline_when_heartbeat_stale(self):
        r = DisplayReducer(heartbeat_stale_sec=30)
        r.apply("netwatch/status", {"lines": ["Quiet night"]}, now=1000)
        state = r.state(now=1000)
        self.assertEqual(state.mode, "offline")
        r.apply("netwatch/heartbeat", {"from": "gateway", "ts": 1000}, now=1000)
        idle = r.state(now=1010)
        self.assertEqual(idle.mode, "idle")
        self.assertEqual(idle.lines, ["Quiet night"])
        stale = r.state(now=1040)
        self.assertEqual(stale.mode, "offline")

    def test_proposal_buttons_and_resolve(self):
        r = DisplayReducer(heartbeat_stale_sec=60)
        r.apply("netwatch/heartbeat", {"ts": 5000}, now=5000)
        r.apply(
            "netwatch/proposal",
            {
                "id": "prp_1",
                "tier": "secure",
                "expires_at": 5120,
                "lines": ["Quarantine?", "esp-cam"],
                "button_a": "Block",
                "button_b": "Ignore",
            },
            now=5001,
        )
        s = r.state(now=5002)
        self.assertEqual(s.mode, "proposal")
        self.assertEqual(s.proposal_id, "prp_1")
        self.assertEqual(s.button_a, "Block")
        self.assertEqual(s.button_b, "Ignore")
        self.assertEqual(s.panel_w, 240)
        self.assertEqual(s.panel_h, 536)
        r.apply("netwatch/resolved", {"id": "prp_1", "status": "approve"}, now=5003)
        self.assertEqual(r.state(now=5004).mode, "idle")

    def test_notify_yields_to_proposal(self):
        r = DisplayReducer(heartbeat_stale_sec=60)
        r.apply("netwatch/heartbeat", {"ts": 1}, now=1)
        r.apply("netwatch/notify", {"event_id": "e1", "lines": ["Hi"], "button_a": "-", "button_b": "-"}, now=2)
        self.assertEqual(r.state(now=3).mode, "notify")
        r.apply(
            "netwatch/proposal",
            {"id": "prp_2", "lines": ["Off heater?"], "button_a": "Yes", "button_b": "No"},
            now=4,
        )
        self.assertEqual(r.state(now=5).mode, "proposal")


class ScenarioCollectorTests(unittest.TestCase):
    """Drive collector through LabSources the same way the fake HTTP server would."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["NETWATCH_DB"] = os.path.join(self.tmp.name, "mock.db")
        os.environ["ARRIVAL_ABSENCE_MIN"] = "20"
        os.environ["UNIFI_URL"] = "http://127.0.0.1:18765"
        os.environ["UNIFI_READ_KEY"] = "mock"
        seed_lab_db()
        self.sources = LabSources()

    def _events(self):
        with db.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts")]

    def test_arrival_unknown_and_empty_heater(self):
        # Phone arrives after seeded absence → arrival event
        self.sources.scenario_arrive()
        with patch.object(collector.unifi, "list_clients", side_effect=self.sources.list_clients):
            with db.conn() as c:
                collector.poll_unifi(c)
        kinds = [e["type"] for e in self._events()]
        self.assertIn("arrival", kinds)

        # Unknown client → new_client
        self.sources.scenario_unknown()
        with patch.object(collector.unifi, "list_clients", side_effect=self.sources.list_clients):
            with db.conn() as c:
                collector.poll_unifi(c)
        kinds = [e["type"] for e in self._events()]
        self.assertIn("new_client", kinds)

        # Empty house + heater 1.8 kW → shelly_power on heater only
        self.sources.scenario_empty_heater()
        import datetime as dt

        away = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        with db.conn() as c:
            c.execute(
                "UPDATE devices SET online=0, last_seen=? WHERE presence_device=1",
                (away,),
            )

        def shelly_status(ip, ch=0):
            # ip looks like 127.0.0.1:18765/shelly/heater
            device_id = str(ip).rstrip("/").split("/")[-1]
            body = self.sources.shelly_status(device_id)
            return {"on": body["output"], "power_w": body["apower"]}

        with patch.object(collector.shelly, "status", side_effect=shelly_status):
            with db.conn() as c:
                collector.poll_shelly(c)
        power = [e for e in self._events() if e["type"] == "shelly_power"]
        self.assertEqual(len(power), 1)
        self.assertEqual(power[0]["device_id"], "heater")
        details = json.loads(power[0]["details"])
        self.assertGreaterEqual(details["power_w"], 1800)

    def test_shelly_http_target_shape(self):
        target = shelly_http_target("heater")
        self.assertTrue(target.endswith("/shelly/heater"))
        self.assertIn(":", target)


class FakeHttpServerTests(unittest.TestCase):
    """host/unifi.py + host/shelly.py against the local fake HTTP server (no broker)."""

    def setUp(self):
        from mock.server import MockLab

        self.lab = MockLab()
        self.lab.start_background(host="127.0.0.1", port=18766)
        self.addCleanup(lambda: self.lab._httpd and self.lab._httpd.shutdown())
        os.environ["UNIFI_URL"] = "http://127.0.0.1:18766"
        os.environ["UNIFI_READ_KEY"] = "mock"
        os.environ["UNIFI_WRITE_KEY"] = "mock"

    def test_unifi_and_shelly_clients_unchanged(self):
        import shelly
        import unifi

        self.lab.sources.scenario_arrive()
        clients = unifi.list_clients()
        self.assertEqual([c["mac"] for c in clients], ["aa:bb:cc:dd:ee:10"])

        self.lab.sources.scenario_empty_heater()
        self.assertEqual(unifi.list_clients(), [])
        st = shelly.status("127.0.0.1:18766/shelly/heater")
        self.assertTrue(st["on"])
        self.assertEqual(st["power_w"], 1800.0)
        fridge = shelly.status("127.0.0.1:18766/shelly/fridge")
        self.assertTrue(fridge["on"])
        self.assertLess(fridge["power_w"], 200)


if __name__ == "__main__":
    unittest.main()
