import aiosqlite


SQLITE_BUSY_TIMEOUT_MS = 30000


async def configure_sqlite_connection(
    db: aiosqlite.Connection,
    *,
    enable_wal: bool = True,
    busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS,
) -> None:
    if enable_wal:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
