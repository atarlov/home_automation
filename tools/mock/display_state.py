"""Reduce netwatch MQTT payloads into a T-Display-S3 AMOLED screen model."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from .constants import HEARTBEAT_STALE_SEC, MCU, PANEL_H, PANEL_W, PRODUCT_NAME


@dataclass
class ScreenState:
    mode: str  # offline | idle | notify | proposal
    lines: list[str] = field(default_factory=list)
    button_a: str | None = None
    button_b: str | None = None
    proposal_id: str | None = None
    tier: str | None = None
    expires_at: int | None = None
    product: str = PRODUCT_NAME
    mcu: str = MCU
    panel_w: int = PANEL_W
    panel_h: int = PANEL_H
    last_heartbeat_ts: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class DisplayReducer:
    """Apply MQTT messages in topic order; heartbeat staleness wins over content."""

    def __init__(self, heartbeat_stale_sec: int = HEARTBEAT_STALE_SEC):
        self.heartbeat_stale_sec = heartbeat_stale_sec
        self.status_lines: list[str] = ["Netwatch ready"]
        self.notify: dict | None = None
        self.proposal: dict | None = None
        self.last_heartbeat_ts: int | None = None

    def apply(self, topic: str, payload: dict, now: float | None = None) -> ScreenState:
        now = time.time() if now is None else now
        if topic == "netwatch/status":
            lines = payload.get("lines") or []
            self.status_lines = [str(x) for x in lines][:4] or ["Netwatch ready"]
        elif topic == "netwatch/notify":
            self.notify = dict(payload)
            self.proposal = None
        elif topic == "netwatch/proposal":
            self.proposal = dict(payload)
            self.notify = None
        elif topic == "netwatch/resolved":
            if self.proposal and str(self.proposal.get("id")) == str(payload.get("id")):
                self.proposal = None
            self.notify = None
        elif topic == "netwatch/heartbeat":
            try:
                self.last_heartbeat_ts = int(payload.get("ts", now))
            except (TypeError, ValueError):
                self.last_heartbeat_ts = int(now)
        return self.state(now=now)

    def state(self, now: float | None = None) -> ScreenState:
        now = time.time() if now is None else now
        if self.last_heartbeat_ts is None or (now - self.last_heartbeat_ts) > self.heartbeat_stale_sec:
            return ScreenState(
                mode="offline",
                lines=["offline", PRODUCT_NAME],
                last_heartbeat_ts=self.last_heartbeat_ts,
            )
        if self.proposal:
            p = self.proposal
            return ScreenState(
                mode="proposal",
                lines=[str(x) for x in (p.get("lines") or [])][:4],
                button_a=str(p.get("button_a") or "Yes")[:8],
                button_b=str(p.get("button_b") or "No")[:8],
                proposal_id=str(p.get("id")) if p.get("id") is not None else None,
                tier=str(p["tier"]) if p.get("tier") is not None else None,
                expires_at=int(p["expires_at"]) if p.get("expires_at") is not None else None,
                last_heartbeat_ts=self.last_heartbeat_ts,
            )
        if self.notify:
            n = self.notify
            return ScreenState(
                mode="notify",
                lines=[str(x) for x in (n.get("lines") or [])][:4],
                button_a=str(n.get("button_a") or "")[:8] or None,
                button_b=str(n.get("button_b") or "")[:8] or None,
                last_heartbeat_ts=self.last_heartbeat_ts,
            )
        return ScreenState(
            mode="idle",
            lines=list(self.status_lines)[:4],
            last_heartbeat_ts=self.last_heartbeat_ts,
        )
