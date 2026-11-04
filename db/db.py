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


async def exhibit_id_taken(exhibit_id):
    """Case-insensitive, so 'a1' can't be created next to 'A1' (confusing on printed QRs)."""
    async with get_conn() as db:
        cur = await db.execute("SELECT 1 FROM exhibits WHERE id = ? COLLATE NOCASE", (exhibit_id,))
        return await cur.fetchone() is not None


async def next_sequential_exhibit_id(width=3):
    """Highest all-digit ID + 1, zero-padded (001, 002, ...). Uses max+1 rather than
    filling gaps so a number is never reused. Non-numeric IDs are ignored."""
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT MAX(CAST(id AS INTEGER)) FROM exhibits WHERE id NOT GLOB '*[^0-9]*'"
        )
        row = await cur.fetchone()
    return str((row[0] or 0) + 1).zfill(width)


async def list_exhibits(tag_ids=None):
    """All exhibits (newest first), each with `tag_names` (comma-joined Armenian names).
    tag_ids: None/empty = all; otherwise exhibits having ANY of those tags."""
    sql = """SELECT e.*,
                    (SELECT group_concat(t.name_hy, ', ')
                       FROM exhibit_tags et JOIN tags t ON t.id = et.tag_id
                      WHERE et.exhibit_id = e.id) AS tag_names
             FROM exhibits e"""
    params = []
    if tag_ids:
        sql += f" WHERE e.id IN (SELECT exhibit_id FROM exhibit_tags WHERE tag_id IN ({','.join('?' * len(tag_ids))}))"
        params = [int(t) for t in tag_ids]
    async with get_conn() as db:
        cur = await db.execute(sql + " ORDER BY e.updated_at DESC", params)
        return await cur.fetchall()


async def list_live_exhibits_for_lang(language_code, tag_id=None):
    """Live exhibits ordered by id, each with `title` = the published translated title in
    `language_code` (falls back to the Armenian staff title). Optional tag filter."""
    sql = """SELECT e.id, COALESCE(NULLIF(a.title_translated, ''), e.title_am) AS title
             FROM exhibits e
             LEFT JOIN audio_cache a ON a.exhibit_id = e.id AND a.language_code = ?
             WHERE e.status = 'live'"""
    params = [language_code]
    if tag_id:
        sql += " AND e.id IN (SELECT exhibit_id FROM exhibit_tags WHERE tag_id = ?)"
        params.append(int(tag_id))
    async with get_conn() as db:
        cur = await db.execute(sql + " ORDER BY e.id", params)
        return await cur.fetchall()


# ── tags ────────────────────────────────────────────────────────────────
TRANSLATION_LANGS = [c for c in LANGUAGES if c != "am"]   # tags are entered in Armenian


async def list_tags():
    """Every tag with exhibit_count and translated_count (of the 9 non-Armenian languages)."""
    async with get_conn() as db:
        cur = await db.execute(
            """SELECT t.id, t.name_hy, t.sort_order,
                      (SELECT COUNT(*) FROM exhibit_tags et WHERE et.tag_id = t.id) AS exhibit_count,
                      (SELECT COUNT(*) FROM tag_translations tt WHERE tt.tag_id = t.id AND tt.lang != 'am') AS translated_count
               FROM tags t ORDER BY t.sort_order, t.name_hy"""
        )
        return await cur.fetchall()


async def get_tag(tag_id):
    async with get_conn() as db:
        cur = await db.execute("SELECT * FROM tags WHERE id = ?", (int(tag_id),))
        return await cur.fetchone()


async def count_tags():
    async with get_conn() as db:
        cur = await db.execute("SELECT COUNT(*) FROM tags")
        return (await cur.fetchone())[0]


async def find_tag_by_name(name_hy, exclude_id=None):
    """Case-insensitive lookup done in Python — SQLite's lower() is ASCII-only, so it
    would treat 'Հանքանյութեր' and 'ՀԱՆՔԱՆՅՈՒԹԵՐ' as different names."""
    name_hy = name_hy.strip()
    async with get_conn() as db:
        cur = await db.execute("SELECT id, name_hy FROM tags")
        for row in await cur.fetchall():
            if row["id"] != exclude_id and row["name_hy"].casefold() == name_hy.casefold():
                return row["id"]
    return None


async def get_or_create_tag(name_hy):
    """-> (tag_id, created). Reuses an existing tag whose name matches case-insensitively."""
    name_hy = name_hy.strip()
    existing = await find_tag_by_name(name_hy)
    if existing is not None:
        return existing, False
    async with get_conn() as db:
        cur = await db.execute(
            "INSERT INTO tags (name_hy, sort_order) "
            "VALUES (?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM tags))",
            (name_hy,),
        )
        await db.commit()
        return cur.lastrowid, True


async def rename_tag(tag_id, new_name_hy):
    """Renames and DROPS the old translations (they translated the old name). Raises ValueError
    if another tag already has that name."""
    new_name_hy = new_name_hy.strip()
    if await find_tag_by_name(new_name_hy, exclude_id=int(tag_id)) is not None:
        raise ValueError("duplicate")
    async with get_conn() as db:
        await db.execute("UPDATE tags SET name_hy = ? WHERE id = ?", (new_name_hy, int(tag_id)))
        await db.execute("DELETE FROM tag_translations WHERE tag_id = ?", (int(tag_id),))
        await db.commit()


async def merge_tags(src_id, dst_id):
    """Reassigns every exhibit of `src` to `dst` (no duplicate links), then removes `src`.
    Returns how many exhibits were linked to src."""
    src_id, dst_id = int(src_id), int(dst_id)
    if src_id == dst_id:
        raise ValueError("same tag")
    async with get_conn() as db:
        cur = await db.execute("SELECT COUNT(*) FROM exhibit_tags WHERE tag_id = ?", (src_id,))
        n = (await cur.fetchone())[0]
        await db.execute(
            "INSERT OR IGNORE INTO exhibit_tags (exhibit_id, tag_id) "
            "SELECT exhibit_id, ? FROM exhibit_tags WHERE tag_id = ?", (dst_id, src_id))
        await db.execute("DELETE FROM exhibit_tags WHERE tag_id = ?", (src_id,))
        await db.execute("DELETE FROM tag_translations WHERE tag_id = ?", (src_id,))
        await db.execute("DELETE FROM tags WHERE id = ?", (src_id,))
        await db.commit()
        return n


async def delete_tag(tag_id):
    """Deletes the tag and its links/translations only — never the exhibits. Returns the
    number of exhibits that were linked."""
    tag_id = int(tag_id)
    async with get_conn() as db:
        cur = await db.execute("SELECT COUNT(*) FROM exhibit_tags WHERE tag_id = ?", (tag_id,))
        n = (await cur.fetchone())[0]
        await db.execute("DELETE FROM exhibit_tags WHERE tag_id = ?", (tag_id,))
        await db.execute("DELETE FROM tag_translations WHERE tag_id = ?", (tag_id,))
        await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        await db.commit()
        return n


async def get_exhibit_tag_ids(exhibit_id):
    async with get_conn() as db:
        cur = await db.execute("SELECT tag_id FROM exhibit_tags WHERE exhibit_id = ?", (exhibit_id,))
        return [r[0] for r in await cur.fetchall()]


async def set_exhibit_tags(exhibit_id, tag_ids):
    """Replaces the exhibit's tag set. Deliberately does NOT touch updated_at/staleness —
    tags are metadata, not content."""
    ids = {int(t) for t in tag_ids}
    async with get_conn() as db:
        await db.execute("DELETE FROM exhibit_tags WHERE exhibit_id = ?", (exhibit_id,))
        await db.executemany(
            "INSERT OR IGNORE INTO exhibit_tags (exhibit_id, tag_id) "
            "SELECT ?, id FROM tags WHERE id = ?", [(exhibit_id, t) for t in ids])
        await db.commit()


async def save_tag_translations(tag_id, translations: dict):
    async with get_conn() as db:
        await db.execute("DELETE FROM tag_translations WHERE tag_id = ?", (int(tag_id),))
        await db.executemany(
            "INSERT INTO tag_translations (tag_id, lang, name) VALUES (?, ?, ?)",
            [(int(tag_id), lang, name) for lang, name in translations.items()
             if lang in TRANSLATION_LANGS and (name or "").strip()],
        )
        await db.commit()


async def list_untranslated_tag_ids():
    async with get_conn() as db:
        cur = await db.execute(
            """SELECT t.id FROM tags t
               WHERE (SELECT COUNT(*) FROM tag_translations tt WHERE tt.tag_id = t.id AND tt.lang != 'am') < ?
               ORDER BY t.sort_order""", (len(TRANSLATION_LANGS),))
        return [r[0] for r in await cur.fetchall()]


async def list_live_tags_for_lang(language_code):
    """Tags with at least one LIVE exhibit, with `live_count` and `label` in the visitor's
    language (Armenian name when there's no translation — or the language is Armenian)."""
    async with get_conn() as db:
        cur = await db.execute(
            """SELECT t.id, COUNT(e.id) AS live_count,
                      COALESCE(NULLIF(tt.name, ''), t.name_hy) AS label
               FROM tags t
               JOIN exhibit_tags et ON et.tag_id = t.id
               JOIN exhibits e ON e.id = et.exhibit_id AND e.status = 'live'
               LEFT JOIN tag_translations tt ON tt.tag_id = t.id AND tt.lang = ? AND ? != 'am'
               GROUP BY t.id ORDER BY t.sort_order, t.name_hy""",
            (language_code, language_code),
        )
        return await cur.fetchall()


async def get_tag_label(tag_id, language_code):
    async with get_conn() as db:
        cur = await db.execute(
            """SELECT COALESCE(NULLIF(tt.name, ''), t.name_hy) AS label FROM tags t
               LEFT JOIN tag_translations tt ON tt.tag_id = t.id AND tt.lang = ? AND ? != 'am'
               WHERE t.id = ?""", (language_code, language_code, int(tag_id)))
        row = await cur.fetchone()
        return row[0] if row else None


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
            "UPDATE audio_cache SET script_stale = 1, script_stale_reason = 'source', audio_stale = 1 "
            "WHERE exhibit_id = ?",
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
                                         draft_script_text, script_stale, script_stale_reason, audio_stale)
               VALUES (?, ?, ?, ?, 0, NULL, 1)
               ON CONFLICT(exhibit_id, language_code) DO UPDATE SET
                   draft_title_translated = excluded.draft_title_translated,
                   draft_script_text = excluded.draft_script_text,
                   script_stale = 0, script_stale_reason = NULL, audio_stale = 1""",
            [(exhibit_id, code, entry["title"], entry["narration"]) for code, entry in scripts.items()],
        )
        await db.commit()


async def save_translated_scripts(exhibit_id, scripts: dict):
    """'Translate from Armenian' result — replaces ONLY the languages in `scripts`
    (the caller passes the 9 non-Armenian ones; the Armenian row is never touched).
    Each replaced script is fresh (script_stale clears, 'armenian' reason cleared)
    and its draft audio no longer matches (audio_stale sets)."""
    assert "am" not in scripts, "translation must never overwrite the Armenian source"
    async with get_conn() as db:
        await db.executemany(
            """UPDATE audio_cache
               SET draft_title_translated = ?, draft_script_text = ?,
                   script_stale = 0, script_stale_reason = NULL, audio_stale = 1
               WHERE exhibit_id = ? AND language_code = ?""",
            [(e["title"], e["narration"], exhibit_id, code) for code, e in scripts.items()],
        )
        await db.commit()


async def save_draft_script_manual(exhibit_id, language_code, title_translated, script_text):
    """A staff member hand-edited one language's script directly — treated
    as the new authoritative text for that language (script_stale clears),
    but its existing draft audio no longer matches (audio_stale sets).

    If the language is Armenian (the translation source) AND its title/script
    actually changed, the other languages' scripts become outdated relative to
    it (script_stale=1, reason 'armenian' — shown as "Armenian changed —
    retranslate" and blocking publish). A language that is already stale for a
    reason keeps that reason. Editing any other language touches only its own row."""
    async with get_conn() as db:
        cur = await db.execute(
            "SELECT draft_title_translated, draft_script_text FROM audio_cache WHERE exhibit_id = ? AND language_code = ?",
            (exhibit_id, language_code),
        )
        before = await cur.fetchone()
        await db.execute(
            """UPDATE audio_cache
               SET draft_title_translated = ?, draft_script_text = ?,
                   script_stale = 0, script_stale_reason = NULL, audio_stale = 1
               WHERE exhibit_id = ? AND language_code = ?""",
            (title_translated, script_text, exhibit_id, language_code),
        )
        changed = before is None or (before["draft_title_translated"], before["draft_script_text"]) != (title_translated, script_text)
        if language_code == "am" and changed:
            await db.execute(
                """UPDATE audio_cache
                   SET script_stale = 1,
                       script_stale_reason = CASE WHEN script_stale = 1 THEN script_stale_reason ELSE 'armenian' END
                   WHERE exhibit_id = ? AND language_code != 'am' AND draft_script_text IS NOT NULL""",
            (exhibit_id,),
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
