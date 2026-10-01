"""HMAC-SHA256 decision signing — same contract as tools/fake_button.py."""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_decision(proposal_id: str, decision: str, secret: str, ts: int | None = None) -> dict:
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny")
    ts = int(time.time()) if ts is None else int(ts)
    digest = hmac.new(
        secret.encode(),
        f"{proposal_id}|{decision}|{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"id": proposal_id, "decision": decision, "ts": ts, "hmac": digest}
