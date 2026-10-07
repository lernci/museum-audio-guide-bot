"""Thin async DB access layer (aiosqlite). Swap for asyncpg + the same SQL
if/when this moves to PostgreSQL — the query shapes don't change.
"""
from contextlib import asynccontextmanager

import aiosqlite

from worker.prompts import LANGUAGES

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
async def create_exhibit(exhibit_id, title_am, fact_sheet_am, created_by):
    async with get_conn() as db:
        await db.execute(
            """INSERT INTO exhibits (id, title_am, fact_sheet_am, created_by)
               VALUES (?, ?, ?, ?)""",
            (exhibit_id, title_am, fact_sheet_am, created_by),
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


async def set_deep_link(exhibit_id, deep_link):
    """Web flow's equivalent of set_exhibit_qr, minus the file path — the web
    admin panel generates the QR image on demand (webapp/app.py's media_qr
    route) instead of persisting a PNG to disk, so a redeploy or container
    recreation can never leave it pointing at a file that no longer exists."""
    async with get_conn() as db:
        await db.execute(
            "UPDATE exhibits SET deep_link = ? WHERE id = ?", (deep_link, exhibit_id)
        )
        await db.commit()


async def get_exhibit(exhibit_id):
    async with get_conn() as db:
        cur = await db.execute("SELECT * FROM exhibits WHERE id = ?", (exhibit_id,))
        return await cur.fetchone()


async def list_exhibits():
    async with get_conn() as db:
        cur = await db.execute("SELECT * FROM exhibits ORDER BY updated_at DESC")
        return await cur.fetchall()


async def update_exhibit_content(exhibit_id, title_am=None, fact_sheet_am=None):
    """Used by the edit flow — only overwrites fields staff actually resubmitted."""
    fields, values = [], []
    if title_am is not None:
        fields.append("title_am = ?")
        values.append(title_am)
    if fact_sheet_am is not None:
        fields.append("fact_sheet_am = ?")
        values.append(fact_sheet_am)
    if not fields:
        return
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(exhibit_id)
    async with get_conn() as db:
        await db.execute(f"UPDATE exhibits SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()


async def clear_exhibit_generation_state(exhibit_id):
    """Wipe cached audio + job history for an exhibit so a resubmitted edit
    goes through the full pipeline again instead of mixing in stale rows."""
    async with get_conn() as db:
        await db.execute("DELETE FROM audio_cache WHERE exhibit_id = ?", (exhibit_id,))
        await db.execute("DELETE FROM generation_jobs WHERE exhibit_id = ?", (exhibit_id,))
        await db.commit()


# ── exhibit_photos ──────────────────────────────────────────────────────
async def replace_exhibit_photos(exhibit_id, telegram_file_ids):
    """Overwrites the full photo set for an exhibit (used on create and on edit)."""
    async with get_conn() as db:
        await db.execute("DELETE FROM exhibit_photos WHERE exhibit_id = ?", (exhibit_id,))
        await db.executemany(
            "INSERT INTO exhibit_photos (exhibit_id, telegram_file_id, sort_order) VALUES (?, ?, ?)",
            [(exhibit_id, file_id, i) for i, file_id in enumerate(telegram_file_ids)],
        )
        await db.commit()


async def get_exhibit_photos(exhibit_id):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT * FROM exhibit_photos WHERE exhibit_id = ? ORDER BY sort_order", (exhibit_id,)
        )
        return await cur.fetchall()


# ── staff_users ─────────────────────────────────────────────────────────
async def get_staff_role(user_id):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT role FROM staff_users WHERE telegram_user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["role"] if row else None


async def get_staff_by_role(role):
    async with get_conn() as db:
        cur = await db.execute("SELECT telegram_user_id FROM staff_users WHERE role = ?", (role,))
        return await cur.fetchall()


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


async def ensure_audio_rows(exhibit_id):
    """Creates the 10 (exhibit, language) rows if they don't exist yet, each
    starting fully stale/missing — the correct starting state for a brand
    new exhibit (nothing generated) and a no-op for one that already has
    rows."""
    async with get_conn() as db:
        await db.executemany(
            """INSERT OR IGNORE INTO audio_cache (exhibit_id, language_code, status)
               VALUES (?, ?, 'pending')""",
            [(exhibit_id, code) for code in LANGUAGES],
        )
        await db.commit()


async def mark_source_edited(exhibit_id):
    """Stage 1 (title/photos/fact sheet) changed — invalidates every
    language's draft script AND draft audio, per the staged workflow's core
    staleness rule. Ensures rows exist first so a first-ever save on a brand
    new exhibit behaves identically to an edit of an existing one."""
    await ensure_audio_rows(exhibit_id)
    async with get_conn() as db:
        await db.execute(
            "UPDATE audio_cache SET script_stale = 1, audio_stale = 1 WHERE exhibit_id = ?",
            (exhibit_id,),
        )
        await db.commit()


async def save_draft_scripts(exhibit_id, scripts: dict):
    """Stage 2 (Generate scripts) result — one row per language. Editing the
    script always invalidates whatever draft audio existed for it, since
    that audio was narrating the *old* script text."""
    async with get_conn() as db:
        await db.executemany(
            """INSERT INTO audio_cache (exhibit_id, language_code, draft_title_translated,
                                         draft_script_text, script_stale, audio_stale)
               VALUES (?, ?, ?, ?, 0, 1)
               ON CONFLICT(exhibit_id, language_code) DO UPDATE SET
                   draft_title_translated = excluded.draft_title_translated,
                   draft_script_text = excluded.draft_script_text,
                   script_stale = 0, audio_stale = 1""",
            [(exhibit_id, code, entry["title"], entry["narration"]) for code, entry in scripts.items()],
        )
        await db.commit()


async def save_draft_script_manual(exhibit_id, language_code, title_translated, script_text):
    """A staff member hand-edited one language's script directly — treated
    as the new authoritative text for that language (script_stale clears),
    but its existing draft audio no longer matches (audio_stale sets)."""
    async with get_conn() as db:
        await db.execute(
            """UPDATE audio_cache
               SET draft_title_translated = ?, draft_script_text = ?,
                   script_stale = 0, audio_stale = 1
               WHERE exhibit_id = ? AND language_code = ?""",
            (title_translated, script_text, exhibit_id, language_code),
        )
        await db.commit()


async def upsert_audio_draft(exhibit_id, language_code, **fields):
    """Like upsert_audio_cache, but writes the draft_* shadow columns the web
    admin's Stage 3 (Generate audio) uses — never the published columns
    bot/visitor.py reads. `fields` keys are the bare names (e.g. "status"),
    this prefixes them to draft_* automatically."""
    fields = {f"draft_{k}": v for k, v in fields.items()}
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


async def clear_audio_stale(exhibit_id, language_code):
    """A draft audio regeneration for this language just succeeded — it now
    matches the current draft script."""
    async with get_conn() as db:
        await db.execute(
            "UPDATE audio_cache SET audio_stale = 0 WHERE exhibit_id = ? AND language_code = ?",
            (exhibit_id, language_code),
        )
        await db.commit()


async def promote_draft_to_live(exhibit_id):
    """Publish: copies every language's draft_* columns onto the published
    columns in one statement — the only thing that can change what
    bot/visitor.py serves. Until this runs, regenerating drafts never
    touches what's currently live."""
    async with get_conn() as db:
        await db.execute(
            """UPDATE audio_cache SET
                   script_text = draft_script_text,
                   title_translated = draft_title_translated,
                   tts_provider = draft_tts_provider,
                   voice_id = draft_voice_id,
                   telegram_file_id = draft_telegram_file_id,
                   telegram_file_unique_id = draft_telegram_file_unique_id,
                   duration_seconds = draft_duration_seconds,
                   status = draft_status,
                   error_message = draft_error_message,
                   generated_at = CURRENT_TIMESTAMP
               WHERE exhibit_id = ?""",
            (exhibit_id,),
        )
        await db.commit()


async def get_all_audio_rows(exhibit_id):
    """All language rows for an exhibit, keyed by language_code — used by the
    web admin detail page to render all 10 languages in one query."""
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT * FROM audio_cache WHERE exhibit_id = ?", (exhibit_id,)
        )
        rows = await cur.fetchall()
        return {r["language_code"]: r for r in rows}


async def all_languages_ready(exhibit_id, expected_count):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM audio_cache WHERE exhibit_id = ? AND status = 'ready'",
            (exhibit_id,),
        )
        row = await cur.fetchone()
        return row["n"] == expected_count


async def get_failed_languages(exhibit_id):
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT language_code FROM audio_cache WHERE exhibit_id = ? AND status = 'failed'",
            (exhibit_id,),
        )
        rows = await cur.fetchall()
        return [r["language_code"] for r in rows]


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
