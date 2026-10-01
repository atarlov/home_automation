"""UniFi Network helpers. Write functions are imported only by the gateway."""

import os

import httpx


def _base():
    return os.environ["UNIFI_URL"].rstrip("/")


def _site():
    return os.environ.get("UNIFI_SITE", "default")


def _client(key):
    # The console cert is commonly self-signed on the LAN.
    return httpx.Client(
        base_url=_base(),
        verify=False,
        timeout=10,
        headers={"X-API-KEY": key, "Accept": "application/json"},
    )


def list_clients():
    with _client(os.environ["UNIFI_READ_KEY"]) as c:
        r = c.get(f"/proxy/network/api/s/{_site()}/stat/sta")
        r.raise_for_status()
        return r.json()["data"]


def list_alarms():
    with _client(os.environ["UNIFI_READ_KEY"]) as c:
        r = c.get(f"/proxy/network/api/s/{_site()}/stat/alarm", params={"archived": "false"})
        r.raise_for_status()
        return r.json()["data"]


def _cmd(manager, payload):
    with _client(os.environ["UNIFI_WRITE_KEY"]) as c:
        r = c.post(f"/proxy/network/api/s/{_site()}/cmd/{manager}", json=payload)
        r.raise_for_status()
        return r.json()


def block(mac):
    return _cmd("stamgr", {"cmd": "block-sta", "mac": mac})


def unblock(mac):
    return _cmd("stamgr", {"cmd": "unblock-sta", "mac": mac})


def create_voucher(minutes, note):
    return _cmd(
        "hotspot",
        {"cmd": "create-voucher", "expire": minutes, "n": 1, "quota": 1, "note": note[:40]},
    )
