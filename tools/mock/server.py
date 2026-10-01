"""HTTP surface for fake UniFi/Shelly, scenario controls, and the AMOLED simulator."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .constants import MOCK_BIND, MOCK_PORT, PANEL_H, PANEL_W, PRODUCT_NAME
from .display_state import DisplayReducer
from .signing import sign_decision
from .sources import LabSources

DISPLAY_DIR = Path(__file__).resolve().parents[1] / "mock_display"
TOPICS = (
    "netwatch/status",
    "netwatch/notify",
    "netwatch/proposal",
    "netwatch/resolved",
    "netwatch/heartbeat",
)


class MockLab:
    def __init__(self, sources: LabSources | None = None):
        self.sources = sources or LabSources()
        self.display = DisplayReducer()
        self.mqtt_ok = False
        self.mqtt_error: str | None = None
        self._mqtt_client = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start_mqtt_bridge(self) -> bool:
        """Optional: subscribe to netwatch topics and feed the display reducer.

        Retries when the broker is down at startup (Errno 61) and reconnects
        after later disconnects so the UI can flip to online without a restart.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:
            self.mqtt_ok = False
            self.mqtt_error = f"paho-mqtt missing: {e}"
            return False
        host = os.environ.get("MQTT_HOST", "127.0.0.1")
        port = int(os.environ.get("MQTT_PORT", 1883))
        user = os.environ.get("MQTT_USER_ESP32", "esp32")
        password = os.environ.get("MQTT_PASS_ESP32", "dev-esp32-pass")

        def on_connect(cli, _userdata, _flags, reason_code, _properties=None):
            code = getattr(reason_code, "value", reason_code)
            try:
                ok = int(code) == 0
            except (TypeError, ValueError):
                ok = not getattr(reason_code, "is_failure", True)
            if not ok:
                self.mqtt_ok = False
                self.mqtt_error = f"MQTT refused: {reason_code}"
                return
            self.mqtt_ok = True
            self.mqtt_error = None
            for topic in TOPICS:
                cli.subscribe(topic, qos=1)

        def on_disconnect(_cli, _userdata, *args, **_kwargs):
            # paho v1: (client, userdata, rc); v2: (..., disconnect_flags, reason_code, properties)
            reason = args[-1] if len(args) == 1 else (args[1] if len(args) >= 2 else args[0] if args else "disconnected")
            self.mqtt_ok = False
            if self.mqtt_error is None:
                self.mqtt_error = f"MQTT disconnected: {reason}"

        def on_message(_cli, _userdata, message):
            try:
                payload = json.loads(message.payload.decode() or "{}")
            except json.JSONDecodeError:
                payload = {}
            self.display.apply(message.topic, payload)

        try:
            cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mock-display")
        except Exception:
            cli = mqtt.Client(client_id="mock-display")
        cli.username_pw_set(user, password)
        cli.reconnect_delay_set(min_delay=1, max_delay=30)
        cli.on_connect = on_connect
        cli.on_disconnect = on_disconnect
        cli.on_message = on_message
        self._mqtt_client = cli
        self.mqtt_ok = False
        self.mqtt_error = f"connecting to {host}:{port}…"

        def _connect_forever():
            while self._mqtt_client is cli:
                try:
                    cli.connect(host, port, keepalive=30)
                    cli.loop_start()
                    return
                except Exception as e:
                    self.mqtt_ok = False
                    self.mqtt_error = str(e)
                    time.sleep(2)

        threading.Thread(target=_connect_forever, name="mock-mqtt-connect", daemon=True).start()
        return True

    def publish_decision(self, proposal_id: str, decision: str) -> dict[str, Any]:
        secret = os.environ["NETWATCH_HMAC_SECRET"]
        body = sign_decision(proposal_id, decision, secret)
        if self._mqtt_client is None or not self.mqtt_ok:
            raise RuntimeError(self.mqtt_error or "MQTT broker not connected")
        import paho.mqtt.publish as pub

        pub.single(
            "netwatch/decision",
            json.dumps(body),
            hostname=os.environ.get("MQTT_HOST", "127.0.0.1"),
            port=int(os.environ.get("MQTT_PORT", 1883)),
            auth={
                "username": os.environ.get("MQTT_USER_ESP32", "esp32"),
                "password": os.environ.get("MQTT_PASS_ESP32", "dev-esp32-pass"),
            },
        )
        return body

    def inject_display(self, topic: str, payload: dict) -> dict:
        """Push a payload into the reducer without MQTT (unit / offline demo)."""
        return self.display.apply(topic, payload).as_dict()

    def serve_forever(self, host: str = MOCK_BIND, port: int = MOCK_PORT):
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                print(f"[mock] {self.address_string()} {fmt % args}", flush=True)

            def _json(self, code: int, body: Any):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(raw)

            def _bytes(self, code: int, data: bytes, content_type: str):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode() or "{}")

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-KEY")
                self.end_headers()

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path in ("/", "/display", "/display/"):
                    return self._serve_file(DISPLAY_DIR / "index.html", "text/html; charset=utf-8")
                if path.startswith("/display/"):
                    name = path[len("/display/") :]
                    if ".." in name or name.startswith("/"):
                        return self._json(400, {"error": "bad path"})
                    target = DISPLAY_DIR / name
                    if not target.is_file():
                        return self._json(404, {"error": "not found"})
                    ctype = "text/css" if name.endswith(".css") else "application/javascript" if name.endswith(".js") else "application/octet-stream"
                    if name.endswith(".html"):
                        ctype = "text/html; charset=utf-8"
                    return self._serve_file(target, ctype)

                if path == "/mock/state":
                    return self._json(
                        200,
                        {
                            "sources": lab.sources.snapshot(),
                            "display": lab.display.state().as_dict(),
                            "mqtt_ok": lab.mqtt_ok,
                            "mqtt_error": lab.mqtt_error,
                            "panel": {"w": PANEL_W, "h": PANEL_H, "product": PRODUCT_NAME},
                        },
                    )
                if path == "/mock/display/state":
                    return self._json(200, lab.display.state().as_dict())

                # UniFi classic Network endpoints used by host/unifi.py
                if "/proxy/network/api/s/" in path and path.endswith("/stat/sta"):
                    return self._json(200, {"data": lab.sources.list_clients(), "meta": {"rc": "ok"}})
                if "/proxy/network/api/s/" in path and path.endswith("/stat/alarm"):
                    return self._json(200, {"data": [], "meta": {"rc": "ok"}})

                # Shelly Gen2 RPC (ip field = host:port/shelly/<id>)
                if path.startswith("/shelly/") and path.endswith("/rpc/Switch.GetStatus"):
                    device_id = path.split("/")[2]
                    try:
                        ch = int((qs.get("id") or ["0"])[0])
                    except ValueError:
                        ch = 0
                    if ch != 0:
                        return self._json(400, {"error": "only channel 0 mocked"})
                    try:
                        return self._json(200, lab.sources.shelly_status(device_id))
                    except KeyError:
                        return self._json(404, {"error": f"unknown shelly {device_id}"})

                if path.startswith("/shelly/") and path.endswith("/rpc/Switch.Set"):
                    device_id = path.split("/")[2]
                    on_raw = (qs.get("on") or ["false"])[0].lower()
                    on = on_raw in ("1", "true", "yes")
                    try:
                        return self._json(200, lab.sources.shelly_set(device_id, on))
                    except KeyError:
                        return self._json(404, {"error": f"unknown shelly {device_id}"})

                self._json(404, {"error": "not found", "path": path})

            def do_POST(self):
                parsed = urlparse(self.path)
                path = parsed.path
                body = self._read_json()

                if path == "/mock/scenario/reset":
                    lab.sources.reset_home()
                    return self._json(200, {"ok": True, "scenario": "reset"})
                if path == "/mock/scenario/arrive":
                    return self._json(200, lab.sources.scenario_arrive())
                if path == "/mock/scenario/unknown":
                    return self._json(200, lab.sources.scenario_unknown())
                if path == "/mock/scenario/empty_heater":
                    return self._json(200, lab.sources.scenario_empty_heater())
                if path == "/mock/display/inject":
                    topic = body.get("topic")
                    payload = body.get("payload") or {}
                    if not topic:
                        return self._json(400, {"error": "topic required"})
                    return self._json(200, lab.inject_display(topic, payload))
                if path == "/mock/decision":
                    pid = body.get("id")
                    decision = body.get("decision")
                    if not pid or decision not in ("approve", "deny"):
                        return self._json(400, {"error": "id and decision=approve|deny required"})
                    try:
                        signed = lab.publish_decision(str(pid), decision)
                    except Exception as e:
                        return self._json(503, {"error": str(e), "hint": "start Mosquitto (docker compose up -d)"})
                    return self._json(200, {"ok": True, "published": signed})

                # UniFi write commands (gateway)
                if "/proxy/network/api/s/" in path and "/cmd/" in path:
                    manager = path.rsplit("/cmd/", 1)[-1]
                    cmd = body.get("cmd")
                    if manager == "stamgr" and cmd == "block-sta":
                        return self._json(200, lab.sources.block(body.get("mac", "")))
                    if manager == "stamgr" and cmd == "unblock-sta":
                        return self._json(200, lab.sources.unblock(body.get("mac", "")))
                    if manager == "hotspot" and cmd == "create-voucher":
                        return self._json(
                            200,
                            {
                                "meta": {"rc": "ok"},
                                "data": [{"code": "MOCK-VOUCHER", "note": body.get("note", ""), "duration": body.get("expire")}],
                            },
                        )
                    return self._json(400, {"error": f"unsupported cmd {manager}/{cmd}"})

                self._json(404, {"error": "not found", "path": path})

            def _serve_file(self, path: Path, content_type: str):
                if not path.is_file():
                    return self._json(404, {"error": "missing display file"})
                return self._bytes(200, path.read_bytes(), content_type)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.serve_forever()

    def start_background(self, host: str = MOCK_BIND, port: int = MOCK_PORT):
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"host": host, "port": port}, daemon=True)
        self._thread.start()
        time.sleep(0.15)
        return self
