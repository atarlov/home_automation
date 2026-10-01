#!/usr/bin/env python3
"""Sign a proposal decision the way the ESP32 will. Usage: fake_button.py <proposal_id> approve|deny"""

import hashlib
import hmac
import json
import os
import sys
import time

from pathlib import Path

import paho.mqtt.publish as pub
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

pid, decision = sys.argv[1], sys.argv[2]
if decision not in ("approve", "deny"):
    raise SystemExit("decision must be approve or deny")
ts = int(time.time())
sig = hmac.new(
    os.environ["NETWATCH_HMAC_SECRET"].encode(),
    f"{pid}|{decision}|{ts}".encode(),
    hashlib.sha256,
).hexdigest()
pub.single(
    "netwatch/decision",
    json.dumps({"id": pid, "decision": decision, "ts": ts, "hmac": sig}),
    hostname=os.environ["MQTT_HOST"],
    port=int(os.environ.get("MQTT_PORT", 1883)),
    auth={"username": os.environ["MQTT_USER_ESP32"], "password": os.environ["MQTT_PASS_ESP32"]},
)
