"""Long-running house agent. NVIDIA decides; the display speaks; the gateway acts."""

import hashlib
import hmac
import json
import os
import time

import db
import gateway
import house
from nvidia_client import NvidiaError, NvidiaModel

SYSTEM = """You are the house agent. You run all day for Assen. UniFi tells you who is home. Shelly plugs are the lights and appliances. The T-Display-S3 on the wall is how you speak: four short lines beside a face. When you have nothing to say, leave the screen alone — it already shows a waiting face.

When an arrival event says Assen is home, greet him and propose shelly_switch on for every shelly with comfort_auto. That runs immediately, with no tap.

While the house is empty, a plug that is drawing real power and is not never_switch_off can be proposed off. The display asks, and a tap decides. Never propose switching off a never_switch_off device.

An unknown client is mentioned with show. Do not block it unless a note says to. Blocking, unblocking, quarantine, and guest vouchers always wait for a tap.

Every event you are given must be closed with propose or dismiss. propose closes the event. Use remember when something should still be true tomorrow. Use only MAC addresses and device ids that appear in the house snapshot or the event. Display lines are ASCII, at most 4, at most 18 characters. Button labels are at most 8 characters.

If there are no events, update the idle screen only when it should change. Otherwise do nothing.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "show",
            "description": "Replace the idle screen. Fails while a question is waiting for a tap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 4,
                    }
                },
                "required": ["lines"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose",
            "description": "Ask the gateway to perform one action and close the event. Comfort switches run immediately. Everything else waits for a tap on the display.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "action_type": {
                        "type": "string",
                        "enum": [
                            "shelly_switch",
                            "block_client",
                            "unblock_client",
                            "quarantine_client",
                            "guest_voucher",
                        ],
                    },
                    "device_id": {"type": "string"},
                    "mac": {"type": "string"},
                    "on": {"type": "boolean"},
                    "minutes": {"type": "integer"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "button_a": {"type": "string"},
                    "button_b": {"type": "string"},
                },
                "required": ["event_id", "action_type", "lines"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dismiss",
            "description": "Close an event when nothing should happen.",
            "parameters": {
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a short note the next session can read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                    "mac": {"type": "string"},
                },
                "required": ["note"],
            },
        },
    },
]


def _action_from(arguments):
    params = {}
    nested = arguments.get("params")
    if isinstance(nested, dict):
        params.update(nested)
    for key in ("device_id", "mac", "on", "channel", "minutes", "note"):
        if arguments.get(key) is not None:
            params[key] = arguments[key]
    return {"type": arguments.get("action_type"), "params": params}


def dispatch(call, sink):
    args = call.arguments or {}
    try:
        if call.name == "show":
            return house.show(args.get("lines"), sink)
        if call.name == "dismiss":
            return house.dismiss(str(args.get("event_id") or ""))
        if call.name == "remember":
            return house.remember(args.get("note"), args.get("mac"))
        if call.name == "propose":
            return house.propose(
                str(args.get("event_id") or ""),
                _action_from(args),
                args.get("lines"),
                args.get("button_a") or "Yes",
                args.get("button_b") or "No",
                sink,
            )
    except Exception as exc:
        return {"error": str(exc)}
    return {"error": f"unknown tool {call.name}"}


def _user_message(events, snap):
    if events:
        lead = "New events. Close each one with propose or dismiss."
    else:
        lead = "No new events. Glance at the house."
    return lead + "\n\nEvents:\n" + json.dumps(events, default=str) + "\n\nHouse:\n" + json.dumps(snap, default=str)


def _assistant_message(completion):
    message = {"role": "assistant", "content": completion.content or ""}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in completion.tool_calls
        ]
    return message


def run_turn(model, sink, *, quiet=False):
    ready = getattr(model, "ensure_ready", None)
    if ready:
        ready()
    events = [] if quiet else house.claim_pending()
    event_ids = [event["id"] for event in events]
    if not events and not quiet:
        return {"skipped": True}
    try:
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _user_message(events, house.snapshot())},
        ]
        content = None
        for _ in range(6):
            completion = model.complete(messages, TOOLS)
            if not completion.tool_calls:
                content = completion.content
                break
            messages.append(_assistant_message(completion))
            for call in completion.tool_calls:
                result = dispatch(call, sink)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
        else:
            house.release(event_ids)
            _log("limit", "tool limit")
            return {"error": "tool limit", "events": event_ids}
        house.close_leftovers(event_ids)
        _log("turn", content or "")
        return {"content": content, "events": event_ids}
    except Exception:
        house.release(event_ids)
        raise


def sign_decision(proposal_id, decision, ts=None):
    ts = int(time.time() if ts is None else ts)
    secret = os.environ["NETWATCH_HMAC_SECRET"].encode()
    digest = hmac.new(secret, f"{proposal_id}|{decision}|{ts}".encode(), hashlib.sha256).hexdigest()
    return {"id": proposal_id, "decision": decision, "ts": ts, "hmac": digest}


def handle_button(sink, proposal_id, decision, now=None):
    if decision not in ("approve", "deny"):
        return "rejected"
    now = time.time() if now is None else now
    with db.conn() as c:
        status = gateway.process_decision(c, sign_decision(proposal_id, decision, ts=int(now)), now=now)
    if status == "rejected":
        return status
    sink.publish("netwatch/resolved", json.dumps({"id": proposal_id, "status": status}))
    if status == "executed":
        sink.publish("netwatch/status", json.dumps({"lines": ["Done"]}))
    elif status == "denied":
        sink.publish("netwatch/status", json.dumps({"lines": ["Left it"]}))
    return status


def _log(kind, content):
    with db.conn() as c:
        c.execute(
            "INSERT INTO agent_turns(ts, kind, content) VALUES(?,?,?)",
            (db.now_iso(), kind, (content or "")[:2000]),
        )


def main():
    db.load_env()
    db.init_db()
    from display_serial import DisplayLink

    link = DisplayLink()
    link.open()
    time.sleep(1)
    link.publish("netwatch/status", json.dumps({"lines": []}))
    model = NvidiaModel()
    quiet_sec = int(os.environ.get("AGENT_QUIET_SEC", 900))
    last_quiet = time.time()
    while True:
        try:
            for press in link.poll():
                handle_button(link, press["id"], press["decision"])
            pending = house.pending_count()
            quiet_due = (time.time() - last_quiet) >= quiet_sec
            if pending or quiet_due:
                outcome = run_turn(model, link, quiet=quiet_due and not pending)
                if quiet_due and not pending:
                    last_quiet = time.time()
                if outcome.get("content"):
                    print(outcome["content"], flush=True)
        except NvidiaError as exc:
            print(exc, flush=True)
            link.publish("netwatch/status", json.dumps({"lines": ["Need NVIDIA key", "set it in .env"]}))
            time.sleep(30)
            continue
        except Exception as exc:
            print(f"agent error: {exc}", flush=True)
            with db.conn() as c:
                db.audit(c, "agent", "turn_failed", {"err": str(exc)})
        time.sleep(2)


if __name__ == "__main__":
    main()
