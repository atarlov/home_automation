"""What the agent is allowed to know and to ask for. Writes go through the gateway."""

import json
import os
import time
import uuid

import collector
import db
import gateway
from face import clean_button, clean_lines

CLAIM_STALE_MIN = 10
_ACTIONS = {
    "quarantine_client",
    "block_client",
    "unblock_client",
    "shelly_switch",
    "guest_voucher",
}


def _reset_stale(c):
    c.execute(
        "UPDATE events SET status='pending', claimed_at=NULL "
        "WHERE status='claimed' AND claimed_at < datetime('now', ?)",
        (f"-{CLAIM_STALE_MIN} minutes",),
    )


def pending_count():
    with db.conn() as c:
        _reset_stale(c)
        return c.execute("SELECT COUNT(*) FROM events WHERE status='pending'").fetchone()[0]


def claim_pending(limit=4):
    with db.conn() as c:
        _reset_stale(c)
        rows = c.execute(
            "SELECT * FROM events WHERE status='pending' ORDER BY ts LIMIT ?",
            (limit,),
        ).fetchall()
        claimed_at = db.now_iso()
        c.executemany(
            "UPDATE events SET status='claimed', claimed_at=? WHERE id=?",
            [(claimed_at, row["id"]) for row in rows],
        )
        return [_event(row) for row in rows]


def release(event_ids):
    if not event_ids:
        return
    with db.conn() as c:
        c.executemany(
            "UPDATE events SET status='pending', claimed_at=NULL WHERE id=? AND status='claimed'",
            [(event_id,) for event_id in event_ids],
        )


def close_leftovers(event_ids):
    if not event_ids:
        return
    with db.conn() as c:
        for event_id in event_ids:
            cur = c.execute(
                "UPDATE events SET status='done' WHERE id=? AND status='claimed'",
                (event_id,),
            )
            if cur.rowcount:
                db.audit(c, "agent", "left_open", {"event": event_id})


def snapshot():
    with db.conn() as c:
        people = [
            dict(row)
            for row in c.execute(
                "SELECT p.name, d.mac, d.online, d.last_seen FROM people p "
                "LEFT JOIN devices d ON d.person_id=p.id AND d.presence_device=1"
            )
        ]
        shelly = [
            {
                "device_id": row["device_id"],
                "name": row["name"],
                "on": bool(row["on_state"]),
                "power_w": row["power_w"],
                "idle_w": row["idle_w"],
                "comfort_auto": bool(row["comfort_auto"]),
                "never_switch_off": bool(row["never_switch_off"]),
            }
            for row in c.execute(
                "SELECT device_id, name, on_state, power_w, idle_w, comfort_auto, never_switch_off FROM shelly"
            )
        ]
        online = [
            {
                "mac": row["mac"],
                "name": row["friendly_name"] or row["hostname"],
                "vlan": row["vlan"],
                "trusted": bool(row["trusted"]),
                "presence": bool(row["presence_device"]),
            }
            for row in c.execute(
                "SELECT mac, friendly_name, hostname, vlan, trusted, presence_device "
                "FROM devices WHERE online=1 ORDER BY last_seen DESC LIMIT 30"
            )
        ]
        notes = [
            row["note"]
            for row in c.execute("SELECT note FROM memory_notes ORDER BY ts DESC LIMIT 8")
        ]
        recent = [
            _event(row)
            for row in c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 8")
        ]
        return {
            "now": db.now_iso(),
            "house_empty": collector.house_is_empty(c),
            "people": people,
            "shelly": shelly,
            "online": online,
            "notes": notes,
            "recent": recent,
        }


def remember(note, mac=None):
    text = " ".join(str(note or "").split())[:300]
    if not text:
        return {"error": "empty note"}
    with db.conn() as c:
        c.execute(
            "INSERT INTO memory_notes(ts, mac, note) VALUES(?,?,?)",
            (db.now_iso(), mac, text),
        )
        db.audit(c, "agent", "remember", {"mac": mac, "note": text})
    return {"ok": True}


def dismiss(event_id):
    with db.conn() as c:
        row = c.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return {"error": "unknown event"}
        if row["status"] == "done":
            return {"error": "event already closed"}
        c.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))
        db.audit(c, "agent", "dismiss", {"event": event_id})
    return {"ok": True}


def show(lines, sink):
    face = getattr(sink, "face", None)
    if face is not None and face.mode == "ask":
        return {"error": "display is waiting for a tap"}
    cleaned = clean_lines(lines)
    if not cleaned:
        return {"error": "lines required"}
    sink.publish("netwatch/status", json.dumps({"lines": cleaned}))
    return {"ok": True, "lines": cleaned}


def propose(event_id, action, lines, button_a="Yes", button_b="No", sink=None):
    cleaned = clean_lines(lines)
    if not cleaned:
        return {"error": "lines required"}
    kind = (action or {}).get("type")
    params = (action or {}).get("params") or {}
    if kind not in _ACTIONS:
        return {"error": f"unsupported action {kind}"}
    error = _check_action(kind, params)
    if error:
        return {"error": error}

    button_a = clean_button(button_a, "Yes")
    button_b = clean_button(button_b, "No")
    pid = "prp_" + uuid.uuid4().hex[:10]
    oled = {"lines": cleaned, "button_a": button_a, "button_b": button_b}
    with db.conn() as c:
        ev = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            return {"error": "unknown event"}
        if ev["status"] == "done":
            return {"error": "event already closed"}
        c.execute(
            "INSERT INTO proposals(id, event_id, ts, action, pattern, oled, expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                pid,
                event_id,
                db.now_iso(),
                json.dumps({"type": kind, "params": params}),
                ev["type"],
                json.dumps(oled),
                time.time() + int(os.environ.get("PROPOSAL_TTL_SEC", 120)),
            ),
        )
        c.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))
        db.audit(c, "agent", "propose", {"event": event_id, "id": pid, "action": kind})
    gateway.handle_proposal(sink, pid)
    with db.conn() as c:
        row = c.execute("SELECT tier, status FROM proposals WHERE id=?", (pid,)).fetchone()
    return {"id": pid, "tier": row["tier"], "status": row["status"], "lines": cleaned}


def _check_action(kind, params):
    if kind in ("quarantine_client", "block_client", "unblock_client"):
        mac = params.get("mac")
        if not mac:
            return "mac required"
        with db.conn() as c:
            if not c.execute("SELECT 1 FROM devices WHERE mac=?", (mac,)).fetchone():
                return "mac is not a known device"
        return None
    if kind == "shelly_switch":
        device_id = params.get("device_id")
        if not isinstance(params.get("on"), bool):
            return "shelly_switch requires boolean on"
        with db.conn() as c:
            row = c.execute(
                "SELECT never_switch_off FROM shelly WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if not row:
            return "unknown shelly"
        if params["on"] is False and row["never_switch_off"]:
            return "never_switch_off"
        return None
    if kind == "guest_voucher":
        try:
            minutes = int(params["minutes"])
        except (KeyError, TypeError, ValueError):
            return "guest_voucher requires minutes"
        if not 1 <= minutes <= 24 * 60:
            return "voucher minutes out of range"
        return None
    return "unsupported action"


def _event(row):
    details = {}
    if row["details"]:
        try:
            details = json.loads(row["details"])
        except json.JSONDecodeError:
            details = {"raw": row["details"]}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "type": row["type"],
        "source": row["source"],
        "mac": row["mac"],
        "device_id": row["device_id"],
        "status": row["status"],
        "details": details,
    }
