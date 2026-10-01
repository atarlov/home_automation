"""The only HTTP surface the sandbox can reach. It never sees UniFi or Shelly credentials."""

import hmac
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import paho.mqtt.publish as mqtt_pub
from fastapi import FastAPI, Header, HTTPException
from pydantic import ValidationError

import db
from models import Triage

CLAIM_STALE_MIN = 10


def token():
    return os.environ["NETWATCH_API_TOKEN"]


def mqtt_auth():
    return {"username": os.environ["MQTT_USER_API"], "password": os.environ["MQTT_PASS_API"]}


def require_auth(header):
    expected = f"Bearer {token()}"
    if (
        not isinstance(header, str)
        or len(header) != len(expected)
        or not hmac.compare_digest(header, expected)
    ):
        raise HTTPException(status_code=401)


def publish(topic, payload, retain=False):
    mqtt_pub.single(
        topic,
        json.dumps(payload),
        hostname=os.environ["MQTT_HOST"],
        port=int(os.environ.get("MQTT_PORT", 1883)),
        auth=mqtt_auth(),
        retain=retain,
    )


def pattern_for(ev, dev):
    vendor = ((dev["vendor"] if dev else "") or "unknown")
    vlan = ((dev["vlan"] if dev else "") or "-")
    return f"{ev['type']}/{vlan}/{vendor.lower().split()[0]}"


def person_name(c, person_id):
    if not person_id:
        return None
    row = c.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
    return row["name"] if row else None


def presence(c):
    rows = c.execute(
        "SELECT p.name, d.mac, d.online, d.last_seen FROM people p "
        "LEFT JOIN devices d ON d.person_id=p.id AND d.presence_device=1"
    ).fetchall()
    return [dict(r) for r in rows]


def baseline(c, mac):
    hour = db.hour_of_day()
    row = c.execute(
        "SELECT avg_tx_mb, avg_rx_mb, samples FROM baselines WHERE mac=? AND hour_of_day=?",
        (mac, hour),
    ).fetchone()
    today = c.execute(
        "SELECT tx_mb, rx_mb FROM traffic_hourly WHERE mac=? AND hour=?",
        (mac, db.hour_key()),
    ).fetchone()
    if not row and not today:
        return None
    out = {"hour_of_day": hour}
    if row:
        out.update(dict(row))
    if today:
        out["tx_mb"] = today["tx_mb"]
        out["rx_mb"] = today["rx_mb"]
    return out


def away_digest(c, ev):
    """Events and executed proposals since this device's last departure.

    Cumulative Shelly watt-hours are not stored yet, so this digest does not invent them.
    """
    if not ev["mac"]:
        return None
    dep = c.execute(
        "SELECT ts FROM events WHERE type='departure' AND mac=? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (ev["mac"], ev["ts"]),
    ).fetchone()
    if not dep:
        return None
    since = dep["ts"]
    events = [
        _event_row(r)
        for r in c.execute(
            "SELECT id, ts, type, details FROM events WHERE mac=? AND ts>=? AND id!=? ORDER BY ts",
            (ev["mac"], since, ev["id"]),
        )
    ]
    executed = []
    for r in c.execute(
        "SELECT p.id, p.ts, p.action, p.status FROM proposals p "
        "JOIN events e ON e.id=p.event_id "
        "WHERE e.mac=? AND p.status='executed' AND p.ts>=? ORDER BY p.ts",
        (ev["mac"], since),
    ):
        item = dict(r)
        item["action"] = json.loads(item["action"])
        executed.append(item)
    return {"since": since, "events": events, "executed": executed}


def _event_row(row):
    item = dict(row)
    item["details"] = json.loads(item.get("details") or "{}")
    return item


def build_context(c, eid):
    ev = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    if not ev:
        raise HTTPException(status_code=404, detail="unknown event")
    dev = c.execute("SELECT * FROM devices WHERE mac=?", (ev["mac"],)).fetchone() if ev["mac"] else None
    pat = pattern_for(ev, dev)
    shelly_rows = [
        dict(r)
        for r in c.execute(
            'SELECT device_id, name, on_state AS "on", power_w, idle_w, never_switch_off, comfort_auto FROM shelly'
        )
    ]
    routines = []
    if ev["type"] == "arrival":
        routines = [
            {"type": "shelly_switch", "params": {"device_id": r["device_id"], "channel": 0, "on": True}}
            for r in c.execute("SELECT device_id FROM shelly WHERE comfort_auto=1")
        ]
    ctx = {
        "now": db.now_iso(),
        "local_hour": db.hour_of_day(),
        "event": {
            "id": ev["id"],
            "type": ev["type"],
            "source": ev["source"],
            "mac": ev["mac"],
            "device_id": ev["device_id"],
            "details": json.loads(ev["details"] or "{}"),
        },
        "device": None,
        "baseline": None,
        "recent_events": [
            _event_row(r)
            for r in c.execute(
                "SELECT id, ts, type, details FROM events WHERE COALESCE(mac, device_id)=COALESCE(?, ?) "
                "AND id!=? ORDER BY ts DESC LIMIT 5",
                (ev["mac"], ev["device_id"], eid),
            )
        ],
        "past_decisions": [],
        "presence": presence(c),
        "house": {"shelly": shelly_rows, "empty": _house_empty_flag(c)},
        "away_digest": away_digest(c, ev) if ev["type"] == "arrival" else None,
        "arrival_routines": routines,
        "owner_preferences": {
            "never_switch_off": [r[0] for r in c.execute("SELECT device_id FROM shelly WHERE never_switch_off=1")],
            "notes": [r[0] for r in c.execute("SELECT note FROM memory_notes WHERE mac IS NULL ORDER BY ts DESC LIMIT 10")],
        },
    }
    for r in c.execute(
        "SELECT p.pattern, p.action, d.decision AS owner, d.ts AS at FROM proposals p "
        "JOIN decisions d ON d.proposal_id=p.id WHERE p.pattern=? ORDER BY d.ts DESC LIMIT 5",
        (pat,),
    ):
        item = dict(r)
        item["action"] = json.loads(item["action"])
        ctx["past_decisions"].append(item)
    if dev:
        ctx["device"] = {
            k: dev[k]
            for k in (
                "mac",
                "friendly_name",
                "vendor",
                "hostname",
                "vlan",
                "expected_vlan",
                "first_seen",
                "trusted",
            )
        }
        ctx["device"]["owner"] = person_name(c, dev["person_id"])
        ctx["device"]["notes"] = [
            r[0]
            for r in c.execute(
                "SELECT note FROM memory_notes WHERE mac=? ORDER BY ts DESC LIMIT 5",
                (dev["mac"],),
            )
        ]
        ctx["baseline"] = baseline(c, dev["mac"])
    return ctx


def _house_empty_flag(c):
    # Imported lazily so api can be used without pulling the collector loop.
    import collector
    return collector.house_is_empty(c)


def check_targets(t, ctx):
    """Reject actions aimed at a MAC or Shelly that this event's context does not contain."""
    action = t.action
    params = action.params or {}
    event = ctx["event"]
    if action.type == "none":
        return
    if action.type in ("quarantine_client", "block_client", "unblock_client"):
        mac = params.get("mac")
        device_mac = (ctx.get("device") or {}).get("mac")
        if not mac or mac != device_mac:
            raise HTTPException(status_code=422, detail="mac is not in context")
        return
    if action.type == "shelly_switch":
        device_id = params.get("device_id")
        known = {s["device_id"] for s in ctx["house"]["shelly"]}
        routine_ids = {r["params"]["device_id"] for r in ctx.get("arrival_routines") or []}
        allowed = routine_ids | ({event.get("device_id")} if event.get("device_id") else set())
        if device_id not in known or device_id not in allowed:
            raise HTTPException(status_code=422, detail="device_id is not in context")
        if not isinstance(params.get("on"), bool):
            raise HTTPException(status_code=422, detail="shelly_switch requires boolean on")
        if params["on"] is False and device_id in ctx["owner_preferences"]["never_switch_off"]:
            raise HTTPException(status_code=422, detail="never_switch_off")
        return
    if action.type == "guest_voucher":
        try:
            minutes = int(params["minutes"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="guest_voucher requires minutes") from None
        if not 1 <= minutes <= 24 * 60:
            raise HTTPException(status_code=422, detail="voucher minutes out of range")
        return
    raise HTTPException(status_code=422, detail="unsupported action")


def validate_decision(t):
    if t.decision in ("notify", "propose", "greet") and t.oled is None:
        raise HTTPException(status_code=422, detail="oled required")
    if t.decision in ("ignore", "log") and t.action.type != "none":
        raise HTTPException(status_code=422, detail="ignore and log cannot carry an action")
    if t.decision == "propose" and t.action.type == "none":
        raise HTTPException(status_code=422, detail="propose requires an action")


@asynccontextmanager
async def lifespan(_app):
    db.load_env()
    db.init_db()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/events/pending")
def pending(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    with db.conn() as c:
        c.execute(
            "UPDATE events SET status='pending', claimed_at=NULL "
            "WHERE status='claimed' AND claimed_at < datetime('now', ?)",
            (f"-{CLAIM_STALE_MIN} minutes",),
        )
        rows = c.execute(
            "SELECT id, type, ts FROM events WHERE status='pending' ORDER BY ts LIMIT 10"
        ).fetchall()
        claimed_at = db.now_iso()
        c.executemany(
            "UPDATE events SET status='claimed', claimed_at=? WHERE id=?",
            [(claimed_at, r["id"]) for r in rows],
        )
        return [dict(r) for r in rows]


@app.get("/events/{eid}/context")
def context(eid: str, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    with db.conn() as c:
        return build_context(c, eid)


@app.post("/events/{eid}/triage")
def submit(eid: str, body: dict, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    try:
        triage = Triage.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json())) from None
    validate_decision(triage)

    with db.conn() as c:
        ev = c.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            raise HTTPException(status_code=404, detail="unknown event")
        if ev["status"] == "done":
            raise HTTPException(status_code=409, detail="already triaged")
        ctx = build_context(c, eid)
        check_targets(triage, ctx)
        c.execute(
            "INSERT OR REPLACE INTO triage(event_id, ts, decision, result) VALUES(?,?,?,?)",
            (eid, db.now_iso(), triage.decision, triage.model_dump_json()),
        )
        c.execute("UPDATE events SET status='done' WHERE id=?", (eid,))
        if triage.memory_note:
            c.execute(
                "INSERT INTO memory_notes(ts, mac, note) VALUES(?,?,?)",
                (db.now_iso(), (ctx["device"] or {}).get("mac"), triage.memory_note),
            )
        if triage.action.type != "none" and triage.decision in ("propose", "greet"):
            pid = "prp_" + uuid.uuid4().hex[:10]
            dev = None
            if ctx["device"]:
                dev = c.execute("SELECT * FROM devices WHERE mac=?", (ctx["device"]["mac"],)).fetchone()
            stored = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
            c.execute(
                "INSERT INTO proposals(id, event_id, ts, action, pattern, oled, expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    pid,
                    eid,
                    db.now_iso(),
                    triage.action.model_dump_json(),
                    pattern_for(stored, dev),
                    triage.oled.model_dump_json() if triage.oled else None,
                    time.time() + int(os.environ.get("PROPOSAL_TTL_SEC", 120)),
                ),
            )
            publish("netwatch/internal/proposal", {"id": pid})
        elif triage.oled and triage.decision in ("notify", "greet"):
            publish("netwatch/notify", {"event_id": eid, **triage.oled.model_dump()})
        db.audit(
            c,
            "agent",
            "triage",
            {"event": eid, "decision": triage.decision, "suspicious": triage.suspicious_input},
        )
    return {"ok": True}


@app.post("/events/{eid}/fail")
def fail(eid: str, body: dict, authorization: str | None = Header(default=None)):
    require_auth(authorization)
    with db.conn() as c:
        row = c.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="unknown event")
        if row["status"] == "done":
            raise HTTPException(status_code=409, detail="already triaged")
        c.execute("UPDATE events SET status='failed' WHERE id=?", (eid,))
        db.audit(c, "agent", "triage_failed", {"event": eid, "reason": str(body.get("reason"))[:300]})
    return {"ok": True}


@app.get("/summary/data")
def summary_data(authorization: str | None = Header(default=None)):
    require_auth(authorization)
    with db.conn() as c:
        return {
            "triage_24h": [
                dict(r)
                for r in c.execute(
                    "SELECT t.ts, e.type, t.decision, json_extract(t.result,'$.summary_line') AS line "
                    "FROM triage t JOIN events e ON e.id=t.event_id "
                    "WHERE t.ts > datetime('now','-24 hours')"
                )
            ],
            "proposals_24h": [
                dict(r)
                for r in c.execute(
                    "SELECT ts, action, status FROM proposals WHERE ts > datetime('now','-24 hours')"
                )
            ],
            "devices_online": c.execute("SELECT COUNT(*) FROM devices WHERE online=1").fetchone()[0],
        }


@app.post("/status")
def status(body: dict, authorization: str | None = Header(default=None)):
    """Retained idle-screen text for the OLED."""
    require_auth(authorization)
    lines = [str(line)[:21] for line in body.get("lines", [])][:4]
    if not lines or not all(line.isascii() for line in lines):
        raise HTTPException(status_code=422, detail="ASCII only")
    publish("netwatch/status", {"lines": lines}, retain=True)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    db.load_env()
    host, port = os.environ.get("NETWATCH_API_BIND", "127.0.0.1:8765").rsplit(":", 1)
    uvicorn.run(app, host=host, port=int(port))
