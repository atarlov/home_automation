"""Shelly Gen2+ RPC over HTTP. set_switch is for the gateway only."""

import httpx


def status(ip, ch=0):
    r = httpx.get(f"http://{ip}/rpc/Switch.GetStatus", params={"id": ch}, timeout=5)
    r.raise_for_status()
    body = r.json()
    return {"on": body.get("output"), "power_w": body.get("apower")}


def set_switch(ip, ch, on):
    r = httpx.get(
        f"http://{ip}/rpc/Switch.Set",
        params={"id": ch, "on": "true" if on else "false"},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()
