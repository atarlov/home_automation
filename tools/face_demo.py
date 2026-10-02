#!/usr/bin/env python3
"""Drive the attached T-Display-S3 without UniFi, Shelly, or NVIDIA.

  python3 tools/face_demo.py idle     # waiting face
  python3 tools/face_demo.py greet    # coming-home lines
  python3 tools/face_demo.py ask      # a question; tap the panel
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from display_serial import DisplayLink  # noqa: E402

SCENES = {
    "idle": ("netwatch/status", {"lines": []}),
    "greet": ("netwatch/notify", {"lines": ["Welcome home", "Hall light on"]}),
    "ask": (
        "netwatch/proposal",
        {
            "id": "prp_demo",
            "lines": ["Heater still on", "Turn it off?"],
            "button_a": "Off",
            "button_b": "Leave",
        },
    ),
}


def main(argv):
    scene = argv[1] if len(argv) > 1 else ""
    if scene not in SCENES:
        print(__doc__.strip())
        return 2
    link = DisplayLink()
    if not link.open():
        return 1
    # Opening the port can reboot the S3. Wait until it says hello, or until
    # it is clearly already running, before sending a frame.
    ready = time.time() + 1.5
    while time.time() < ready and not link.hellos:
        link.poll()
        time.sleep(0.05)
    time.sleep(0.15)
    topic, payload = SCENES[scene]
    body = json.dumps(payload)
    link.publish(topic, body)
    print(scene, flush=True)
    if scene != "ask":
        return 0
    print("tap Off or Leave on the panel", flush=True)
    seen_hello = link.hellos
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            presses = link.poll()
        except OSError as exc:
            print(f"display dropped ({exc}); waiting for it", flush=True)
            time.sleep(0.4)
            continue
        if link.hellos != seen_hello:
            seen_hello = link.hellos
            time.sleep(0.3)
            link.publish(topic, body)
        for press in presses:
            print(press["decision"], flush=True)
            link.publish(
                "netwatch/resolved",
                json.dumps({"id": press["id"], "status": press["decision"]}),
            )
            return 0
        time.sleep(0.05)
    print("no tap", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
