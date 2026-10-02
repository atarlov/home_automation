"""Action tiers, HMAC checks, expiry, and the only process allowed to change the network."""

import hashlib
import hmac
import json
import os
import time

import paho.mqtt.client as mqtt

import db
import shelly
import unifi


def tier_for(action, c):
    kind = action["type"]
    params = action.get("params") or {}
    if kind == "shelly_switch":
        row = c.execute(
            "SELECT comfort_auto, never_switch_off FROM shelly WHERE device_id=?",
            (params.get("device_id"),),
        ).fetchone()
        if not row:
            return "reject"
        if params.get("on") is False and row["never_switch_off"]:
            return "reject"
        if params.get("on") is True and row["comfort_auto"]:
            return "comfort"
        return "approve"
    if kind == "guest_voucher":
        return "approve"
    if kind in ("quarantine_client", "block_client", "unblock_client"):
        return "secure"
    if kind == "acknowledge":
        return "ack"
    return "reject"


def execute(action, c):
    kind = action["type"]
    params = action.get("params") or {}
    if kind == "block_client":
        return unifi.block(params["mac"])
    if kind == "unblock_client":
        return unifi.unblock(params["mac"])
    if kind == "quarantine_client":
        # MVP: block the client. A quarantine VLAN can replace this later.
        return unifi.block(params["mac"])
    if kind == "guest_voucher":
        return unifi.create_voucher(int(params["minutes"]), params.get("note", ""))
    if kind == "shelly_switch":
        row = c.execute(
            "SELECT ip, never_switch_off FROM shelly WHERE device_id=?",
            (params["device_id"],),
        ).fetchone()
        if not row:
            raise ValueError(f"unknown shelly {params['device_id']}")
        if params.get("on") is False and row["never_switch_off"]:
            raise ValueError("never_switch_off")
        return shelly.set_switch_for(
            params["device_id"],
            row["ip"],
            int(params.get("channel", 0)),
            bool(params["on"]),
        )
    raise ValueError(f"unknown action {kind}")


def valid_sig(msg, now=None):
    now = time.time() if now is None else now
    try:
        pid = str(msg["id"])
        decision = str(msg["decision"])
        ts = int(msg["ts"])
        given = str(msg.get("hmac", ""))
    except (KeyError, TypeError, ValueError):
        return False
    if decision not in ("approve", "deny"):
        return False
    secret = os.environ["NETWATCH_HMAC_SECRET"].encode()
    expected = hmac.new(secret, f"{pid}|{decision}|{ts}".encode(), hashlib.sha256).hexdigest()
    if len(given) != len(expected) or not hmac.compare_digest(expected, given):
        return False
    return abs(now - ts) < 60


def run(c, pid, action, source):
    try:
        result = execute(action, c)
        c.execute("UPDATE proposals SET status='executed' WHERE id=?", (pid,))
        db.audit(
            c,
            "gateway",
            "executed",
            {"id": pid, "action": action, "by": source, "result": str(result)[:300]},
        )
        return "executed"
    except Exception as e:
        c.execute("UPDATE proposals SET status='failed' WHERE id=?", (pid,))
        db.audit(c, "gateway", "exec_failed", {"id": pid, "err": str(e)})
        return "failed"


def process_decision(c, msg, now=None):
    now = time.time() if now is None else now
    pid = msg.get("id")
    if not valid_sig(msg, now=now):
        db.audit(c, "gateway", "decision_rejected", {"id": pid, "reason": "bad signature"})
        return "rejected"
    row = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not row or now > row["expires_at"]:
        db.audit(c, "gateway", "decision_rejected", {"id": pid, "reason": "missing or expired"})
        return "rejected"
    decision = msg["decision"]
    new_status = "approved" if decision == "approve" else "denied"
    cur = c.execute(
        "UPDATE proposals SET status=? WHERE id=? AND status='pending' AND expires_at>=?",
        (new_status, pid, now),
    )
    if cur.rowcount != 1:
        db.audit(c, "gateway", "decision_rejected", {"id": pid, "reason": "not pending"})
        return "rejected"
    c.execute(
        "INSERT INTO decisions(proposal_id, ts, source, decision) VALUES(?,?,?,?)",
        (pid, db.now_iso(), "esp32", decision),
    )
    action = json.loads(row["action"])
    if action.get("type") == "acknowledge":
        c.execute("UPDATE proposals SET status='executed' WHERE id=?", (pid,))
        db.audit(c, "gateway", "acknowledged", {"id": pid, "decision": decision})
        return "acked"
    if decision == "deny":
        db.audit(c, "gateway", "denied", {"id": pid})
        return "denied"
    return run(c, pid, json.loads(row["action"]), source="esp32")


def expire_pending(c, now=None):
    now = time.time() if now is None else now
    rows = c.execute(
        "SELECT id FROM proposals WHERE status='pending' AND tier IS NOT NULL AND expires_at < ?",
        (now,),
    ).fetchall()
    expired = []
    for row in rows:
        cur = c.execute(
            "UPDATE proposals SET status='expired' WHERE id=? AND status='pending'",
            (row["id"],),
        )
        if cur.rowcount:
            expired.append(row["id"])
    return expired


def handle_proposal(cli, pid, now=None):
    now = time.time() if now is None else now
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM proposals WHERE id=? AND status='pending'",
            (pid,),
        ).fetchone()
        if not row:
            return
        if row["expires_at"] < now:
            c.execute("UPDATE proposals SET status='expired' WHERE id=? AND status='pending'", (pid,))
            cli.publish("netwatch/resolved", json.dumps({"id": pid, "status": "expired"}), qos=1)
            return
        action = json.loads(row["action"])
        tier = tier_for(action, c)
        c.execute("UPDATE proposals SET tier=? WHERE id=?", (tier, pid))
        if tier == "reject":
            c.execute("UPDATE proposals SET status='rejected' WHERE id=? AND status='pending'", (pid,))
            db.audit(c, "gateway", "rejected", {"id": pid, "action": action})
            return
        if tier == "comfort":
            cur = c.execute(
                "UPDATE proposals SET status='approved' WHERE id=? AND status='pending'",
                (pid,),
            )
            if cur.rowcount != 1:
                return
            if row["oled"]:
                oled = json.loads(row["oled"])
                cli.publish(
                    "netwatch/notify",
                    json.dumps({"event_id": row["event_id"], **oled}),
                    qos=1,
                )
            outcome = run(c, pid, action, source="policy")
            shown = "approve" if outcome == "executed" else outcome
            cli.publish("netwatch/resolved", json.dumps({"id": pid, "status": shown}), qos=1)
            return
        oled = (
            json.loads(row["oled"])
            if row["oled"]
            else {"lines": ["Action requested"], "button_a": "Yes", "button_b": "No"}
        )
        cli.publish(
            "netwatch/proposal",
            json.dumps({"id": pid, "tier": tier, "expires_at": int(row["expires_at"]), **oled}),
            qos=1,
        )


def reconcile(cli):
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, status, action FROM proposals WHERE status IN ('pending', 'approved')"
        ).fetchall()
    for row in rows:
        if row["status"] == "pending":
            handle_proposal(cli, row["id"])
            continue
        action = json.loads(row["action"])
        if action.get("type") == "guest_voucher":
            with db.conn() as c:
                db.audit(c, "gateway", "restart_needs_attention", {"id": row["id"]})
            continue
        with db.conn() as c:
            current = c.execute(
                "SELECT action FROM proposals WHERE id=? AND status='approved'",
                (row["id"],),
            ).fetchone()
            if current:
                run(c, row["id"], json.loads(current["action"]), source="restart")


def on_internal_proposal(cli, msg):
    handle_proposal(cli, json.loads(msg)["id"])


def on_decision(cli, msg):
    body = json.loads(msg)
    with db.conn() as c:
        status = process_decision(c, body)
    if status == "rejected":
        return
    shown = "approve" if status == "executed" else status
    cli.publish("netwatch/resolved", json.dumps({"id": body.get("id"), "status": shown}), qos=1)


ROUTES = {
    "netwatch/internal/proposal": on_internal_proposal,
    "netwatch/decision": on_decision,
}


def _accepted(reason_code):
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return not getattr(reason_code, "is_failure", True)


def on_connect(cli, _userdata, _flags, reason_code, _properties):
    if not _accepted(reason_code):
        print(f"gateway mqtt refused: {reason_code}", flush=True)
        return
    for topic in ROUTES:
        cli.subscribe(topic, qos=1)
    reconcile(cli)


def on_message(cli, _userdata, message):
    handler = ROUTES.get(message.topic)
    if not handler:
        return
    try:
        handler(cli, message.payload.decode())
    except Exception as e:
        print(f"gateway handler {message.topic} failed: {e}", flush=True)


def main():
    db.load_env()
    db.init_db()
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    cli.username_pw_set(os.environ["MQTT_USER_GATEWAY"], os.environ["MQTT_PASS_GATEWAY"])
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.reconnect_delay_set(min_delay=1, max_delay=30)
    while True:
        try:
            cli.connect(os.environ["MQTT_HOST"], int(os.environ.get("MQTT_PORT", 1883)), keepalive=30)
            break
        except Exception as e:
            print(f"gateway mqtt connect failed: {e}", flush=True)
            time.sleep(2)
    cli.loop_start()
    try:
        while True:
            with db.conn() as c:
                for pid in expire_pending(c):
                    cli.publish("netwatch/resolved", json.dumps({"id": pid, "status": "expired"}), qos=1)
            cli.publish("netwatch/heartbeat", json.dumps({"from": "gateway", "ts": int(time.time())}))
            time.sleep(10)
    finally:
        cli.loop_stop()


if __name__ == "__main__":
    main()
