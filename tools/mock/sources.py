"""In-memory fake UniFi client list + Shelly Gen2 Switch RPC for the collector."""

from __future__ import annotations

import copy
import threading
from typing import Any

from .constants import AP_MAC, PHONE_MAC, UNKNOWN_MAC


def _client(
    mac: str,
    *,
    hostname: str,
    oui: str = "Mock",
    network: str = "LAN",
    tx_bytes: int = 0,
    rx_bytes: int = 0,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "mac": mac,
        "hostname": hostname,
        "name": name or hostname,
        "oui": oui,
        "network": network,
        "ap_mac": AP_MAC,
        "tx_bytes": tx_bytes,
        "rx_bytes": rx_bytes,
    }


class LabSources:
    """Mutable UniFi/Shelly responses shared by the fake HTTP server."""

    def __init__(self):
        self._lock = threading.RLock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.shellies: dict[str, dict[str, Any]] = {
            "hall": {"on": False, "power_w": 0.0},
            "fridge": {"on": True, "power_w": 90.0},
            "heater": {"on": False, "power_w": 0.0},
        }
        self.blocked: set[str] = set()
        self.reset_home()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "clients": copy.deepcopy(list(self.clients.values())),
                "shellies": copy.deepcopy(self.shellies),
                "blocked": sorted(self.blocked),
            }

    def list_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(c) for c in self.clients.values() if c["mac"] not in self.blocked]

    def shelly_status(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.shellies.get(device_id)
            if row is None:
                raise KeyError(device_id)
            return {"id": 0, "output": bool(row["on"]), "apower": float(row["power_w"]), "source": "mock"}

    def shelly_set(self, device_id: str, on: bool) -> dict[str, Any]:
        with self._lock:
            if device_id not in self.shellies:
                raise KeyError(device_id)
            self.shellies[device_id]["on"] = bool(on)
            if not on:
                self.shellies[device_id]["power_w"] = 0.0
            elif device_id == "fridge" and self.shellies[device_id]["power_w"] < 1:
                self.shellies[device_id]["power_w"] = 90.0
            elif device_id == "heater" and self.shellies[device_id]["power_w"] < 1:
                self.shellies[device_id]["power_w"] = 1800.0
            elif device_id == "hall" and self.shellies[device_id]["power_w"] < 1:
                self.shellies[device_id]["power_w"] = 5.0
            return {"was_on": not on, "id": 0}

    def block(self, mac: str) -> dict[str, Any]:
        with self._lock:
            self.blocked.add(mac.lower())
            self.clients.pop(mac.lower(), None)
            return {"meta": {"rc": "ok"}, "data": [{"mac": mac, "cmd": "block-sta"}]}

    def unblock(self, mac: str) -> dict[str, Any]:
        with self._lock:
            self.blocked.discard(mac.lower())
            return {"meta": {"rc": "ok"}, "data": [{"mac": mac, "cmd": "unblock-sta"}]}

    def reset_home(self) -> None:
        """Quiet house: phone away, heater off, fridge on, no unknowns."""
        with self._lock:
            self.clients = {}
            self.blocked.clear()
            self.shellies = {
                "hall": {"on": False, "power_w": 0.0},
                "fridge": {"on": True, "power_w": 90.0},
                "heater": {"on": False, "power_w": 0.0},
            }

    def scenario_arrive(self) -> dict[str, Any]:
        """Phone joins Wi-Fi (collector should emit arrival if DB last_seen is old)."""
        with self._lock:
            self.clients[PHONE_MAC] = _client(
                PHONE_MAC, hostname="assen-phone", oui="Apple", network="LAN", tx_bytes=1_000_000, rx_bytes=2_000_000
            )
            return {"ok": True, "scenario": "arrive", "mac": PHONE_MAC}

    def scenario_unknown(self) -> dict[str, Any]:
        """Unknown IoT client appears."""
        with self._lock:
            self.clients[UNKNOWN_MAC] = _client(
                UNKNOWN_MAC,
                hostname="esp-cam",
                oui="Espressif",
                network="IoT",
                tx_bytes=500_000,
                rx_bytes=100_000,
            )
            return {"ok": True, "scenario": "unknown", "mac": UNKNOWN_MAC}

    def scenario_empty_heater(self) -> dict[str, Any]:
        """Nobody home; heater draws ~1.8 kW (never_switch_off fridge stays on)."""
        with self._lock:
            self.clients.pop(PHONE_MAC, None)
            self.shellies["heater"] = {"on": True, "power_w": 1800.0}
            self.shellies["fridge"] = {"on": True, "power_w": 90.0}
            return {"ok": True, "scenario": "empty_heater", "heater_w": 1800.0}

    def set_client(self, client: dict[str, Any]) -> None:
        with self._lock:
            mac = client["mac"].lower()
            row = dict(client)
            row["mac"] = mac
            self.clients[mac] = row

    def remove_client(self, mac: str) -> None:
        with self._lock:
            self.clients.pop(mac.lower(), None)
