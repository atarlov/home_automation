"""Nightly baselines from the last 14 days of traffic_hourly. Pure SQL, no model."""

import datetime as dt

import db


def rollup(c, days=14):
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H")
    c.execute("DELETE FROM baselines")
    c.execute(
        """
        INSERT INTO baselines(mac, hour_of_day, avg_tx_mb, avg_rx_mb, samples)
        SELECT mac,
               CAST(substr(hour, 12, 2) AS INTEGER),
               AVG(tx_mb),
               AVG(rx_mb),
               COUNT(*)
        FROM traffic_hourly
        WHERE hour >= ?
        GROUP BY mac, CAST(substr(hour, 12, 2) AS INTEGER)
        """,
        (cutoff,),
    )
    return c.execute("SELECT COUNT(*) FROM baselines").fetchone()[0]


if __name__ == "__main__":
    db.load_env()
    db.init_db()
    with db.conn() as c:
        n = rollup(c)
    print(f"baselines updated: {n}")
