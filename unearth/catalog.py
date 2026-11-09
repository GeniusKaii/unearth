"""SQLite catalogue: the single source of truth for what was found.

One row per recovered candidate.  Everything the album shows, every filter and
every export goes through this database, so a scan can be resumed, re-triaged
or re-exported without touching the disk again.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Iterator

from . import util

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS source (
    id        INTEGER PRIMARY KEY,
    spec      TEXT NOT NULL,
    kind      TEXT,
    label     TEXT,
    size      INTEGER,
    sector    INTEGER,
    note      TEXT,
    created   TEXT
);

CREATE TABLE IF NOT EXISTS scan (
    id        INTEGER PRIMARY KEY,
    source_id INTEGER,
    target    TEXT,
    profile   TEXT,
    range_start INTEGER,
    range_end   INTEGER,
    chunk     INTEGER,
    cursor    INTEGER DEFAULT 0,
    started   TEXT,
    finished  TEXT,
    status    TEXT,
    bytes     INTEGER DEFAULT 0,
    files     INTEGER DEFAULT 0,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS file (
    id        INTEGER PRIMARY KEY,
    scan_id   INTEGER,
    src       TEXT,
    kind      TEXT,
    fmt       TEXT,
    ext       TEXT,
    off       INTEGER,
    size      INTEGER,
    complete  INTEGER DEFAULT 1,
    name      TEXT,
    path      TEXT,
    sha256    TEXT,
    width     INTEGER,
    height    INTEGER,
    duration  REAL,
    captured  TEXT,
    day       TEXT,
    make      TEXT,
    model     TEXT,
    gps_lat   REAL,
    gps_lon   REAL,
    orientation INTEGER,
    score     INTEGER DEFAULT 0,
    dup_group INTEGER DEFAULT 0,
    thumb     TEXT,
    note      TEXT,
    created   TEXT
);

CREATE INDEX IF NOT EXISTS ix_file_off  ON file(off);
CREATE INDEX IF NOT EXISTS ix_file_day  ON file(day);
CREATE INDEX IF NOT EXISTS ix_file_kind ON file(kind);
CREATE INDEX IF NOT EXISTS ix_file_sha  ON file(sha256);
CREATE INDEX IF NOT EXISTS ix_file_scan ON file(scan_id);

CREATE TABLE IF NOT EXISTS tag (
    file_id INTEGER NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (file_id, tag)
);
"""

FILE_COLUMNS = (
    "scan_id", "src", "kind", "fmt", "ext", "off", "size", "complete", "name",
    "path", "sha256", "width", "height", "duration", "captured", "day", "make",
    "model", "gps_lat", "gps_lon", "orientation", "score", "dup_group", "thumb",
    "note", "created",
)


class Catalog:
    def __init__(self, path: str, readonly: bool = False):
        self.path = os.path.abspath(path)
        util.ensure_dir(os.path.dirname(self.path) or ".")
        if readonly:
            uri = "file:%s?mode=ro" % self.path.replace("\\", "/")
            self.conn = sqlite3.connect(uri, uri=True)
        else:
            self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        if not readonly:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def commit(self) -> None:
        self.conn.commit()

    # -- sources / scans -----------------------------------------------------
    def add_source(self, spec: str, kind: str, label: str = "", size: int = 0,
                   sector: int = 512, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO source(spec, kind, label, size, sector, note, created) "
            "VALUES(?,?,?,?,?,?,?)",
            (spec, kind, label, size, sector, note, util.now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def begin_scan(self, source_id: int, target: str, profile: str,
                   start: int, end: int, chunk: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO scan(source_id, target, profile, range_start, range_end, "
            "chunk, cursor, started, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, target, profile, start, end, chunk, start, util.now_iso(), "running"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def checkpoint(self, scan_id: int, cursor: int, files: int) -> None:
        self.conn.execute(
            "UPDATE scan SET cursor=?, bytes=?, files=? WHERE id=?",
            (cursor, cursor, files, scan_id),
        )
        self.conn.commit()

    def finish_scan(self, scan_id: int, status: str = "done", note: str = "") -> None:
        self.conn.execute(
            "UPDATE scan SET status=?, finished=?, note=? WHERE id=?",
            (status, util.now_iso(), note, scan_id),
        )
        self.conn.commit()

    def last_open_scan(self, target: str, profile: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scan WHERE target=? AND profile=? AND status='running' "
            "ORDER BY id DESC LIMIT 1", (target, profile),
        ).fetchone()

    # -- files ---------------------------------------------------------------
    def add_file(self, **fields) -> int:
        fields.setdefault("created", util.now_iso())
        cols = [c for c in FILE_COLUMNS if c in fields]
        sql = "INSERT INTO file(%s) VALUES(%s)" % (
            ",".join(cols), ",".join("?" * len(cols)),
        )
        cur = self.conn.execute(sql, tuple(fields[c] for c in cols))
        return int(cur.lastrowid)

    def update_file(self, file_id: int, **fields) -> None:
        cols = [c for c in fields if c in FILE_COLUMNS or c in ("id",)]
        if not cols:
            return
        sql = "UPDATE file SET %s WHERE id=?" % ",".join("%s=?" % c for c in cols)
        self.conn.execute(sql, tuple(fields[c] for c in cols) + (file_id,))

    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM file WHERE id=?", (file_id,)).fetchone()

    def iter_files(self, kind: str | None = None, order: str = "id") -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM file"
        params = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        sql += " ORDER BY " + order
        yield from self.conn.execute(sql, params)

    def count_files(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM file").fetchone()
        return int(row[0]) if row else 0

    # -- statistics ----------------------------------------------------------
    def stats(self) -> dict:
        c = self.conn
        out = {"files": 0, "bytes": 0, "by_kind": {}, "by_fmt": {}, "by_day": {},
               "dups": 0, "complete": 0, "with_time": 0, "with_gps": 0}
        row = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM file").fetchone()
        if row:
            out["files"], out["bytes"] = int(row[0]), int(row[1])
        for row in c.execute("SELECT kind, COUNT(*) FROM file GROUP BY kind ORDER BY 2 DESC"):
            out["by_kind"][row[0] or "?"] = int(row[1])
        for row in c.execute("SELECT fmt, COUNT(*) FROM file GROUP BY fmt ORDER BY 2 DESC"):
            out["by_fmt"][row[0] or "?"] = int(row[1])
        for row in c.execute("SELECT day, COUNT(*) FROM file WHERE day<>'' "
                             "GROUP BY day ORDER BY 2 DESC LIMIT 12"):
            out["by_day"][row[0]] = int(row[1])
        out["dups"] = int(c.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM file "
            "WHERE sha256<>'' GROUP BY sha256 HAVING n>1)").fetchone()[0])
        out["complete"] = int(c.execute("SELECT COUNT(*) FROM file WHERE complete=1").fetchone()[0])
        out["with_time"] = int(c.execute("SELECT COUNT(*) FROM file WHERE day<>''").fetchone()[0])
        out["with_gps"] = int(c.execute(
            "SELECT COUNT(*) FROM file WHERE gps_lat IS NOT NULL").fetchone()[0])
        return out
