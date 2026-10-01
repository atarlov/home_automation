"""Poll UniFi and Shelly, record diffs, and emit pending events.

Traffic hour buckets follow the host's local clock; set that clock to Europe/Berlin.
"""

import datetime as dt
import os
import time

import db
import shelly
import unifi

POLL_SEC = 30


def absence_min():
    return int(os.environ.get("ARRIVAL_ABSENCE_MIN", 20))


def minutes_since(iso):
    return (dt.datetime.now(dt.timezone.utc) - db.parse_ts(iso)).total_seconds() / 60


def poll_unifi(c):
    seen = {x["mac"]: x for x in unifi.list_clients()}
    known = {r["mac"]: r for r in c.execute("SELECT * FROM devices")}
    now = db.now_iso()
    hour = db.hour_key()

    for mac, x in seen.items():
        d = known.get(mac)
        name = x.get("name") or x.get("hostname")
        vlan = x.get("network")
        tx_bytes = x.get("tx_bytes") or 0
        rx_bytes = x.get("rx_bytes") or 0
        if d is None:
            c.execute(
                "INSERT INTO devices(mac, vendor, hostname, vlan, first_seen, last_seen, online, last_ap, "
                "last_tx_bytes, last_rx_bytes) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (
                    mac,
                    x.get("oui"),
                    name,
                    vlan,
                    now,
                    now,
                    x.get("ap_mac"),
                    tx_bytes,
                    rx_bytes,
                ),
            )
            db.emit(
                c,
                "new_client",
                "unifi",
                mac=mac,
                details={"hostname": name, "vendor": x.get("oui"), "vlan": vlan, "ap": x.get("ap_mac")},
            )
            continue

        if not d["online"] and d["presence_device"] and d["last_seen"] and minutes_since(d["last_seen"]) >= absence_min():
            db.emit(
                c,
                "arrival",
                "unifi",
                mac=mac,
                cooldown_min=absence_min(),
                details={"away_min": round(minutes_since(d["last_seen"])), "ap": x.get("ap_mac")},
            )

        if d["expected_vlan"] and vlan != d["expected_vlan"]:
            db.emit(
                c,
                "vlan_mismatch",
                "unifi",
                mac=mac,
                details={"vlan": vlan, "expected": d["expected_vlan"]},
            )

        # Counters reset on reconnect, so a negative delta is treated as zero.
        dtx = max(0, tx_bytes - (d["last_tx_bytes"] or 0)) / 1e6
        drx = max(0, rx_bytes - (d["last_rx_bytes"] or 0)) / 1e6
        c.execute(
            "INSERT INTO traffic_hourly(mac, hour, tx_mb, rx_mb) VALUES(?,?,?,?) "
            "ON CONFLICT(mac, hour) DO UPDATE SET tx_mb=tx_mb+excluded.tx_mb, rx_mb=rx_mb+excluded.rx_mb",
            (mac, hour, dtx, drx),
        )
        check_spike(c, mac)

        c.execute(
            "UPDATE devices SET hostname=?, vlan=?, last_seen=?, online=1, last_ap=?, "
            "last_tx_bytes=?, last_rx_bytes=? WHERE mac=?",
            (name, vlan, now, x.get("ap_mac"), tx_bytes, rx_bytes, mac),
        )

    for mac, d in known.items():
        if d["online"] and mac not in seen:
            c.execute("UPDATE devices SET online=0 WHERE mac=?", (mac,))


def check_spike(c, mac):
    h = db.hour_of_day()
    cur = c.execute(
        "SELECT tx_mb, rx_mb FROM traffic_hourly WHERE mac=? AND hour=?",
        (mac, db.hour_key()),
    ).fetchone()
    base = c.execute(
        "SELECT avg_tx_mb, avg_rx_mb, samples FROM baselines WHERE mac=? AND hour_of_day=?",
        (mac, h),
    ).fetchone()
    if not cur or not base or base["samples"] < 3:
        return
    # Confirm tx/rx direction on the console (AP-perspective vs client-perspective).
    for field, avg in (("tx_mb", base["avg_tx_mb"]), ("rx_mb", base["avg_rx_mb"])):
        if cur[field] > max(50, 5 * (avg or 0)):
            db.emit(
                c,
                "traffic_spike",
                "unifi",
                mac=mac,
                details={
                    "field": field,
                    "current_mb": round(cur[field], 1),
                    "baseline_mb": round(avg or 0, 1),
                },
            )


def presence_rows(c):
    return c.execute(
        "SELECT d.mac, d.online, d.last_seen, p.name FROM devices d "
        "JOIN people p ON p.id=d.person_id WHERE d.presence_device=1"
    ).fetchall()


def house_is_empty(c):
    rows = presence_rows(c)
    if not rows:
        return False
    return all(
        (not r["online"]) and r["last_seen"] and minutes_since(r["last_seen"]) >= absence_min()
        for r in rows
    )


def check_presence(c):
    """Departure and house_empty, debounced by the absence window."""
    rows = presence_rows(c)
    if not rows:
        return
    away = [
        r for r in rows
        if (not r["online"]) and r["last_seen"] and minutes_since(r["last_seen"]) >= absence_min()
    ]
    for r in away:
        db.emit(
            c,
            "departure",
            "unifi",
            mac=r["mac"],
            cooldown_min=12 * 60,
            details={"person": r["name"]},
        )
    if len(away) == len(rows):
        db.emit(c, "house_empty", "system", device_id="house", cooldown_min=12 * 60)


def poll_shelly(c):
    empty = house_is_empty(c)
    for s in c.execute("SELECT * FROM shelly").fetchall():
        try:
            st = shelly.status(s["ip"])
        except Exception as e:
            db.audit(c, "collector", "shelly_unreachable", {"device_id": s["device_id"], "err": str(e)})
            continue
        c.execute(
            "UPDATE shelly SET on_state=?, power_w=?, updated=? WHERE device_id=?",
            (int(bool(st["on"])), st["power_w"], db.now_iso(), s["device_id"]),
        )
        power = st["power_w"] or 0
        idle = s["idle_w"] or 1
        if empty and st["on"] and not s["never_switch_off"] and power > max(20, 5 * idle):
            db.emit(
                c,
                "shelly_power",
                "shelly",
                device_id=s["device_id"],
                cooldown_min=120,
                details={"power_w": st["power_w"], "idle_w": s["idle_w"], "name": s["name"]},
            )


def main():
    db.load_env()
    db.init_db()
    while True:
        with db.conn() as c:
            try:
                poll_unifi(c)
                check_presence(c)
                poll_shelly(c)
            except Exception as e:
                db.audit(c, "collector", "poll_error", {"err": str(e)})
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
