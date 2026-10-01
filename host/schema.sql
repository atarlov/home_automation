PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  mac TEXT PRIMARY KEY,
  friendly_name TEXT, vendor TEXT, hostname TEXT,
  vlan TEXT, expected_vlan TEXT,
  first_seen TEXT, last_seen TEXT,
  online INTEGER DEFAULT 0, last_ap TEXT,
  trusted INTEGER DEFAULT 0,
  person_id INTEGER REFERENCES people(id),
  presence_device INTEGER DEFAULT 0,        -- 1 = counts for arrival/departure
  last_tx_bytes INTEGER, last_rx_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS traffic_hourly (
  mac TEXT, hour TEXT, tx_mb REAL DEFAULT 0, rx_mb REAL DEFAULT 0,
  PRIMARY KEY (mac, hour)
);

CREATE TABLE IF NOT EXISTS baselines (          -- filled nightly by rollup.py
  mac TEXT, hour_of_day INTEGER,
  avg_tx_mb REAL, avg_rx_mb REAL, samples INTEGER,
  PRIMARY KEY (mac, hour_of_day)
);

CREATE TABLE IF NOT EXISTS shelly (
  device_id TEXT PRIMARY KEY, name TEXT, ip TEXT, kind TEXT,
  on_state INTEGER, power_w REAL, idle_w REAL,
  never_switch_off INTEGER DEFAULT 0,
  comfort_auto INTEGER DEFAULT 0,             -- may be switched ON without approval
  updated TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, ts TEXT, type TEXT, source TEXT,
  mac TEXT, device_id TEXT, details TEXT,
  status TEXT DEFAULT 'pending',              -- pending | claimed | done | failed
  claimed_at TEXT
);

CREATE INDEX IF NOT EXISTS events_status ON events(status, ts);

CREATE TABLE IF NOT EXISTS triage (
  event_id TEXT PRIMARY KEY REFERENCES events(id),
  ts TEXT, decision TEXT, result TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, event_id TEXT, ts TEXT,
  action TEXT, tier TEXT, pattern TEXT, oled TEXT,
  expires_at REAL,
  status TEXT DEFAULT 'pending'   -- pending|approved|denied|expired|executed|failed|rejected
);

CREATE TABLE IF NOT EXISTS decisions (
  proposal_id TEXT PRIMARY KEY, ts TEXT, source TEXT, decision TEXT
);

CREATE TABLE IF NOT EXISTS memory_notes (
  id INTEGER PRIMARY KEY, ts TEXT, mac TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, what TEXT, detail TEXT
);
