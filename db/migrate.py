"""Brings a SQLite DB to the current schema shape (review workflow, photos,
translated titles). Safe to run repeatedly — every step checks current state
first and is a no-op if already applied. Creates all tables from scratch if
the DB is empty. Run with: python3 -m db.migrate
"""
import asyncio

import aiosqlite

from db.db import DB_PATH

FRESH_SCHEMA = [
    """CREATE TABLE staff_users (
        telegram_user_id BIGINT PRIMARY KEY,
        full_name        TEXT NOT NULL,
        role             TEXT NOT NULL DEFAULT 'content'
                          CHECK (role IN ('content', 'owner')),
        added_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE exhibits (
        id            TEXT PRIMARY KEY,
        title_am      TEXT NOT NULL,
        fact_sheet_am TEXT NOT NULL,
        qr_code_path  TEXT,
        deep_link     TEXT,
        status        TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'processing', 'review', 'live', 'unpublished', 'failed')),
        created_by    BIGINT REFERENCES staff_users(telegram_user_id),
        created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE exhibit_photos (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        exhibit_id       TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
        telegram_file_id TEXT NOT NULL,
        sort_order       INTEGER NOT NULL DEFAULT 0,
        UNIQUE (exhibit_id, sort_order)
    )""",
    "CREATE INDEX idx_exhibit_photos_exhibit ON exhibit_photos(exhibit_id)",
    """CREATE TABLE audio_cache (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        exhibit_id              TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
        language_code           TEXT NOT NULL
                                 CHECK (language_code IN
                                   ('am','en','ru','fr','es','de','fa','zh','it','el')),
        script_text             TEXT,
        title_translated        TEXT,
        tts_provider            TEXT
                                 CHECK (tts_provider IN ('local_am', 'openai', 'elevenlabs')),
        voice_id                TEXT,
        telegram_file_id        TEXT,
        telegram_file_unique_id TEXT,
        duration_seconds        REAL,
        status                  TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'generating', 'ready', 'failed')),
        error_message           TEXT,
        generated_at            TIMESTAMP,
        UNIQUE (exhibit_id, language_code)
    )""",
    "CREATE INDEX idx_audio_cache_exhibit ON audio_cache(exhibit_id)",
    "CREATE INDEX idx_audio_cache_status ON audio_cache(status)",
    """CREATE TABLE generation_jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        exhibit_id    TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
        job_type      TEXT NOT NULL CHECK (job_type IN ('qr', 'translate', 'tts')),
        language_code TEXT,
        status        TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'running', 'success', 'failed')),
        attempts      INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX idx_generation_jobs_status ON generation_jobs(status)",
    "CREATE INDEX idx_generation_jobs_exhibit ON generation_jobs(exhibit_id)",
    """CREATE TABLE exhibit_views (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        exhibit_id        TEXT NOT NULL REFERENCES exhibits(id) ON DELETE CASCADE,
        telegram_user_id  BIGINT NOT NULL,
        language_code     TEXT NOT NULL,
        viewed_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX idx_exhibit_views_exhibit ON exhibit_views(exhibit_id)",
]


async def _tables(conn) -> set:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in await cur.fetchall()}


async def _columns(conn, table: str) -> set:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _table_sql(conn, table: str):
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _rebuild_staff_users(conn) -> None:
    """CHECK constraints can't be ALTERed in SQLite — rebuild the table to pick
    up the new role values, remapping admin->owner and editor->content.

    Built under a temp name, old table dropped, temp renamed into place —
    NOT the reverse. SQLite's RENAME TABLE auto-rewrites other tables'
    REFERENCES clauses that point at the renamed name, so renaming the *old*
    table away would leave exhibits.created_by pointing at a name we're
    about to drop. Renaming the *new* table into place instead is a no-op
    for other tables (nothing yet references the temp name)."""
    await conn.execute(FRESH_SCHEMA[0].replace("staff_users", "staff_users_new", 1))
    await conn.execute(
        """INSERT INTO staff_users_new (telegram_user_id, full_name, role, added_at)
           SELECT telegram_user_id, full_name,
                  CASE role WHEN 'admin' THEN 'owner' WHEN 'editor' THEN 'content' ELSE role END,
                  added_at
           FROM staff_users"""
    )
    await conn.execute("DROP TABLE staff_users")
    await conn.execute("ALTER TABLE staff_users_new RENAME TO staff_users")


async def _rebuild_exhibits(conn) -> None:
    """Same CHECK-constraint problem for status, plus dropping photo_file_id
    (superseded by exhibit_photos). Existing photo_file_id values are backfilled
    into exhibit_photos as sort_order 0 before the column disappears. See
    _rebuild_staff_users for why the temp-name/drop-old/rename-new ordering
    matters — the reverse would orphan audio_cache/generation_jobs/
    exhibit_views/exhibit_photos' foreign keys."""
    old_cols = await _columns(conn, "exhibits")
    await conn.execute(FRESH_SCHEMA[1].replace("exhibits", "exhibits_new", 1))
    await conn.execute(
        """INSERT INTO exhibits_new (id, title_am, fact_sheet_am, qr_code_path, deep_link,
                                      status, created_by, created_at, updated_at)
           SELECT id, title_am, fact_sheet_am, qr_code_path, deep_link,
                  CASE status WHEN 'ready' THEN 'live' ELSE status END,
                  created_by, created_at, updated_at
           FROM exhibits"""
    )
    if "photo_file_id" in old_cols:
        if "exhibit_photos" not in await _tables(conn):
            await conn.execute(FRESH_SCHEMA[2])
            await conn.execute(FRESH_SCHEMA[3])
        await conn.execute(
            """INSERT OR IGNORE INTO exhibit_photos (exhibit_id, telegram_file_id, sort_order)
               SELECT id, photo_file_id, 0 FROM exhibits WHERE photo_file_id IS NOT NULL"""
        )
    await conn.execute("DROP TABLE exhibits")
    await conn.execute("ALTER TABLE exhibits_new RENAME TO exhibits")


async def migrate() -> None:
    conn = await aiosqlite.connect(DB_PATH)
    try:
        await conn.execute("PRAGMA foreign_keys = OFF")
        tables = await _tables(conn)

        if not tables:
            for stmt in FRESH_SCHEMA:
                await conn.execute(stmt)
            await conn.commit()
            print("Fresh schema created.")
            return

        staff_sql = await _table_sql(conn, "staff_users")
        if staff_sql and "'admin'" in staff_sql:
            await _rebuild_staff_users(conn)

        exhibits_sql = await _table_sql(conn, "exhibits")
        if exhibits_sql and (
            "'ready'" in exhibits_sql
            or "photo_file_id" in exhibits_sql
            or "'unpublished'" not in exhibits_sql
        ):
            await _rebuild_exhibits(conn)

        if "exhibit_photos" not in await _tables(conn):
            await conn.execute(FRESH_SCHEMA[2])
            await conn.execute(FRESH_SCHEMA[3])

        if "title_translated" not in await _columns(conn, "audio_cache"):
            await conn.execute("ALTER TABLE audio_cache ADD COLUMN title_translated TEXT")

        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.commit()
        print("Migration complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
