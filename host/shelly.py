"""Shelly switches. Cloud Control API when SHELLY_CLOUD_* is set, otherwise LAN RPC.

Cloud docs: https://shelly-api-docs.shelly.cloud/cloud-control-api/
The cloud allows about one request per second. One all_status call covers every plug.
"""

import os

import httpx


class ShellyCloudError(RuntimeError):
    pass


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


def cloud_host():
    return os.environ.get("SHELLY_CLOUD_HOST", "").strip().rstrip("/")


def cloud_key():
    return os.environ.get("SHELLY_CLOUD_AUTH_KEY", "").strip()


def cloud_enabled():
    return bool(cloud_host() and cloud_key())


def _cloud_get(path, params):
    query = {"auth_key": cloud_key(), **params}
    response = httpx.get(f"{cloud_host()}{path}", params=query, timeout=20)
    if response.status_code >= 300:
        raise ShellyCloudError(f"Shelly cloud {response.status_code}: {response.text[:240]}")
    body = response.json()
    if isinstance(body, dict) and body.get("isok") is False:
        raise ShellyCloudError(f"Shelly cloud rejected the request: {body!r}"[:300])
    return body


def _cloud_post(path, payload):
    response = httpx.post(
        f"{cloud_host()}{path}",
        params={"auth_key": cloud_key()},
        json=payload,
        timeout=20,
    )
    if response.status_code >= 300:
        raise ShellyCloudError(f"Shelly cloud {response.status_code}: {response.text[:240]}")
    if response.content:
        body = response.json()
        if isinstance(body, dict) and body.get("isok") is False:
            raise ShellyCloudError(f"Shelly cloud rejected the request: {body!r}"[:300])
        return body
    return {"ok": True}


def switch_from_status(status, channel=0):
    """Normalize Gen2 switch:N and Gen1 relays/meters into on + power_w."""
    if not isinstance(status, dict):
        return None
    gen2 = status.get(f"switch:{channel}")
    if isinstance(gen2, dict):
        return {"on": bool(gen2.get("output")), "power_w": gen2.get("apower")}
    relays = status.get("relays") or []
    if channel < len(relays) and isinstance(relays[channel], dict):
        meters = status.get("meters") or []
        power = None
        if channel < len(meters) and isinstance(meters[channel], dict):
            power = meters[channel].get("power")
        return {"on": bool(relays[channel].get("ison")), "power_w": power}
    return None


def _devices_status_map(body):
    if not isinstance(body, dict):
        return {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    devices = data.get("devices_status") if isinstance(data, dict) else None
    return devices if isinstance(devices, dict) else {}


def cloud_all_status():
    """One cloud read of every device on the account. Keys are Shelly device ids."""
    body = _cloud_get("/device/all_status", {"show_info": "true", "no_shared": "true"})
    found = {}
    for device_id, raw in _devices_status_map(body).items():
        if not isinstance(raw, dict):
            continue
        info = raw.get("_dev_info") if isinstance(raw.get("_dev_info"), dict) else {}
        found[str(device_id)] = {"id": str(device_id), "gen": info.get("gen"), "code": info.get("code"), **reading_from_status(raw, info)}
    return found


def reading_from_status(raw, info=None):
    """Switch, room climate, and motion fields from one cloud status object."""
    info = info or {}
    switch = switch_from_status(raw, 0)
    temp = humidity = lux = motion = battery = None
    if switch is None:
        tmp = raw.get("tmp") if isinstance(raw.get("tmp"), dict) else {}
        if tmp.get("is_valid", True):
            temp = tmp.get("tC", tmp.get("value"))
        hum = raw.get("hum") if isinstance(raw.get("hum"), dict) else {}
        if hum.get("is_valid", True):
            humidity = hum.get("value")
    motion_block = raw.get("motion:0")
    if isinstance(motion_block, dict) and "motion" in motion_block:
        motion = int(bool(motion_block["motion"]))
    light = raw.get("illuminance:0")
    if isinstance(light, dict):
        lux = light.get("lux")
    bat = raw.get("bat") if isinstance(raw.get("bat"), dict) else None
    if bat is None:
        power = raw.get("devicepower:0")
        battery_block = power.get("battery") if isinstance(power, dict) else None
        if isinstance(battery_block, dict):
            bat = {"value": battery_block.get("percent")}
    if isinstance(bat, dict):
        battery = bat.get("value")
    online = info.get("online")
    if online is None and isinstance(raw.get("cloud"), dict):
        online = raw["cloud"].get("connected")
    return {
        "on": None if switch is None else switch["on"],
        "power_w": None if switch is None else switch["power_w"],
        "temp_c": temp,
        "humidity": humidity,
        "lux": lux,
        "motion": motion,
        "battery": battery,
        "online": online,
    }


def cloud_set_switch(device_id, channel, on):
    return _cloud_post(
        "/v2/devices/api/set/switch",
        {"id": str(device_id), "channel": int(channel), "on": bool(on)},
    )


def status_for(device_id, ip, channel=0):
    if cloud_enabled():
        states = cloud_all_status()
        state = states.get(str(device_id))
        if not state or state["on"] is None:
            raise ShellyCloudError(f"no switch status for {device_id}")
        return {"on": state["on"], "power_w": state["power_w"]}
    return status(ip, channel)


def set_switch_for(device_id, ip, channel, on):
    if cloud_enabled():
        return cloud_set_switch(device_id, channel, on)
    return set_switch(ip, channel, on)
