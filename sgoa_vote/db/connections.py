"""Database access for the three-file store.

DELIBERATE DEVIATION FROM THE WORK ORDER (§2 "Storage" says WAL mode).

SQLite does not provide atomic commit across ATTACHed databases when any of them
is in WAL journalling mode; the multi-database master journal that makes such a
commit all-or-nothing only exists in rollback-journal modes.  A vote must write
the ledger (eligibility.db), the ballots (ballot.db) and the audit event
(audit.db) as one indivisible unit, so choosing WAL here would mean giving up
invariant I3/I4 and acceptance criterion AC-03.

Invariants outrank implementation details (work order §1), so this build uses a
rollback journal (DELETE) with synchronous=FULL.  At SGOA's scale -- around 36
entitlements and a few hundred writes across an entire meeting -- the cost is a
few milliseconds per transaction against a p95 budget of two seconds.  Readers
use their own connections and a 10 second busy timeout.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import threading
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent

DB_FILES = {
    "eligibility": "eligibility.db",
    "ballot": "ballot.db",
    "audit": "audit.db",
}

_SCHEMA_FILES = {
    "eligibility": "schema_eligibility.sql",
    "ballot": "schema_ballot.sql",
    "audit": "schema_audit.sql",
}


def _configure(conn: sqlite3.Connection, schemas=("main", "ballot", "audit")) -> None:
    for schema in schemas:
        # DELETE rather than TRUNCATE: TRUNCATE leaves a zero-length -journal
        # file behind, and on Windows the running server keeps a handle on it,
        # so a second connection (a scrutineer inspecting the file, a backup
        # tool) gets a disk I/O error on any schema read-write. DELETE removes
        # the journal at the end of each transaction and gives exactly the same
        # atomic multi-database commit.
        conn.execute(f"PRAGMA {schema}.journal_mode=DELETE")
        conn.execute(f"PRAGMA {schema}.synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")


class Database:
    """Owns one serialised writer connection and hands out reader connections.

    The writer opens eligibility.db as `main` and attaches the other two, so all
    SQL in the codebase can use the same qualified names everywhere:
    unqualified/`main` for eligibility tables, `ballot.` and `audit.` for the
    other two.  Readers attach identically.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = {name: self.data_dir / fn for name, fn in DB_FILES.items()}
        self._lock = threading.RLock()
        self._writer: sqlite3.Connection | None = None
        self._depth = 0
        self.init_schema()

    # -- setup ---------------------------------------------------------------

    def init_schema(self) -> None:
        for name, schema_file in _SCHEMA_FILES.items():
            sql = (SCHEMA_DIR / schema_file).read_text(encoding="utf-8")
            conn = sqlite3.connect(self.paths[name], isolation_level=None)
            try:
                _configure(conn, schemas=("main",))
                conn.executescript(sql)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False is safe here precisely because every write goes
        # through the single writer connection behind self._lock, and each reader
        # is created and closed inside one request.
        conn = sqlite3.connect(self.paths["eligibility"], isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for name in ("ballot", "audit"):
            conn.execute("ATTACH DATABASE ? AS " + name, (str(self.paths[name]),))
        _configure(conn)
        return conn

    # -- access --------------------------------------------------------------

    @property
    def writer_conn(self) -> sqlite3.Connection:
        if self._writer is None:
            self._writer = self._connect()
        return self._writer

    @contextlib.contextmanager
    def writer(self):
        """Serialised, all-or-nothing transaction spanning all three databases.

        Nested calls join the outer transaction rather than starting a new one,
        so a domain helper can be used standalone or inside a larger unit of work.
        """
        with self._lock:
            conn = self.writer_conn
            outermost = self._depth == 0
            if outermost:
                conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield conn
            except BaseException:
                self._depth -= 1
                if outermost:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    conn.execute("COMMIT")

    @contextlib.contextmanager
    def reader(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # -- maintenance ---------------------------------------------------------

    def backup_to(self, target_dir: Path) -> list[Path]:
        """Copy all three files while holding the writer lock.

        In rollback-journal mode an idle database leaves no side files to
        capture, so a plain copy taken between transactions is consistent.
        """
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        written = []
        with self._lock:
            for name, path in self.paths.items():
                dest = target_dir / DB_FILES[name]
                shutil.copy2(path, dest)
                written.append(dest)
        return written

    def integrity(self) -> dict[str, str]:
        out = {}
        with self._lock:
            for schema in ("main", "ballot", "audit"):
                row = self.writer_conn.execute(f"PRAGMA {schema}.integrity_check").fetchone()
                key = "eligibility" if schema == "main" else schema
                out[key] = row[0] if row else "unknown"
        return out

    def journal_modes(self) -> dict[str, str]:
        out = {}
        with self._lock:
            for schema in ("main", "ballot", "audit"):
                row = self.writer_conn.execute(f"PRAGMA {schema}.journal_mode").fetchone()
                key = "eligibility" if schema == "main" else schema
                out[key] = row[0] if row else "unknown"
        return out

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
