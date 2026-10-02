"""Frames for the T-Display-S3 AMOLED. Four lines, eighteen ASCII characters.

The panel is drawn landscape, long edge horizontal. Empty lines mean the
waiting face, with no caption.
"""

import json

LINE_LEN = 18
BUTTON_LEN = 8
LINE_COUNT = 4


def clean_line(value, limit=LINE_LEN):
    text = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in str(value or ""))
    text = " ".join(text.replace("|", "/").split())
    return text[:limit]


def clean_lines(values):
    lines = []
    for value in values or []:
        line = clean_line(value)
        if line:
            lines.append(line)
        if len(lines) == LINE_COUNT:
            break
    return lines


def clean_button(value, default):
    text = clean_line(value or default, BUTTON_LEN)
    return text or default


def encode_show(mode, lines, proposal_id=None, button_a=None, button_b=None):
    shown = clean_lines(lines)
    while len(shown) < LINE_COUNT:
        shown.append("")
    if mode != "ask":
        mode = "idle"
        proposal_id = None
        button_a = None
        button_b = None
    pid = clean_line(proposal_id or "-", 24) or "-"
    left = clean_button(button_a, "Yes") if mode == "ask" else "-"
    right = clean_button(button_b, "No") if mode == "ask" else "-"
    body = "|".join([mode, pid, left, right, *shown])
    return f"SHOW {body}"


def parse_device_line(line):
    text = (line or "").strip()
    if not text:
        return None
    if text.startswith("HELLO"):
        return {"op": "hello", "raw": text}
    if text == "PONG":
        return {"op": "pong"}
    if text.startswith("BTN "):
        parts = text.split()
        if len(parts) == 3 and parts[2] in ("approve", "deny"):
            return {"op": "button", "id": parts[1], "decision": parts[2]}
    return None


class Face:
    """Remember the idle lines and turn gateway publishes into SHOW frames."""

    def __init__(self):
        self.status_lines = []
        self.mode = "idle"

    def apply(self, topic, payload):
        if isinstance(payload, str):
            payload = json.loads(payload)
        if topic == "netwatch/status":
            self.status_lines = clean_lines(payload.get("lines"))
            self.mode = "idle"
            return encode_show("idle", self.status_lines)
        if topic == "netwatch/notify":
            lines = clean_lines(payload.get("lines")) or list(self.status_lines)
            self.mode = "idle"
            return encode_show("idle", lines)
        if topic == "netwatch/proposal":
            self.mode = "ask"
            return encode_show(
                "ask",
                payload.get("lines"),
                payload.get("id"),
                payload.get("button_a"),
                payload.get("button_b"),
            )
        if topic == "netwatch/resolved" and self.mode == "ask":
            self.mode = "idle"
            return encode_show("idle", self.status_lines)
        return None
