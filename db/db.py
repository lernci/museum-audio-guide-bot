"""Thin async DB access layer (aiosqlite). Swap for asyncpg + the same SQL
if/when this moves to PostgreSQL — the query shapes don't change.
"""
from contextlib import asynccontextmanager

import aiosqlite

DB_PATH = "museum_bot.sqlite3"


@asynccontextmanager
async def get_conn():
    conn = await aiosqlite.connect(DB_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        await conn.close()


# ── exhibits ────────────────────────────────────────────────────────────
async def create_exhibit(exhibit_id, title_am, fact_sheet_am, photo_file_id, created_by):
    async with get_conn() as db:
        await db.execute(
            """INSERT INTO exhibits (id, title_am, fact_sheet_am, photo_file_id, created_by)
               VALUES (?, ?, ?, ?, ?)""",
            (exhibit_id, title_am, fact_sheet_am, photo_file_id, created_by),
        )
        await db.commit()


async def set_exhibit_status(exhibit_id, status):
    async with get_conn() as db:
        await db.execute(
            "UPDATE exhibits SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, exhibit_id),
        )
        await db.commit()


async def set_exhibit_qr(exhibit_id, qr_code_path, deep_link):
    async with get_conn() as db:
        await db.execute(
            "UPDATE exhibits SET qr_code_path = ?, deep_link = ? WHERE id = ?",
            (qr_code_path, deep_link, exhibit_id),
        )
        await db.commit()


async def get_exhibit(exhibit_id):
    async with get_conn() as db:
        cur = await db.execute("SELECT * FROM exhibits WHERE id = ?", (exhibit_id,))
        return await cur.fetchone()


# ── audio_cache ─────────────────────────────────────────────────────────
async def upsert_audio_cache(exhibit_id, language_code, **fields):
    columns = ["exhibit_id", "language_code", *fields.keys()]
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{k} = excluded.{k}" for k in fields.keys())
    values = [exhibit_id, language_code, *fields.values()]
    async with get_conn() as db:
        await db.execute(
            f"""INSERT INTO audio_cache ({", ".join(columns)}) VALUES ({placeholders})
                ON CONFLICT(exhibit_id, language_code) DO UPDATE SET {updates}""",
            values,
        )
        await db.commit()


async def get_cached_voice(exhibit_id, language_code):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT * FROM audio_cache WHERE exhibit_id = ? AND language_code = ? AND status = 'ready'",
            (exhibit_id, language_code),
        )
        return await cur.fetchone()


async def get_audio_row(exhibit_id, language_code):
    """Like get_cached_voice but returns the row regardless of status —
    used by /retry to fetch a failed row's already-generated script_text."""
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT * FROM audio_cache WHERE exhibit_id = ? AND language_code = ?",
            (exhibit_id, language_code),
        )
        return await cur.fetchone()


async def all_languages_ready(exhibit_id, expected_count):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM audio_cache WHERE exhibit_id = ? AND status = 'ready'",
            (exhibit_id,),
        )
        row = await cur.fetchone()
        return row["n"] == expected_count


# ── generation_jobs ─────────────────────────────────────────────────────
async def enqueue_job(exhibit_id, job_type, language_code=None):
    async with get_conn() as db:
        await db.execute(
            "INSERT INTO generation_jobs (exhibit_id, job_type, language_code) VALUES (?, ?, ?)",
            (exhibit_id, job_type, language_code),
        )
        await db.commit()


async def mark_job(job_id, status, error_message=None):
    async with get_conn() as db:
        await db.execute(
            """UPDATE generation_jobs
               SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP,
                   attempts = attempts + 1
               WHERE id = ?""",
            (status, error_message, job_id),
        )
        await db.commit()


async def mark_jobs_by_type(exhibit_id, job_type, status, language_code=None, error_message=None):
    """Update job(s) matched by (exhibit_id, job_type[, language_code]) rather than
    by numeric id — the worker deals in (exhibit, stage) terms, not job ids."""
    async with get_conn() as db:
        if language_code is None:
            await db.execute(
                """UPDATE generation_jobs
                   SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP,
                       attempts = attempts + 1
                   WHERE exhibit_id = ? AND job_type = ?""",
                (status, error_message, exhibit_id, job_type),
            )
        else:
            await db.execute(
                """UPDATE generation_jobs
                   SET status = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP,
                       attempts = attempts + 1
                   WHERE exhibit_id = ? AND job_type = ? AND language_code = ?""",
                (status, error_message, exhibit_id, job_type, language_code),
            )
        await db.commit()


async def get_unfinished_exhibit_ids():
    """Used on bot startup to requeue anything interrupted by a restart."""
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT DISTINCT exhibit_id FROM generation_jobs WHERE status IN ('queued', 'running')"
        )
        rows = await cur.fetchall()
        return [r["exhibit_id"] for r in rows]
