"""SQLite access for the host services. Timestamps are UTC `YYYY-MM-DD HH:MM:SS`."""

import datetime as dt
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMA = Path(__file__).with_name("schema.sql")


def load_env():
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")


def db_path():
    try:
        return os.environ["NETWATCH_DB"]
    except KeyError:
        raise RuntimeError("NETWATCH_DB is not set") from None


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(value):
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)


def hour_key(now=None):
    """Local hour bucket. Set the host clock to Europe/Berlin."""
    return (now or dt.datetime.now()).strftime("%Y-%m-%dT%H")


def hour_of_day(now=None):
    return (now or dt.datetime.now()).hour


@contextmanager
def conn():
    raw = sqlite3.connect(db_path(), timeout=10)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys=ON")
    try:
        yield raw
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init_db():
    path = Path(db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path)
    try:
        raw.executescript(SCHEMA.read_text())
        raw.commit()
    finally:
        raw.close()


def audit(c, actor, what, detail=None):
    c.execute(
        "INSERT INTO audit(ts, actor, what, detail) VALUES(?,?,?,?)",
        (now_iso(), actor, what, json.dumps(detail) if detail is not None else None),
    )


def emit(c, type_, source, mac=None, device_id=None, details=None, cooldown_min=60):
    """Insert an event unless the same type+subject fired within the cooldown."""
    subject = mac or device_id
    recent = c.execute(
        "SELECT 1 FROM events WHERE type=? AND COALESCE(mac, device_id)=? "
        "AND ts > datetime('now', ?)",
        (type_, subject, f"-{int(cooldown_min)} minutes"),
    ).fetchone()
    if recent:
        return None
    eid = "evt_" + uuid.uuid4().hex[:10]
    c.execute(
        "INSERT INTO events(id, ts, type, source, mac, device_id, details) VALUES(?,?,?,?,?,?,?)",
        (eid, now_iso(), type_, source, mac, device_id, json.dumps(details or {})),
    )
    return eid


if __name__ == "__main__":
    load_env()
    init_db()
    print(f"initialized {db_path()}")
