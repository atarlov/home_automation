"""Shelly Cloud status parsing. No account and no network."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

import shelly  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_gen2_switch_and_gen1_relay(self):
        gen2 = shelly.switch_from_status({"switch:0": {"output": True, "apower": 18.5}})
        self.assertEqual(gen2, {"on": True, "power_w": 18.5})
        gen1 = shelly.switch_from_status(
            {"relays": [{"ison": False}], "meters": [{"power": 2.0}]}
        )
        self.assertEqual(gen1, {"on": False, "power_w": 2.0})
        self.assertIsNone(shelly.switch_from_status({"sys": {}}))

    def test_climate_and_motion_readings(self):
        room = shelly.reading_from_status(
            {"tmp": {"tC": 21.9, "is_valid": True}, "hum": {"value": 67, "is_valid": True}, "bat": {"value": 100}}
        )
        self.assertEqual(room["temp_c"], 21.9)
        self.assertEqual(room["humidity"], 67)
        self.assertEqual(room["battery"], 100)
        self.assertIsNone(room["on"])
        motion = shelly.reading_from_status(
            {"motion:0": {"motion": False}, "illuminance:0": {"lux": 110}, "devicepower:0": {"battery": {"percent": 90}}}
        )
        self.assertEqual(motion["motion"], 0)
        self.assertEqual(motion["lux"], 110)
        self.assertEqual(motion["battery"], 90)

    def test_all_status_map(self):
        body = {
            "isok": True,
            "data": {
                "devices_status": {
                    "abc": {
                        "_dev_info": {"gen": "G2", "online": True, "code": "SNPL-00112EU"},
                        "switch:0": {"output": False, "apower": 0},
                    }
                }
            },
        }
        found = shelly._devices_status_map(body)
        self.assertIn("abc", found)
        state = shelly.switch_from_status(found["abc"])
        self.assertEqual(state["on"], False)


class CloudSwitchTests(unittest.TestCase):
    def test_set_switch_uses_cloud_when_configured(self):
        with patch.dict(os.environ, {"SHELLY_CLOUD_HOST": "https://shelly-1-eu.shelly.cloud", "SHELLY_CLOUD_AUTH_KEY": "secret"}):
            with patch.object(shelly, "_cloud_post", return_value={"ok": True}) as post:
                shelly.set_switch_for("abc", "", 0, True)
        post.assert_called_once_with(
            "/v2/devices/api/set/switch",
            {"id": "abc", "channel": 0, "on": True},
        )

    def test_local_rpc_when_cloud_is_unset(self):
        with patch.dict(os.environ, {"SHELLY_CLOUD_HOST": "", "SHELLY_CLOUD_AUTH_KEY": ""}, clear=False):
            with patch.object(shelly, "set_switch", return_value={"ok": True}) as local:
                shelly.set_switch_for("hall", "192.168.1.50", 0, False)
        local.assert_called_once_with("192.168.1.50", 0, False)


if __name__ == "__main__":
    unittest.main()
