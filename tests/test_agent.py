"""Agent tools, display frames, and one scripted model turn. No network and no panel."""

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

import agent  # noqa: E402
import db  # noqa: E402
import house  # noqa: E402
from face import Face, encode_show, parse_device_line  # noqa: E402
from nvidia_client import Completion, ToolCall, parse_completion  # noqa: E402

MAC = "aa:bb:cc:dd:ee:10"


class Sink:
    def __init__(self):
        self.face = Face()
        self.frames = []

    def publish(self, topic, payload, qos=0, retain=False):
        line = self.face.apply(topic, payload)
        if line:
            self.frames.append(line)


class ScriptModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        return self.replies.pop(0)


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["NETWATCH_DB"] = os.path.join(self.tmp.name, "t.db")
        os.environ["NETWATCH_HMAC_SECRET"] = "test-hmac"
        os.environ["PROPOSAL_TTL_SEC"] = "120"
        os.environ["ARRIVAL_ABSENCE_MIN"] = "20"
        db.init_db()

    def test_frames_round_trip_and_buttons(self):
        frame = encode_show("ask", ["Heater still on", "Turn it off?"], "prp_abc", "Off", "Leave")
        self.assertTrue(frame.startswith("SHOW ask|prp_abc|Off|Leave|"))
        self.assertLessEqual(max(len(part) for part in frame.split("|")[4:]), 18)
        self.assertEqual(parse_device_line("HELLO T-Display-S3-AMOLED 240 536")["op"], "hello")
        press = parse_device_line("BTN prp_abc approve")
        self.assertEqual(press, {"op": "button", "id": "prp_abc", "decision": "approve"})
        self.assertIsNone(parse_device_line("BTN prp_abc maybe"))

        face = Face()
        ask = face.apply(
            "netwatch/proposal",
            {"id": "prp_abc", "lines": ["Heater still on"], "button_a": "Off", "button_b": "Leave"},
        )
        self.assertIn("ask|prp_abc|", ask)
        back = face.apply("netwatch/resolved", {"id": "prp_abc", "status": "denied"})
        self.assertTrue(back.startswith("SHOW idle|"))
        greeting = face.apply("netwatch/notify", {"lines": ["Welcome home"]})
        self.assertIn("Welcome home", greeting)
        self.assertIsNone(face.apply("netwatch/resolved", {"id": "prp_abc"}))

    def test_model_tool_call_parses(self):
        completion = parse_completion(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "show",
                                        "arguments": json.dumps({"lines": ["Quiet night"]}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(completion.tool_calls[0].name, "show")
        self.assertEqual(completion.tool_calls[0].arguments["lines"], ["Quiet night"])

    def test_arrival_turns_the_comfort_light_on_and_greets(self):
        self._hall()
        event_id = self._event("arrival", mac=MAC, details={"away_min": 180})
        model = ScriptModel(
            [
                Completion(
                    tool_calls=[
                        ToolCall(
                            "1",
                            "propose",
                            {
                                "event_id": event_id,
                                "action_type": "shelly_switch",
                                "device_id": "hall",
                                "on": True,
                                "lines": ["Welcome home", "Hall light on"],
                            },
                        )
                    ]
                ),
                Completion(content="welcomed"),
            ]
        )
        sink = Sink()
        calls = []
        with patch.object(agent.gateway, "execute", lambda action, c: calls.append(action) or {"ok": True}):
            outcome = agent.run_turn(model, sink)
        self.assertEqual(outcome["content"], "welcomed")
        self.assertEqual(calls[0]["params"]["device_id"], "hall")
        self.assertTrue(calls[0]["params"]["on"])
        self.assertTrue(any("Welcome home" in frame for frame in sink.frames))
        with db.conn() as c:
            status = c.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()["status"]
        self.assertEqual(status, "done")

    def test_fridge_off_is_refused_and_the_event_stays_open(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, never_switch_off) VALUES('fridge','Fridge','10.0.0.6',1)"
            )
        event_id = self._event("shelly_power", device_id="fridge")
        result = house.propose(
            event_id,
            {"type": "shelly_switch", "params": {"device_id": "fridge", "on": False}},
            ["Fridge off?"],
            sink=Sink(),
        )
        self.assertEqual(result["error"], "never_switch_off")
        with db.conn() as c:
            status = c.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()["status"]
        self.assertEqual(status, "pending")

    def test_question_waits_for_the_button(self):
        with db.conn() as c:
            c.execute(
                "INSERT INTO devices(mac, hostname, first_seen, last_seen) VALUES(?,?,?,?)",
                (MAC, "cam", db.now_iso(), db.now_iso()),
            )
        event_id = self._event("new_client", mac=MAC)
        sink = Sink()
        result = house.propose(
            event_id,
            {"type": "block_client", "params": {"mac": MAC}},
            ["Unknown camera", "Block it?"],
            button_a="Block",
            button_b="Leave",
            sink=sink,
        )
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["tier"], "secure")
        self.assertTrue(sink.frames[0].startswith("SHOW ask|"))
        covered = house.show(["nope"], sink)
        self.assertEqual(covered["error"], "display is waiting for a tap")

        calls = []
        with patch.object(agent.gateway, "execute", lambda action, c: calls.append(action) or {"ok": True}):
            status = agent.handle_button(sink, result["id"], "approve")
        self.assertEqual(status, "executed")
        self.assertEqual(calls[0]["type"], "block_client")
        self.assertTrue(sink.frames[-1].startswith("SHOW idle|"))
        self.assertIn("Done", sink.frames[-1])

    def test_a_glance_with_no_tools_closes_a_claimed_event(self):
        event_id = self._event("departure", mac=MAC)
        model = ScriptModel([Completion(content="nothing to do")])
        outcome = agent.run_turn(model, Sink())
        self.assertEqual(outcome["events"], [event_id])
        with db.conn() as c:
            status = c.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()["status"]
            left = c.execute("SELECT kind FROM agent_turns").fetchone()["kind"]
        self.assertEqual(status, "done")
        self.assertEqual(left, "turn")

    def _hall(self):
        with db.conn() as c:
            c.execute("INSERT INTO people(id, name) VALUES(1, 'Assen')")
            c.execute(
                "INSERT INTO devices(mac, friendly_name, person_id, presence_device, online, last_seen) "
                "VALUES(?,?,1,1,1,?)",
                (MAC, "Phone", db.now_iso()),
            )
            c.execute(
                "INSERT INTO shelly(device_id, name, ip, comfort_auto, never_switch_off) "
                "VALUES('hall','Hall','10.0.0.8',1,0)"
            )

    def _event(self, type_, mac=None, device_id=None, details=None):
        event_id = "evt_" + uuid.uuid4().hex[:10]
        with db.conn() as c:
            c.execute(
                "INSERT INTO events(id, ts, type, source, mac, device_id, details) VALUES(?,?,?,?,?,?,?)",
                (event_id, db.now_iso(), type_, "test", mac, device_id, json.dumps(details or {})),
            )
        return event_id


if __name__ == "__main__":
    unittest.main()
