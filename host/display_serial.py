"""USB serial link to the T-Display-S3 AMOLED firmware.

Opening /dev/cu.* asserts DTR, and this board treats that as a reset. The
panel then disappears and the next read raises "Device not configured".
Prefer /dev/tty.* and keep DTR and RTS low so a tap can be read.
"""

import os
import time

from face import Face, encode_card, parse_device_line


def default_port():
    chosen = os.environ.get("DISPLAY_PORT")
    if chosen:
        return chosen
    tty = "/dev/tty.usbmodem3101"
    if os.path.exists(tty):
        return tty
    return "/dev/cu.usbmodem3101"


class DisplayLink:
    def __init__(self, port=None, baud=115200):
        self.port_name = port or default_port()
        self.baud = baud
        self.face = Face()
        self.hellos = 0
        self._ser = None
        self._buf = ""
        self._next_open = 0.0

    def open(self):
        now = time.time()
        if now < self._next_open:
            return False
        try:
            import serial
        except ImportError:
            print("pyserial is not installed; the display stays dark", flush=True)
            return False
        try:
            port = serial.Serial()
            port.port = self.port_name
            port.baudrate = self.baud
            port.timeout = 0
            port.dtr = False
            port.rts = False
            port.open()
        except Exception as exc:
            self._next_open = now + 0.5
            print(f"display not open ({self.port_name}): {exc}", flush=True)
            return False
        self._ser = port
        self._next_open = 0.0
        print(f"display open {self.port_name}", flush=True)
        return True

    def send_deck(self, cards):
        cards = list(cards)[:4] or [{"mode": "idle", "lines": []}]
        count = len(cards)
        for index, card in enumerate(cards):
            line = encode_card(
                index,
                count,
                card.get("mode") or "idle",
                card.get("lines") or [],
                card.get("id"),
                card.get("button_a"),
                card.get("button_b"),
                card.get("pict") or "face",
            )
            self._write_line(line)

    def _write_line(self, line):
        if self._ser is None and not self.open():
            return
        try:
            self._ser.write((line + "\n").encode())
        except OSError as exc:
            self._drop(exc)

    def publish(self, topic, payload, qos=0, retain=False):
        line = self.face.apply(topic, payload)
        if not line:
            return
        self._write_line(line)

    def poll(self):
        if self._ser is None:
            self.open()
            return []
        try:
            chunk = self._ser.read(1024)
        except OSError as exc:
            self._drop(exc)
            return []
        if not chunk:
            return []
        self._buf += chunk.decode("utf-8", "replace")
        found = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            parsed = parse_device_line(line)
            if not parsed:
                continue
            if parsed["op"] == "hello":
                self.hellos += 1
                print(parsed["raw"], flush=True)
            elif parsed["op"] == "button":
                found.append(parsed)
        if len(self._buf) > 2048:
            self._buf = self._buf[-256:]
        return found

    def _drop(self, exc):
        print(f"display dropped ({exc}); waiting for it", flush=True)
        try:
            if self._ser is not None:
                self._ser.close()
        except OSError:
            pass
        self._ser = None
        self._buf = ""
        self._next_open = time.time() + 0.4
