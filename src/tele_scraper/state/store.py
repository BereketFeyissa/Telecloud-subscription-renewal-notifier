"""Dedup state.

A notification is sent once per ``(recipient, channel, component_id, status, rung)`` and stays
suppressed until the status or the rung changes (CLAUDE.md §8.2).

Only the most recent record per ``(recipient, channel, component_id)`` is kept. That is what
makes the semantics "until it changes" rather than "once, ever": a component that recovers and
later degrades again will notify again.

If the store is unreachable the run fails. It does not fall back to sending everything - that
would turn a storage blip into an alert storm.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import TracebackType
from typing import Protocol, runtime_checkable

from tele_scraper.errors import StateStoreError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_members (
    digest_key   TEXT NOT NULL,
    ack_key      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    recorded_at  REAL NOT NULL,
    PRIMARY KEY (digest_key, ack_key)
);

CREATE TABLE IF NOT EXISTS known_components (
    component_id TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    last_seen    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS acknowledgements (
    ack_key      TEXT PRIMARY KEY,
    component_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    rung         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    acked_by     TEXT NOT NULL,
    acked_at     REAL NOT NULL,
    expires_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ack_component ON acknowledgements (component_id);

CREATE TABLE IF NOT EXISTS sent_notifications (
    scope        TEXT PRIMARY KEY,
    dedup_key    TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    channel      TEXT NOT NULL,
    component_id TEXT NOT NULL,
    status       TEXT NOT NULL,
    rung         TEXT NOT NULL,
    sent_at      REAL NOT NULL,
    ack_key      TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_notifications (sent_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone, so a store created
    before acknowledgements existed would otherwise be missing these columns and every write
    would fail at runtime.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sent_notifications)")}
    for column in ("ack_key", "fingerprint"):
        if column not in existing:
            conn.execute(
                f"ALTER TABLE sent_notifications ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    # Created here, not in _SCHEMA: indexing a column that the migration above may have just
    # added would fail on a database created before acknowledgements existed.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_ack_key ON sent_notifications (ack_key)")


@runtime_checkable
class StateStore(Protocol):
    """Dedup backend."""

    async def already_sent(self, scope: str, dedup_key: str) -> bool: ...

    async def record_sent(
        self,
        scope: str,
        dedup_key: str,
        *,
        recipient: str,
        channel: str,
        component_id: str,
        status: str,
        rung: str,
        ack_key: str = "",
        fingerprint: str = "",
    ) -> None: ...

    async def find_fingerprint(self, ack_key: str) -> str | None: ...

    async def record_digest_members(
        self, digest_key: str, members: list[tuple[str, str]], *, now: float
    ) -> None: ...

    async def find_digest_members(self, digest_key: str) -> list[tuple[str, str]]: ...

    async def record_seen(self, components: list[tuple[str, str]], *, now: float) -> None: ...

    async def list_known(self) -> list[dict[str, str]]: ...

    async def forget(self, component_id: str) -> None: ...

    async def is_acknowledged(self, ack_key: str, fingerprint: str, *, now: float) -> bool: ...

    async def acknowledge(
        self,
        ack_key: str,
        *,
        component_id: str,
        status: str,
        rung: str,
        fingerprint: str,
        acked_by: str,
        ttl_seconds: float,
        now: float,
    ) -> None: ...

    async def find_acknowledgeable(self, component_id: str) -> list[dict[str, str]]: ...

    async def prune(self, older_than_seconds: float) -> int: ...

    async def check(self) -> None: ...

    async def close(self) -> None: ...


class SqliteStateStore:
    """SQLite-backed store.

    Kubernetes runs this as a single replica with a PVC (CLAUDE.md §14.1, §14.9), so a local
    file is sufficient and avoids an extra moving part. Scaling past one replica requires a
    shared backend and leader election - see §14.11 before changing this.

    ``sqlite3`` is synchronous; calls are dispatched to a worker thread and serialized with a
    lock so the event loop is never blocked.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        """Create the parent directory, open the database, and apply the schema."""
        try:
            await asyncio.to_thread(self._open_sync)
        except (OSError, sqlite3.Error) as exc:
            # The default path is the container's PVC mount point, so this is what a first local
            # run hits. Say how to fix it rather than just reporting errno 13.
            raise StateStoreError(
                f"cannot open state store at {self._path}: {exc}. "
                "Set STATE_DSN to a writable path - the default is where the PVC mounts inside "
                "the container, not somewhere a workstation can write"
            ) from exc

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=10.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
        self._conn = conn

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateStoreError("state store used before open()")
        return self._conn

    async def already_sent(self, scope: str, dedup_key: str) -> bool:
        """Whether this exact notification has already gone out."""

        def _query() -> bool:
            row = (
                self._require()
                .execute("SELECT dedup_key FROM sent_notifications WHERE scope = ?", (scope,))
                .fetchone()
            )
            return row is not None and row[0] == dedup_key

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"dedup lookup failed: {exc}") from exc

    async def record_sent(
        self,
        scope: str,
        dedup_key: str,
        *,
        recipient: str,
        channel: str,
        component_id: str,
        status: str,
        rung: str,
        ack_key: str = "",
        fingerprint: str = "",
    ) -> None:
        """Remember this delivery, replacing any earlier record for the same scope.

        The fingerprint is stored so a later acknowledgement can be recorded against exactly
        the facts the recipient saw, rather than whatever is current when they press Confirm.
        """

        def _write() -> None:
            conn = self._require()
            conn.execute(
                "INSERT INTO sent_notifications "
                "(scope, dedup_key, recipient, channel, component_id, status, rung, sent_at, "
                "ack_key, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET "
                "dedup_key=excluded.dedup_key, status=excluded.status, "
                "rung=excluded.rung, sent_at=excluded.sent_at, "
                "ack_key=excluded.ack_key, fingerprint=excluded.fingerprint",
                (
                    scope,
                    dedup_key,
                    recipient,
                    channel,
                    component_id,
                    status,
                    rung,
                    time.time(),
                    ack_key,
                    fingerprint,
                ),
            )
            conn.commit()

        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except sqlite3.Error as exc:
                raise StateStoreError(f"dedup write failed: {exc}") from exc

    async def record_digest_members(
        self, digest_key: str, members: list[tuple[str, str]], *, now: float
    ) -> None:
        """Remember what a digest listed, so confirming it can acknowledge each item.

        Telegram caps ``callback_data`` at 64 bytes, far too little to carry a list of
        component ids, so the button references the digest and the membership is looked up
        here. Replaces any previous membership for the same digest.
        """

        def _write() -> None:
            conn = self._require()
            conn.execute("DELETE FROM digest_members WHERE digest_key = ?", (digest_key,))
            conn.executemany(
                "INSERT INTO digest_members (digest_key, ack_key, fingerprint, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                [(digest_key, ack, fp, now) for ack, fp in members],
            )
            conn.commit()

        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except sqlite3.Error as exc:
                raise StateStoreError(f"digest membership write failed: {exc}") from exc

    async def find_digest_members(self, digest_key: str) -> list[tuple[str, str]]:
        """What the most recent digest with this key listed."""

        def _query() -> list[tuple[str, str]]:
            rows = (
                self._require()
                .execute(
                    "SELECT ack_key, fingerprint FROM digest_members WHERE digest_key = ?",
                    (digest_key,),
                )
                .fetchall()
            )
            return [(r[0], r[1]) for r in rows]

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"digest membership lookup failed: {exc}") from exc

    async def record_seen(self, components: list[tuple[str, str]], *, now: float) -> None:
        """Remember which components the portal listed, so a disappearance can be noticed.

        An alerting system that quietly stops watching something is worse than one that is
        noisy: without this, a component vanishing from the listing - whether deleted on
        purpose or lost to a portal glitch - would simply stop being evaluated (CLAUDE.md §6).
        """

        def _write() -> None:
            conn = self._require()
            conn.executemany(
                "INSERT INTO known_components (component_id, name, last_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(component_id) DO UPDATE SET name=excluded.name, "
                "last_seen=excluded.last_seen",
                [(cid, name, now) for cid, name in components],
            )
            conn.commit()

        if not components:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except sqlite3.Error as exc:
                raise StateStoreError(f"known-component write failed: {exc}") from exc

    async def list_known(self) -> list[dict[str, str]]:
        """Every component the portal has listed and that we have not been told to forget."""

        def _query() -> list[dict[str, str]]:
            rows = (
                self._require()
                .execute("SELECT component_id, name, last_seen FROM known_components")
                .fetchall()
            )
            return [{"component_id": r[0], "name": r[1], "last_seen": str(r[2])} for r in rows]

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"known-component lookup failed: {exc}") from exc

    async def forget(self, component_id: str) -> None:
        """Stop tracking a component, once its disappearance has been acknowledged."""

        def _delete() -> None:
            conn = self._require()
            conn.execute("DELETE FROM known_components WHERE component_id = ?", (component_id,))
            conn.commit()

        async with self._lock:
            try:
                await asyncio.to_thread(_delete)
            except sqlite3.Error as exc:
                raise StateStoreError(f"forget failed: {exc}") from exc

    async def find_fingerprint(self, ack_key: str) -> str | None:
        """The fingerprint most recently notified for a situation, or None if never sent."""

        def _query() -> str | None:
            row = (
                self._require()
                .execute(
                    "SELECT fingerprint FROM sent_notifications WHERE ack_key = ? "
                    "ORDER BY sent_at DESC LIMIT 1",
                    (ack_key,),
                )
                .fetchone()
            )
            return str(row[0]) if row is not None and row[0] else None

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"fingerprint lookup failed: {exc}") from exc

    async def is_acknowledged(self, ack_key: str, fingerprint: str, *, now: float) -> bool:
        """Whether this exact situation is currently acknowledged.

        Three ways an ack stops applying, all deliberate:
        it was never made, the underlying facts changed (``fingerprint``), or it aged out
        (``expires_at``). A snooze that never lapses is how alerting goes quietly dead.
        """

        def _query() -> bool:
            row = (
                self._require()
                .execute(
                    "SELECT fingerprint, expires_at FROM acknowledgements WHERE ack_key = ?",
                    (ack_key,),
                )
                .fetchone()
            )
            if row is None:
                return False
            return bool(row[0] == fingerprint and row[1] > now)

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"acknowledgement lookup failed: {exc}") from exc

    async def acknowledge(
        self,
        ack_key: str,
        *,
        component_id: str,
        status: str,
        rung: str,
        fingerprint: str,
        acked_by: str,
        ttl_seconds: float,
        now: float,
    ) -> None:
        """Record a confirmation, replacing any earlier one for the same situation."""

        def _write() -> None:
            conn = self._require()
            conn.execute(
                "INSERT INTO acknowledgements "
                "(ack_key, component_id, status, rung, fingerprint, acked_by, acked_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ack_key) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "acked_by=excluded.acked_by, acked_at=excluded.acked_at, "
                "expires_at=excluded.expires_at",
                (
                    ack_key,
                    component_id,
                    status,
                    rung,
                    fingerprint,
                    acked_by,
                    now,
                    now + ttl_seconds,
                ),
            )
            conn.commit()

        async with self._lock:
            try:
                await asyncio.to_thread(_write)
            except sqlite3.Error as exc:
                raise StateStoreError(f"acknowledgement write failed: {exc}") from exc

    async def find_acknowledgeable(self, component_id: str) -> list[dict[str, str]]:
        """Situations recently notified for a component, so a CLI ack can name one.

        Reads the sent log rather than the ack table: the point is to find what is outstanding.
        """

        def _query() -> list[dict[str, str]]:
            rows = (
                self._require()
                .execute(
                    "SELECT DISTINCT ack_key, status, rung, fingerprint FROM sent_notifications "
                    "WHERE component_id = ? AND ack_key != ''",
                    (component_id,),
                )
                .fetchall()
            )
            return [
                {"ack_key": r[0], "status": r[1], "rung": r[2], "fingerprint": r[3]} for r in rows
            ]

        async with self._lock:
            try:
                return await asyncio.to_thread(_query)
            except sqlite3.Error as exc:
                raise StateStoreError(f"acknowledgement lookup failed: {exc}") from exc

    async def prune(self, older_than_seconds: float) -> int:
        """Delete records older than the retention window. Returns the number removed."""
        cutoff = time.time() - older_than_seconds

        def _delete() -> int:
            conn = self._require()
            cursor = conn.execute("DELETE FROM sent_notifications WHERE sent_at < ?", (cutoff,))
            removed = cursor.rowcount
            # Lapsed acks are dropped too; keeping them would only slow lookups.
            removed += conn.execute(
                "DELETE FROM acknowledgements WHERE expires_at < ?", (time.time(),)
            ).rowcount
            conn.commit()
            return removed

        async with self._lock:
            try:
                return await asyncio.to_thread(_delete)
            except sqlite3.Error as exc:
                raise StateStoreError(f"dedup prune failed: {exc}") from exc

    async def check(self) -> None:
        """Readiness probe: confirm the database answers a trivial query."""

        def _ping() -> None:
            self._require().execute("SELECT 1").fetchone()

        async with self._lock:
            try:
                await asyncio.to_thread(_ping)
            except sqlite3.Error as exc:
                raise StateStoreError(f"state store unhealthy: {exc}") from exc

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def __aenter__(self) -> SqliteStateStore:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


class MemoryStateStore:
    """In-process store for tests and ``--dry-run`` experiments. Never used in production."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, float]] = {}
        self._acks: dict[str, tuple[str, float, str]] = {}
        self._sent_fingerprints: dict[str, tuple[str, str, str, str]] = {}
        self._known: dict[str, tuple[str, float]] = {}
        self._digests: dict[str, list[tuple[str, str]]] = {}

    async def already_sent(self, scope: str, dedup_key: str) -> bool:
        row = self._rows.get(scope)
        return row is not None and row[0] == dedup_key

    async def record_sent(
        self,
        scope: str,
        dedup_key: str,
        *,
        recipient: str,
        channel: str,
        component_id: str,
        status: str,
        rung: str,
        ack_key: str = "",
        fingerprint: str = "",
    ) -> None:
        self._rows[scope] = (dedup_key, time.time())
        if ack_key:
            self._sent_fingerprints[ack_key] = (fingerprint, status, rung, component_id)

    async def find_fingerprint(self, ack_key: str) -> str | None:
        row = self._sent_fingerprints.get(ack_key)
        return row[0] if row else None

    async def record_digest_members(
        self, digest_key: str, members: list[tuple[str, str]], *, now: float
    ) -> None:
        self._digests[digest_key] = list(members)

    async def find_digest_members(self, digest_key: str) -> list[tuple[str, str]]:
        return list(self._digests.get(digest_key, []))

    async def record_seen(self, components: list[tuple[str, str]], *, now: float) -> None:
        for cid, name in components:
            self._known[cid] = (name, now)

    async def list_known(self) -> list[dict[str, str]]:
        return [
            {"component_id": cid, "name": name, "last_seen": str(seen)}
            for cid, (name, seen) in self._known.items()
        ]

    async def forget(self, component_id: str) -> None:
        self._known.pop(component_id, None)

    async def is_acknowledged(self, ack_key: str, fingerprint: str, *, now: float) -> bool:
        row = self._acks.get(ack_key)
        return row is not None and row[0] == fingerprint and row[1] > now

    async def acknowledge(
        self,
        ack_key: str,
        *,
        component_id: str,
        status: str,
        rung: str,
        fingerprint: str,
        acked_by: str,
        ttl_seconds: float,
        now: float,
    ) -> None:
        self._acks[ack_key] = (fingerprint, now + ttl_seconds, acked_by)

    async def find_acknowledgeable(self, component_id: str) -> list[dict[str, str]]:
        return [
            {"ack_key": k, "status": v[1], "rung": v[2], "fingerprint": v[0]}
            for k, v in self._sent_fingerprints.items()
            if v[3] == component_id
        ]

    async def prune(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        stale = [k for k, (_, ts) in self._rows.items() if ts < cutoff]
        for key in stale:
            del self._rows[key]
        lapsed = [k for k, (_, exp, _) in self._acks.items() if exp < time.time()]
        for key in lapsed:
            del self._acks[key]
        return len(stale) + len(lapsed)

    async def check(self) -> None:
        return None

    async def close(self) -> None:
        self._rows.clear()
        self._acks.clear()
        self._sent_fingerprints.clear()
        self._known.clear()
        self._digests.clear()


def build_store(backend: str, dsn: Path) -> StateStore:
    """Build the configured store.

    Raises:
        StateStoreError: for a backend that has not been implemented. Adding one (Redis,
            Postgres) is a dependency decision and needs approval first (CLAUDE.md §0, §4).
    """
    if backend == "sqlite":
        return SqliteStateStore(dsn)
    raise StateStoreError(
        f"state backend {backend!r} is not implemented; only 'sqlite' is available"
    )
