"""Durable deduplication and intent recording for a single worker process."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, app TEXT NOT NULL, torrent TEXT NOT NULL UNIQUE,
                    entity_id INTEGER NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    plan TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
                );
            """)
            # Deluge's added_time has whole-second precision.
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('managed_since', ?)", (str(int(time.time())),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def since(self):
        with self.connect() as db:
            return float(db.execute("SELECT value FROM metadata WHERE key='managed_since'").fetchone()[0])

    def metadata(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_metadata(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def enqueue(self, app, torrent, entity_id, size):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(app,torrent,entity_id,size,created,updated) VALUES (?,?,?,?,?,?)",
                       (app, torrent, entity_id, size, now, now))
            row = dict(db.execute("SELECT * FROM jobs WHERE torrent=?", (torrent,)).fetchone())
            if row["app"] != app or row["entity_id"] != entity_id:
                raise ValueError("torrent already belongs to another application or title")
            return row

    def update(self, job_id, **fields):
        allowed = {"status", "plan", "error", "attempts", "next_attempt"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid job update")
        fields["updated"] = time.time()
        if isinstance(fields.get("plan"), dict):
            fields["plan"] = json.dumps(fields["plan"])
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                       (*fields.values(), job_id))

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def ready(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','moving','resuming') AND next_attempt<=? ORDER BY id LIMIT 20",
                (time.time(),))]

    def jobs(self, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def reservations(self, disk, exclude):
        # Conservatively reserve full sizes until torrents finish. This may
        # temporarily double-count already-written bytes, but cannot overbook.
        with self.connect() as db:
            rows = db.execute("SELECT plan FROM jobs WHERE id!=? AND status IN ('moving','resuming','done','blocked') AND plan IS NOT NULL", (exclude,))
            plans = [json.loads(r[0]) for r in rows]
        return sum(p["size"] for p in plans if p["disk"] == disk)

    def downloading(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status='done'")]
