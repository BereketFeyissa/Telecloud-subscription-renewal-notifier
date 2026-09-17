"""Dedup semantics: once per (status, rung), and again when it changes (§8.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tele_scraper.errors import StateStoreError
from tele_scraper.state.store import MemoryStateStore, SqliteStateStore, build_store

SCOPE = "ops|slack|https://hooks.test/x|comp-1"


@pytest.fixture
async def sqlite_store(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = SqliteStateStore(tmp_path / "nested" / "state.sqlite3")
    await store.open()
    yield store
    await store.close()


async def record(store, key: str) -> None:  # type: ignore[no-untyped-def]
    await store.record_sent(
        SCOPE,
        key,
        recipient="ops",
        channel="slack",
        component_id="comp-1",
        status="EXPIRING_SOON",
        rung="259200s",
    )


@pytest.mark.parametrize("factory", ["sqlite", "memory"])
async def test_dedup_lifecycle(factory: str, tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.sqlite3") if factory == "sqlite" else MemoryStateStore()
    if isinstance(store, SqliteStateStore):
        await store.open()

    assert await store.already_sent(SCOPE, "key-a") is False
    await record(store, "key-a")
    assert await store.already_sent(SCOPE, "key-a") is True

    # A new status or rung produces a different key, so it is NOT suppressed.
    assert await store.already_sent(SCOPE, "key-b") is False
    await record(store, "key-b")
    assert await store.already_sent(SCOPE, "key-b") is True

    # Only the latest state is remembered, so returning to an earlier state re-notifies.
    assert await store.already_sent(SCOPE, "key-a") is False
    await store.close()


async def test_sqlite_creates_missing_parent_directories(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    await sqlite_store.check()
    assert await sqlite_store.already_sent(SCOPE, "x") is False


async def test_prune_removes_old_rows(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    await record(sqlite_store, "key-a")
    assert await sqlite_store.prune(older_than_seconds=-1) == 1
    assert await sqlite_store.already_sent(SCOPE, "key-a") is False


async def test_prune_keeps_fresh_rows(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    await record(sqlite_store, "key-a")
    assert await sqlite_store.prune(older_than_seconds=3600) == 0


async def test_using_the_store_before_open_is_an_error(tmp_path: Path) -> None:
    store = SqliteStateStore(tmp_path / "s.sqlite3")
    with pytest.raises(StateStoreError, match="before open"):
        await store.already_sent(SCOPE, "k")


async def test_context_manager_opens_and_closes(tmp_path: Path) -> None:
    async with SqliteStateStore(tmp_path / "s.sqlite3") as store:
        await store.check()
    with pytest.raises(StateStoreError):
        await store.check()


async def test_memory_store_prune_and_check() -> None:
    store = MemoryStateStore()
    await record(store, "key-a")
    assert await store.prune(older_than_seconds=-1) == 1
    await store.check()
    await store.close()


def test_unimplemented_backend_is_refused_not_guessed() -> None:
    """Adding Redis or Postgres is a dependency decision that needs approval (§0, §4)."""
    with pytest.raises(StateStoreError, match="not implemented"):
        build_store("redis", Path("/tmp/x"))


def test_sqlite_backend_builds() -> None:
    assert isinstance(build_store("sqlite", Path("/tmp/x.sqlite3")), SqliteStateStore)


# --- acknowledgements (exercised against SQLite, not just the in-memory double) -----


async def record_with_ack(store, ack_key: str, fingerprint: str) -> None:  # type: ignore[no-untyped-def]
    await store.record_sent(
        SCOPE,
        "key",
        recipient="ops",
        channel="slack",
        component_id="comp-1",
        status="EXPIRED",
        rung="-",
        ack_key=ack_key,
        fingerprint=fingerprint,
    )


async def test_sqlite_ack_lifecycle(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    import time as _time

    now = _time.time()
    await record_with_ack(sqlite_store, "comp-1|EXPIRED|-", "fp1")

    assert await sqlite_store.is_acknowledged("comp-1|EXPIRED|-", "fp1", now=now) is False
    await sqlite_store.acknowledge(
        "comp-1|EXPIRED|-",
        component_id="comp-1",
        status="EXPIRED",
        rung="-",
        fingerprint="fp1",
        acked_by="ops",
        ttl_seconds=3600,
        now=now,
    )
    assert await sqlite_store.is_acknowledged("comp-1|EXPIRED|-", "fp1", now=now) is True
    # Facts moved -> the ack no longer applies.
    assert await sqlite_store.is_acknowledged("comp-1|EXPIRED|-", "fp2", now=now) is False
    # Aged out -> the alert returns.
    assert await sqlite_store.is_acknowledged("comp-1|EXPIRED|-", "fp1", now=now + 7200) is False


async def test_sqlite_find_acknowledgeable_returns_what_the_cli_needs(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    """Regression: this returned the wrong keys and only the in-memory double was tested."""
    await record_with_ack(sqlite_store, "comp-1|EXPIRED|-", "fp1")
    rows = await sqlite_store.find_acknowledgeable("comp-1")
    assert rows and set(rows[0]) == {"ack_key", "status", "rung", "fingerprint"}
    assert rows[0]["ack_key"] == "comp-1|EXPIRED|-"
    assert rows[0]["fingerprint"] == "fp1"


async def test_sqlite_find_fingerprint(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    await record_with_ack(sqlite_store, "comp-1|EXPIRED|-", "fp1")
    assert await sqlite_store.find_fingerprint("comp-1|EXPIRED|-") == "fp1"
    assert await sqlite_store.find_fingerprint("never-sent") is None


async def test_prune_drops_lapsed_acknowledgements(sqlite_store) -> None:  # type: ignore[no-untyped-def]
    import time as _time

    await sqlite_store.acknowledge(
        "comp-1|EXPIRED|-",
        component_id="comp-1",
        status="EXPIRED",
        rung="-",
        fingerprint="fp1",
        acked_by="ops",
        ttl_seconds=-1,
        now=_time.time(),
    )
    assert await sqlite_store.prune(older_than_seconds=3600) >= 1


async def test_a_store_created_before_acknowledgements_is_migrated(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS leaves an old table alone, so columns must be added."""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE sent_notifications (scope TEXT PRIMARY KEY, dedup_key TEXT NOT NULL, "
        "recipient TEXT NOT NULL, channel TEXT NOT NULL, component_id TEXT NOT NULL, "
        "status TEXT NOT NULL, rung TEXT NOT NULL, sent_at REAL NOT NULL)"
    )
    legacy.commit()
    legacy.close()

    store = SqliteStateStore(path)
    await store.open()
    await record_with_ack(store, "comp-1|EXPIRED|-", "fp1")
    assert await store.find_fingerprint("comp-1|EXPIRED|-") == "fp1"
    await store.close()


async def test_an_unwritable_path_says_how_to_fix_it(tmp_path: Path) -> None:
    """The default STATE_DSN is the container's PVC mount, so this is the first-run experience."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so it cannot become a directory")
    store = SqliteStateStore(blocked / "nested" / "state.sqlite3")
    with pytest.raises(StateStoreError) as excinfo:
        await store.open()
    assert "STATE_DSN" in str(excinfo.value), "the error must name the setting that fixes it"
