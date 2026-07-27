from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class TrafficStore:
    """Small SQLite time-series store fed by cumulative edge-router counters."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One SQLite connection per thread. The API calls these methods through
        # asyncio.to_thread, which reuses a small pool of worker threads, so
        # thread-local storage lets us keep a connection alive per worker
        # instead of opening a new one on every sample/query. sqlite3 forbids
        # sharing a single connection across threads, so a shared singleton is
        # not an option here.
        self._local = threading.local()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection = connection
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS counter_cursor (
                    source_key TEXT PRIMARY KEY, upload INTEGER NOT NULL, download INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traffic_bucket (
                    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, bucket_start INTEGER NOT NULL,
                    upload INTEGER NOT NULL DEFAULT 0, download INTEGER NOT NULL DEFAULT 0,
                    selections INTEGER NOT NULL DEFAULT 0, active_peak INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (entity_type, entity_id, bucket_start)
                );
                CREATE TABLE IF NOT EXISTS traffic_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
                    entity_type TEXT, entity_id TEXT, event_type TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS traffic_bucket_lookup ON traffic_bucket(entity_type, bucket_start);
                CREATE INDEX IF NOT EXISTS traffic_event_lookup ON traffic_event(created_at DESC);
                """
            )

    def sample(self, *, source_key: str, entity_type: str, entity_id: str, upload: int, download: int, active: int, selections: int = 0, now: int | None = None) -> None:
        now = now or int(time.time())
        bucket = now - (now % 60)
        with self._connect() as db:
            previous = db.execute("SELECT upload, download FROM counter_cursor WHERE source_key = ?", (source_key,)).fetchone()
            up_delta = max(0, upload - int(previous["upload"])) if previous else 0
            down_delta = max(0, download - int(previous["download"])) if previous else 0
            db.execute(
                "INSERT INTO counter_cursor(source_key, upload, download, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source_key) DO UPDATE SET upload=excluded.upload, download=excluded.download, updated_at=excluded.updated_at",
                (source_key, upload, download, now),
            )
            db.execute(
                "INSERT INTO traffic_bucket(entity_type, entity_id, bucket_start, upload, download, selections, active_peak) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, entity_id, bucket_start) DO UPDATE SET upload=upload+excluded.upload, download=download+excluded.download, selections=MAX(selections, excluded.selections), active_peak=MAX(active_peak, excluded.active_peak)",
                (entity_type, entity_id, bucket, up_delta, down_delta, selections, active),
            )

    def event(self, event_type: str, message: str, *, entity_type: str | None = None, entity_id: str | None = None, now: int | None = None) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO traffic_event(created_at, entity_type, entity_id, event_type, message) VALUES (?, ?, ?, ?, ?)", (now or int(time.time()), entity_type, entity_id, event_type, message))

    def overview(self, seconds: int) -> dict[str, Any]:
        since = int(time.time()) - seconds
        with self._connect() as db:
            total = db.execute("SELECT COALESCE(SUM(upload),0) upload, COALESCE(SUM(download),0) download, COALESCE(MAX(active_peak),0) active FROM traffic_bucket WHERE bucket_start >= ? AND entity_type IN ('port','pool')", (since,)).fetchone()
            series = db.execute("SELECT bucket_start, SUM(upload) upload, SUM(download) download FROM traffic_bucket WHERE bucket_start >= ? AND entity_type IN ('port','pool') GROUP BY bucket_start ORDER BY bucket_start", (since,)).fetchall()
        return {"upload": int(total["upload"]), "download": int(total["download"]), "active": int(total["active"]), "series": [dict(row) for row in series]}

    def entities(self, entity_type: str, seconds: int) -> list[dict[str, Any]]:
        since = int(time.time()) - seconds
        with self._connect() as db:
            rows = db.execute("SELECT entity_id, SUM(upload) upload, SUM(download) download, MAX(active_peak) active, MAX(bucket_start) last_activity FROM traffic_bucket WHERE entity_type = ? AND bucket_start >= ? GROUP BY entity_id ORDER BY (SUM(upload)+SUM(download)) DESC", (entity_type, since)).fetchall()
        return [dict(row) for row in rows]

    def detail(self, entity_type: str, entity_id: str, seconds: int) -> dict[str, Any]:
        since = int(time.time()) - seconds
        with self._connect() as db:
            rows = db.execute("SELECT bucket_start, upload, download, selections, active_peak FROM traffic_bucket WHERE entity_type=? AND entity_id=? AND bucket_start>=? ORDER BY bucket_start", (entity_type, entity_id, since)).fetchall()
        upload = sum(int(row["upload"]) for row in rows)
        download = sum(int(row["download"]) for row in rows)
        return {"entity_type": entity_type, "entity_id": entity_id, "upload": upload, "download": download, "series": [dict(row) for row in rows]}

    def events(self, limit: int = 100, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT created_at, entity_type, entity_id, event_type, message FROM traffic_event"
        values: list[Any] = []
        filters = []
        if entity_type:
            filters.append("entity_type = ?")
            values.append(entity_type)
        if entity_id:
            filters.append("entity_id = ?")
            values.append(entity_id)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY id DESC LIMIT ?"
        values.append(max(1, min(limit, 500)))
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, values).fetchall()]
