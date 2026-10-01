"""Host-side tests that do not need UniFi, Shelly, or a broker."""

import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

import api  # noqa: E402
import collector  # noqa: E402
import db  # noqa: E402
import gateway  # noqa: E402
import rollup  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from models import Triage  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}
MAC = "aa:bb:cc:dd:ee:01"


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["NETWATCH_DB"] = os.path.join(self.tmp.name, "t.db")
        os.environ["NETWATCH_API_TOKEN"] = "test-token"
        os.environ["NETWATCH_HMAC_SECRET"] = "test-hmac"
        os.environ["ARRIVAL_ABSENCE_MIN"] = "20"
        os.environ["PROPOSAL_TTL_SEC"] = "120"
        os.environ["MQTT_HOST"] = "127.0.0.1"
        os.environ["MQTT_PORT"] = "1883"
        os.environ["MQTT_USER_API"] = "api"
        os.environ["MQTT_PASS_API"] = "x"
        db.init_db()

    def events(self):
        with db.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY ts")]


class EmitTests(DbCase):
    def test_cooldown_suppresses_the_same_subject(self):
        with db.conn() as c:
            first = db.emit(c, "new_client", "unifi", mac=MAC)
            second = db.emit(c, "new_client", "unifi", mac=MAC)
            other = db.emit(c, "vlan_mismatch", "unifi", mac=MAC)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(other)
        self.assertEqual(len(self.events()), 2)


class CollectorTests(DbCase):
    def client(self, mac=MAC, **extra):
        base = {
            "mac": mac,
            "hostname": "phone",
            "oui": "Apple",
            "network": "LAN",
            "ap_mac": "ap1",
            "tx_bytes": 1000,
            "rx_bytes": 1000,
        }
        base.update(extra)
        return base

    def test_new_client_is_not_an_arrival(self):
        with patch.object(collector.unifi, "list_clients", return_value=[self.client()]):
            with db.conn() as c:
                collector.poll_unifi(c)
        kinds = [e["type"] for e in self.events()]
        self.assertEqual(kinds, ["new_client"])

    def test_arrival_only_after_the_absence_window(self):
        self._presence(minutes_ago=25)
        with patch.object(collector.unifi, "list_clients", return_value=[self.client()]):
            with db.conn() as c:
                collector.poll_unifi(c)
        self.assertIn("arrival", [e["type"] for e in self.events()])

        self.setUp()
        self._presence(minutes_ago=5)
        with patch.object(collector.unifi, "list_clients", return_value=[self.client()]):
            with db.conn() as c:
                collector.poll_unifi(c)
        self.assertNotIn("arrival", [e["type"] for e in self.events()])

    def test_departure_and_house_empty(self):
        self._presence(minutes_ago=25, online=0)
        with db.conn() as c:
            collector.check_presence(c)
        kinds = [e["type"] for e in self.events()]
        self.assertIn("departure", kinds)
        self.assertIn("house_empty", kinds)

    def test_vlan_mismatch_is_debounced(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, expected_vlan, online, last_seen, last_tx_bytes, last_rx_bytes) "
                "VALUES(?,?,1,?,0,0)",
                (MAC, "LAN", db.now_iso()),
            )
        seen = [self.client(network="IoT")]
        with patch.object(collector.unifi, "list_clients", return_value=seen):
            with db.conn() as c:
                collector.poll_unifi(c)
                collector.poll_unifi(c)
        mismatches = [e for e in self.events() if e["type"] == "vlan_mismatch"]
        self.assertEqual(len(mismatches), 1)

    def test_traffic_spike_needs_a_baseline(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, online, last_seen, last_tx_bytes, last_rx_bytes, trusted) "
                "VALUES(?,1,?,0,0,0)",
                (MAC, db.now_iso()),
            )
            c.execute(
                "INSERT INTO baselines(mac, hour_of_day, avg_tx_mb, avg_rx_mb, samples) VALUES(?,?,10,1,5)",
                (MAC, db.hour_of_day()),
            )
        huge = self.client(tx_bytes=80_000_000, rx_bytes=0)
        with patch.object(collector.unifi, "list_clients", return_value=[huge]):
            with db.conn() as c:
                collector.poll_unifi(c)
        self.assertIn("traffic_spike", [e["type"] for e in self.events()])

    def test_empty_house_flags_a_heater_and_spares_a_fridge(self):
        self._presence(minutes_ago=30, online=0)
        with db.conn() as c:
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, idle_w, never_switch_off) VALUES(?,?,?,?,?)",
                ("heater", "Heater", "10.0.0.5", 1, 0),
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, idle_w, never_switch_off) VALUES(?,?,?,?,?)",
                ("fridge", "Fridge", "10.0.0.6", 40, 1),
            )

        def status(ip, ch=0):
            return {"on": True, "power_w": 1800 if ip.endswith(".5") else 90}

        with patch.object(collector.shelly, "status", side_effect=status):
            with db.conn() as c:
                collector.poll_shelly(c)
        power = [e for e in self.events() if e["type"] == "shelly_power"]
        self.assertEqual(len(power), 1)
        self.assertEqual(power[0]["device_id"], "heater")

    def _presence(self, minutes_ago, online=0):
        seen = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with db.conn() as c:
            c.execute("INSERT INTO people(id, name) VALUES(1, 'Assen')")
            c.execute(
                "INSERT INTO devices(mac, hostname, online, last_seen, presence_device, person_id, "
                "last_tx_bytes, last_rx_bytes) VALUES(?,?,?,?,1,1,0,0)",
                (MAC, "phone", online, seen),
            )


class RollupTests(DbCase):
    def test_rollup_averages_the_last_two_weeks(self):
        now = dt.datetime.now()
        recent_a = now.strftime("%Y-%m-%dT%H")
        recent_b = (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H")
        old = (now - dt.timedelta(days=40)).strftime("%Y-%m-%dT%H")
        with db.conn() as c:
            c.execute(
                "INSERT INTO traffic_hourly(mac, hour, tx_mb, rx_mb) VALUES(?,?,?,?)",
                (MAC, recent_a, 10, 1),
            )
            c.execute(
                "INSERT INTO traffic_hourly(mac, hour, tx_mb, rx_mb) VALUES(?,?,?,?)",
                (MAC, recent_b, 30, 3),
            )
            c.execute(
                "INSERT INTO traffic_hourly(mac, hour, tx_mb, rx_mb) VALUES(?,?,?,?)",
                (MAC, old, 1000, 1000),
            )
            rollup.rollup(c)
            row = c.execute(
                "SELECT avg_tx_mb, samples, hour_of_day FROM baselines WHERE mac=?",
                (MAC,),
            ).fetchone()
        self.assertEqual(row["samples"], 2)
        self.assertEqual(row["avg_tx_mb"], 20)
        self.assertEqual(row["hour_of_day"], now.hour)


class ModelTests(unittest.TestCase):
    def test_oled_limits(self):
        Triage.model_validate(_body())
        with self.assertRaises(ValidationError):
            Triage.model_validate(_body(oled=_oled(lines=("x" * 22,))))
        with self.assertRaises(ValidationError):
            Triage.model_validate(_body(oled=_oled(lines=("hi \u2603",))))


class ApiTests(DbCase):
    def setUp(self):
        super().setUp()
        self.http = TestClient(api.app)
        self.enterContext(self.http)

    def test_auth_and_claim(self):
        eid = self._event()
        bare = self.http.get("/events/pending")
        self.assertEqual(bare.status_code, 401)
        first = self.http.get("/events/pending", headers=AUTH)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()[0]["id"], eid)
        second = self.http.get("/events/pending", headers=AUTH)
        self.assertEqual(second.json(), [])

    def test_stale_claim_is_requeued(self):
        eid = self._event()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        with db.conn() as c:
            c.execute(
                "UPDATE events SET status='claimed', claimed_at=? WHERE id=?",
                (old, eid),
            )
        body = self.http.get("/events/pending", headers=AUTH).json()
        self.assertEqual([row["id"] for row in body], [eid])

    def test_fresh_claim_is_kept(self):
        eid = self._event()
        with db.conn() as c:
            c.execute(
                "UPDATE events SET status='claimed', claimed_at=? WHERE id=?",
                (db.now_iso(), eid),
            )
        self.assertEqual(self.http.get("/events/pending", headers=AUTH).json(), [])

    def test_context_includes_past_decisions_and_routines(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, vendor, hostname, vlan, trusted, online, last_seen) "
                "VALUES(?,?,?,?,0,1,?)",
                (MAC, "Espressif", "plug", "IoT", db.now_iso()),
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, comfort_auto) VALUES('shelly-hall','Hall','10.0.0.8',1)"
            )
            eid = self._event(c, type_="arrival", details={"away_min": 30})
            c.execute(
                "INSERT INTO proposals(id, event_id, ts, action, pattern, status) "
                "VALUES(?,?,?,?,?, 'denied')",
                (
                    "prp_old",
                    eid,
                    db.now_iso(),
                    json.dumps({"type": "none", "params": {}}),
                    "arrival/IoT/espressif",
                ),
            )
            c.execute(
                "INSERT INTO decisions(proposal_id, ts, source, decision) VALUES('prp_old', ?, 'esp32', 'deny')",
                (db.now_iso(),),
            )
        body = self.http.get(f"/events/{eid}/context", headers=AUTH)
        self.assertEqual(body.status_code, 200)
        ctx = body.json()
        self.assertEqual(ctx["device"]["mac"], MAC)
        self.assertEqual(ctx["past_decisions"][0]["owner"], "deny")
        self.assertEqual(ctx["past_decisions"][0]["action"]["type"], "none")
        self.assertEqual(ctx["arrival_routines"][0]["params"]["device_id"], "shelly-hall")

    def test_log_completes_and_a_second_submit_conflicts(self):
        eid = self._event()
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, hostname, online, last_seen) VALUES(?,?,1,?)",
                (MAC, "phone", db.now_iso()),
            )
        ok = self.http.post(f"/events/{eid}/triage", headers=AUTH, json=_body(memory_note="seen once"))
        self.assertEqual(ok.status_code, 200)
        again = self.http.post(f"/events/{eid}/triage", headers=AUTH, json=_body())
        self.assertEqual(again.status_code, 409)
        with db.conn() as c:
            note = c.execute("SELECT note FROM memory_notes").fetchone()["note"]
            status = c.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()["status"]
        self.assertEqual(note, "seen once")
        self.assertEqual(status, "done")

    def test_validation_and_target_checks(self):
        eid = self._event()
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, hostname, online, last_seen) VALUES(?,?,1,?)",
                (MAC, "phone", db.now_iso()),
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, never_switch_off) VALUES('fridge','Fridge','10.0.0.6',1)"
            )
        long_line = self.http.post(
            f"/events/{eid}/triage",
            headers=AUTH,
            json=_body(decision="notify", action=_action("none"), oled=_oled(lines=("x" * 22,))),
        )
        self.assertEqual(long_line.status_code, 422)
        smuggle = self.http.post(
            f"/events/{eid}/triage",
            headers=AUTH,
            json=_body(decision="log", action=_action("block_client", {"mac": MAC})),
        )
        self.assertEqual(smuggle.status_code, 422)
        wrong_mac = self.http.post(
            f"/events/{eid}/triage",
            headers=AUTH,
            json=_body(
                decision="propose",
                action=_action("block_client", {"mac": "ff:ff:ff:ff:ff:ff"}),
                oled=_oled(),
            ),
        )
        self.assertEqual(wrong_mac.status_code, 422)
        fridge = self._event(type_="shelly_power", mac=None, device_id="fridge")
        protected = self.http.post(
            f"/events/{fridge}/triage",
            headers=AUTH,
            json=_body(
                decision="propose",
                action=_action("shelly_switch", {"device_id": "fridge", "channel": 0, "on": False}),
                oled=_oled(),
            ),
        )
        self.assertEqual(protected.status_code, 422)

    def test_propose_records_a_proposal(self):
        eid = self._event()
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, hostname, online, last_seen) VALUES(?,?,1,?)",
                (MAC, "phone", db.now_iso()),
            )
        sent = []

        def capture(topic, payload, retain=False):
            sent.append((topic, payload))

        with patch.object(api, "publish", capture):
            res = self.http.post(
                f"/events/{eid}/triage",
                headers=AUTH,
                json=_body(
                    decision="propose",
                    action=_action("block_client", {"mac": MAC}),
                    oled=_oled(),
                ),
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(sent[0][0], "netwatch/internal/proposal")
        with db.conn() as c:
            row = c.execute("SELECT status, action FROM proposals").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(json.loads(row["action"])["type"], "block_client")

    def test_fail_marks_the_event(self):
        eid = self._event()
        res = self.http.post(f"/events/{eid}/fail", headers=AUTH, json={"reason": "model timeout"})
        self.assertEqual(res.status_code, 200)
        with db.conn() as c:
            status = c.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()["status"]
        self.assertEqual(status, "failed")

    def _event(self, c=None, type_="new_client", mac=MAC, device_id=None, details=None):
        eid = "evt_" + uuid.uuid4().hex[:10]
        row = (eid, db.now_iso(), type_, "test", mac, device_id, json.dumps(details or {}))
        if c is not None:
            c.execute(
                "INSERT INTO events(id, ts, type, source, mac, device_id, details) VALUES(?,?,?,?,?,?,?)",
                row,
            )
            return eid
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO events(id, ts, type, source, mac, device_id, details) VALUES(?,?,?,?,?,?,?)",
                row,
            )
        return eid


class GatewayTests(DbCase):
    def test_tiers(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, comfort_auto, never_switch_off) "
                "VALUES('hall','Hall','10.0.0.8',1,0)"
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, comfort_auto, never_switch_off) "
                "VALUES('fridge','Fridge','10.0.0.6',0,1)"
            )
            self.assertEqual(
                gateway.tier_for({"type": "shelly_switch", "params": {"device_id": "hall", "on": True}}, c),
                "comfort",
            )
            self.assertEqual(
                gateway.tier_for({"type": "shelly_switch", "params": {"device_id": "hall", "on": False}}, c),
                "approve",
            )
            self.assertEqual(
                gateway.tier_for({"type": "shelly_switch", "params": {"device_id": "fridge", "on": False}}, c),
                "reject",
            )
            self.assertEqual(gateway.tier_for({"type": "block_client", "params": {"mac": MAC}}, c), "secure")
            self.assertEqual(gateway.tier_for({"type": "guest_voucher", "params": {"minutes": 60}}, c), "approve")
            self.assertEqual(gateway.tier_for({"type": "none", "params": {}}, c), "reject")

    def test_signature_window(self):
        good = _signed("prp_1", "approve", 1_000)
        self.assertTrue(gateway.valid_sig(good, now=1_030))
        self.assertFalse(gateway.valid_sig(good, now=1_070))
        bad = dict(good, hmac="0" * 64)
        self.assertFalse(gateway.valid_sig(bad, now=1_030))

    def test_approve_once_then_ignore_the_replay(self):
        calls = []

        def fake_execute(action, c):
            calls.append(action)
            return {"blocked": True}

        pid = self._proposal({"type": "block_client", "params": {"mac": MAC}})
        with patch.object(gateway, "execute", fake_execute):
            with db.conn() as c:
                first = gateway.process_decision(c, _signed(pid, "approve", 5_000), now=5_010)
            with db.conn() as c:
                second = gateway.process_decision(c, _signed(pid, "approve", 5_020), now=5_030)
        self.assertEqual(first, "executed")
        self.assertEqual(second, "rejected")
        self.assertEqual(len(calls), 1)
        with db.conn() as c:
            status = c.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()["status"]
            audits = c.execute(
                "SELECT what FROM audit WHERE what='decision_rejected'"
            ).fetchall()
        self.assertEqual(status, "executed")
        self.assertEqual(len(audits), 1)

    def test_bad_signature_and_expiry_do_not_run(self):
        pid = self._proposal({"type": "block_client", "params": {"mac": MAC}}, expires=100)
        with patch.object(gateway, "execute", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran"))):
            with db.conn() as c:
                # Signature is fine, but the proposal already expired.
                rejected = gateway.process_decision(c, _signed(pid, "approve", 150), now=160)
            with db.conn() as c:
                denied = gateway.process_decision(
                    c, _signed("prp_missing", "deny", 80), now=90
                )
            with db.conn() as c:
                bad = dict(_signed(pid, "approve", 50), hmac="0" * 64)
                bad_sig = gateway.process_decision(c, bad, now=80)
        self.assertEqual(rejected, "rejected")
        self.assertEqual(denied, "rejected")
        self.assertEqual(bad_sig, "rejected")
        with db.conn() as c:
            self.assertEqual(
                c.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()["status"],
                "pending",
            )

    def test_deny_records_the_decision(self):
        pid = self._proposal({"type": "block_client", "params": {"mac": MAC}})
        with db.conn() as c:
            status = gateway.process_decision(c, _signed(pid, "deny", 10), now=20)
            decision = c.execute("SELECT decision FROM decisions WHERE proposal_id=?", (pid,)).fetchone()
        self.assertEqual(status, "denied")
        self.assertEqual(decision["decision"], "deny")

    def test_expire_leaves_unclassified_proposals(self):
        old = self._proposal({"type": "block_client", "params": {"mac": MAC}}, tier="secure", expires=10)
        fresh = self._proposal({"type": "block_client", "params": {"mac": MAC}}, tier=None, expires=10)
        with db.conn() as c:
            gone = gateway.expire_pending(c, now=20)
            left = c.execute("SELECT status FROM proposals WHERE id=?", (fresh,)).fetchone()["status"]
        self.assertEqual(gone, [old])
        self.assertEqual(left, "pending")

    def test_comfort_runs_and_reject_does_not(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, comfort_auto, never_switch_off) "
                "VALUES('hall','Hall','10.0.0.8',1,0)"
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, never_switch_off) VALUES('fridge','Fridge','10.0.0.6',1)"
            )
        comfort = self._proposal(
            {"type": "shelly_switch", "params": {"device_id": "hall", "channel": 0, "on": True}},
            tier=None,
            oled=_oled(lines=("Welcome back",)),
        )
        blocked = self._proposal(
            {"type": "shelly_switch", "params": {"device_id": "fridge", "channel": 0, "on": False}},
            tier=None,
        )
        calls = []
        cli = _Cli()
        with patch.object(gateway, "execute", lambda action, c: calls.append(action) or {"ok": True}):
            gateway.handle_proposal(cli, comfort, now=time.time())
            gateway.handle_proposal(cli, blocked, now=time.time())
        self.assertEqual(calls[0]["params"]["device_id"], "hall")
        topics = [item[0] for item in cli.sent]
        self.assertIn("netwatch/notify", topics)
        self.assertIn("netwatch/resolved", topics)
        with db.conn() as c:
            comfort_status = c.execute("SELECT status, tier FROM proposals WHERE id=?", (comfort,)).fetchone()
            blocked_status = c.execute("SELECT status FROM proposals WHERE id=?", (blocked,)).fetchone()["status"]
        self.assertEqual(comfort_status["status"], "executed")
        self.assertEqual(comfort_status["tier"], "comfort")
        self.assertEqual(blocked_status, "rejected")

    def test_secure_proposal_is_published_and_not_executed(self):
        pid = self._proposal(
            {"type": "quarantine_client", "params": {"mac": MAC}},
            tier=None,
            oled=_oled(lines=("Quarantine?",)),
        )
        cli = _Cli()
        with patch.object(gateway, "execute", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran"))):
            gateway.handle_proposal(cli, pid, now=time.time())
        self.assertEqual(cli.sent[0][0], "netwatch/proposal")
        self.assertEqual(cli.sent[0][1]["tier"], "secure")
        with db.conn() as c:
            status = c.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()["status"]
        self.assertEqual(status, "pending")

    def _proposal(self, action, tier="secure", expires=None, oled=None):
        pid = "prp_" + uuid.uuid4().hex[:10]
        eid = "evt_" + uuid.uuid4().hex[:10]
        with db.conn() as c:
            c.execute(
                "INSERT INTO events(id, ts, type, source, details) VALUES(?,?, 'new_client', 'test', '{}')",
                (eid, db.now_iso()),
            )
            c.execute(
                "INSERT INTO proposals(id, event_id, ts, action, tier, oled, expires_at, status) "
                "VALUES(?,?,?,?,?,?,?, 'pending')",
                (
                    pid,
                    eid,
                    db.now_iso(),
                    json.dumps(action),
                    tier,
                    json.dumps(oled) if oled else None,
                    time.time() + 100 if expires is None else expires,
                ),
            )
        return pid


class _Cli:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.sent.append((topic, json.loads(payload)))


def _oled(lines=("Hello",), button_a="Yes", button_b="No"):
    return {"lines": list(lines), "button_a": button_a, "button_b": button_b}


def _action(kind, params=None):
    return {"type": kind, "params": params or {}}


def _body(**overrides):
    body = {
        "decision": "log",
        "severity": 1,
        "confidence": 0.5,
        "reason": "test",
        "action": _action("none"),
        "oled": None,
        "summary_line": "test",
        "memory_note": None,
        "suspicious_input": False,
    }
    body.update(overrides)
    return body


def _signed(pid, decision, ts):
    digest = hmac.new(
        b"test-hmac",
        f"{pid}|{decision}|{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"id": pid, "decision": decision, "ts": ts, "hmac": digest}


if __name__ == "__main__":
    unittest.main()
