"""
Background async worker: runs the OpenAI-translate + TTS + ffmpeg + cache
pipeline for a newly confirmed exhibit, without blocking the Admin FSM.

── Why in-process asyncio instead of Celery ────────────────────────────────
At this scale (staff add a handful of exhibits per week, not thousands of
concurrent jobs), Celery/RQ would mean running + operating a broker (Redis/
RabbitMQ) and a separate worker process for no real benefit. A single
asyncio.Queue consumed by a background task inside the same process as the
bot gives us concurrency (fan out the 10 languages per exhibit) without any
extra infrastructure.

The risk with a pure in-memory queue is that a bot restart mid-job loses
work silently. We avoid that by treating `generation_jobs` (Postgres/SQLite)
as the source of truth: every unit of work is inserted as a row *before* it
runs, and `on_startup` re-enqueues anything left in 'queued'/'running' state.
If job volume ever grows enough to need multiple machines, this queue can be
swapped for Celery/RQ later without changing `process_exhibit` itself — only
how it gets *invoked* changes.
"""
import asyncio
import json
from pathlib import Path

import openai
from aiogram.types import FSInputFile

from db import db
from utils.audio import to_telegram_voice, probe_duration_seconds
from utils.hayq_tts import synthesize_apittshy
from utils.qr import build_deep_link, generate_qr_png
from worker.prompts import SYSTEM_PROMPT, build_user_message, SUBMIT_SCRIPTS_TOOL, LANGUAGES

TMP_DIR = Path("tmp_audio")
QR_DIR = Path("qr_codes")
LOG_CHANNEL_ID = None  # set from config: a private channel used as blob storage
BOT_USERNAME = None    # set from config

TTS_ROUTING = {
    "am": "local_am",
    "en": "openai", "ru": "openai", "fr": "openai", "es": "openai",
    "de": "openai", "fa": "openai", "zh": "openai", "it": "openai", "el": "openai",
}

OPENAI_TTS_MODEL = "tts-1"
OPENAI_TTS_VOICE = "alloy"  # one voice for all 9 languages — OpenAI TTS reads the input text's own language
OPENAI_SCRIPTS_MODEL = "gpt-5.5"  # docent-script generation (all 10 languages, one call per exhibit)

MAX_CONCURRENT_TTS = 3  # respects provider rate limits while still parallelizing

_queue: asyncio.Queue[str] = asyncio.Queue()

_openai_client = None  # lazily constructed — openai.AsyncOpenAI() raises immediately if
                        # OPENAI_API_KEY isn't set, which would crash the whole bot at import time.


def _get_openai_client() -> openai.AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.AsyncOpenAI()
    return _openai_client


async def enqueue_exhibit(exhibit_id: str) -> None:
    """Called by the admin FSM handler right after 'Confirm'."""
    await db.enqueue_job(exhibit_id, job_type="qr")
    await db.enqueue_job(exhibit_id, job_type="translate")
    for lang in TTS_ROUTING:
        await db.enqueue_job(exhibit_id, job_type="tts", language_code=lang)
    await _queue.put(exhibit_id)


async def recover_unfinished_jobs() -> None:
    """Call once on bot startup, before the worker loop starts consuming."""
    for exhibit_id in await db.get_unfinished_exhibit_ids():
        await _queue.put(exhibit_id)


async def worker_loop(bot) -> None:
    """Long-running background task — start with asyncio.create_task in main.py."""
    while True:
        exhibit_id = await _queue.get()
        try:
            await process_exhibit(bot, exhibit_id)
        except Exception as exc:  # noqa: BLE001 — never let one bad exhibit kill the loop
            try:
                await notify_owners(bot, f"Exhibit {exhibit_id} pipeline crashed: {exc}")
            except Exception:  # noqa: BLE001 — a bad staff row must not kill the loop either
                pass
        finally:
            _queue.task_done()


async def process_exhibit(bot, exhibit_id: str) -> None:
    exhibit = await db.get_exhibit(exhibit_id)
    await db.set_exhibit_status(exhibit_id, "processing")

    # 1) QR code — cheap, do it first so staff get the printable asset fast
    #    even while translation/TTS are still running.
    deep_link = build_deep_link(BOT_USERNAME, exhibit_id)
    qr_path = QR_DIR / f"{exhibit_id}.png"
    generate_qr_png(deep_link, qr_path)
    await db.set_exhibit_qr(exhibit_id, str(qr_path), deep_link)
    if exhibit["created_by"] is not None:
        # Exhibits added via the web admin panel have no Telegram creator to DM —
        # they can see/download the QR from the exhibit's page instead.
        await bot.send_photo(
            exhibit["created_by"],
            photo=FSInputFile(qr_path),
            caption=f"QR code for exhibit {exhibit_id} — print and place next to the object.",
        )
    await db.mark_jobs_by_type(exhibit_id, "qr", status="success")

    # 2) One Claude call -> localized title + narration script for all 10 languages.
    try:
        scripts = await _generate_scripts(exhibit_id, exhibit["title_am"], exhibit["fact_sheet_am"])
    except Exception as exc:
        await db.mark_jobs_by_type(exhibit_id, "translate", status="failed", error_message=str(exc))
        await db.set_exhibit_status(exhibit_id, "failed")
        await notify_owners(bot, f"Exhibit {exhibit_id}: translation step failed — {exc}")
        return
    await db.mark_jobs_by_type(exhibit_id, "translate", status="success")

    # 3) Fan out TTS + ffmpeg + upload per language, bounded concurrency.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TTS)

    async def bound_tts(lang: str, entry: dict):
        async with semaphore:
            return await _tts_and_cache(bot, exhibit_id, lang, entry["title"], entry["narration"])

    results = await asyncio.gather(
        *(bound_tts(lang, entry) for lang, entry in scripts.items()),
        return_exceptions=True,
    )

    failures = [lang for lang, r in zip(scripts, results) if isinstance(r, Exception)]
    if failures:
        await db.set_exhibit_status(exhibit_id, "failed")
        await notify_owners(
            bot,
            f"Exhibit {exhibit_id} ({exhibit['title_am']}): {len(failures)} language(s) failed "
            f"({', '.join(failures)}). Retry with /retry {exhibit_id} <lang>.",
        )
    else:
        await db.set_exhibit_status(exhibit_id, "review")
        summary = "\n".join(f"{lang}: ok" for lang in scripts)
        await notify_owners(
            bot,
            f"Exhibit {exhibit_id} ({exhibit['title_am']}) is ready for review.\n\n"
            f"{summary}\n\nUse /review {exhibit_id} to proofread, then /publish {exhibit_id} to go live.",
        )


async def _generate_scripts(exhibit_id: str, title_am: str, fact_sheet_am: str) -> dict:
    client = _get_openai_client()
    response = await client.chat.completions.create(
        model=OPENAI_SCRIPTS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(exhibit_id, title_am, fact_sheet_am)},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": SUBMIT_SCRIPTS_TOOL["name"],
                "description": SUBMIT_SCRIPTS_TOOL["description"],
                "parameters": SUBMIT_SCRIPTS_TOOL["input_schema"],
            },
        }],
        tool_choice={"type": "function", "function": {"name": "submit_docent_scripts"}},
    )
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError("OpenAI response missing submit_docent_scripts tool call")
    return json.loads(tool_calls[0].function.arguments)["scripts"]


async def _synthesize_and_upload(bot, exhibit_id: str, lang: str, script_text: str) -> dict:
    """Synth -> ffmpeg -> upload to the log channel. Shared by both the old
    monolithic Telegram-FSM pipeline and the web admin's staged draft
    generation — everything above this (which columns the result lands in)
    is the caller's concern, not this function's."""
    provider = TTS_ROUTING[lang]
    raw_path = TMP_DIR / f"{exhibit_id}_{lang}_raw"
    ogg_path = TMP_DIR / f"{exhibit_id}_{lang}.ogg"
    try:
        if provider == "local_am":
            raw_path, voice_id = await _synthesize_local_am(script_text, raw_path)
        elif provider == "openai":
            raw_path, voice_id = await _synthesize_openai(script_text, lang, raw_path)
        else:  # elevenlabs, or future providers
            raw_path, voice_id = await _synthesize_elevenlabs(script_text, lang, raw_path)

        await to_telegram_voice(raw_path, ogg_path)
        duration = await probe_duration_seconds(ogg_path)
        sent = await bot.send_voice(LOG_CHANNEL_ID, voice=FSInputFile(ogg_path))
        return {
            "tts_provider": provider,
            "voice_id": voice_id,
            "telegram_file_id": sent.voice.file_id,
            "telegram_file_unique_id": sent.voice.file_unique_id,
            "duration_seconds": duration,
        }
    finally:
        raw_path.unlink(missing_ok=True)
        ogg_path.unlink(missing_ok=True)


async def _tts_and_cache(bot, exhibit_id: str, lang: str, title_translated: str, script_text: str) -> None:
    try:
        result = await _synthesize_and_upload(bot, exhibit_id, lang, script_text)
        await db.upsert_audio_cache(
            exhibit_id, lang,
            script_text=script_text,
            title_translated=title_translated,
            status="ready",
            error_message=None,  # clear any stale error from a prior failed attempt
            **result,
        )
        await db.mark_jobs_by_type(exhibit_id, "tts", status="success", language_code=lang)
    except Exception as exc:
        # keep script_text/title on failure too — they're the expensive-to-regenerate
        # part (an OpenAI call), TTS is what actually failed, so /retry can skip
        # straight to TTS.
        await db.upsert_audio_cache(
            exhibit_id, lang, script_text=script_text, title_translated=title_translated,
            tts_provider=TTS_ROUTING[lang], status="failed", error_message=str(exc),
        )
        await db.mark_jobs_by_type(exhibit_id, "tts", status="failed", language_code=lang, error_message=str(exc))
        raise


async def _tts_and_cache_draft(bot, exhibit_id: str, lang: str, title_translated: str, script_text: str) -> None:
    """Web admin's Stage 3 equivalent of _tts_and_cache — writes the draft_*
    shadow columns instead, so a live exhibit keeps serving its old audio
    until Publish promotes this draft."""
    try:
        result = await _synthesize_and_upload(bot, exhibit_id, lang, script_text)
        await db.upsert_audio_draft(
            exhibit_id, lang,
            script_text=script_text, title_translated=title_translated,
            status="ready", error_message=None, **result,
        )
        await db.clear_audio_stale(exhibit_id, lang)
    except Exception as exc:
        await db.upsert_audio_draft(
            exhibit_id, lang, script_text=script_text, title_translated=title_translated,
            tts_provider=TTS_ROUTING[lang], status="failed", error_message=str(exc),
        )
        raise


async def retry_language(bot, exhibit_id: str, lang: str) -> None:
    """Handler for the owner `/retry <exhibit_id> <lang>` command — works from
    both 'failed' and 'review' states. Reuses the already-generated title +
    script (no Claude call) and retries just the TTS step. Returns the exhibit
    to 'review' once no language is left failing."""
    cached = await db.get_audio_row(exhibit_id, lang)
    if cached is None or not cached["script_text"]:
        raise RuntimeError(f"No stored script for {exhibit_id}/{lang} — retry full exhibit instead")

    await _tts_and_cache(bot, exhibit_id, lang, cached["title_translated"], cached["script_text"])

    still_failed = await db.get_failed_languages(exhibit_id)
    if not still_failed:
        await db.set_exhibit_status(exhibit_id, "review")
        await notify_owners(bot, f"Exhibit {exhibit_id} is back in review after retrying {lang}.")
    else:
        await notify_owners(
            bot,
            f"Exhibit {exhibit_id}: {lang} retried successfully, still failing: {', '.join(still_failed)}.",
        )


# ── Web admin staged workflow ───────────────────────────────────────────
# Stage 2 (Generate scripts), Stage 3 (Generate audio) and Publish, called
# directly from webapp/app.py's routes — no queue, since each is a single
# staff-initiated action the staff waits on, not a fire-and-forget job like
# the Telegram FSM's enqueue_exhibit. All draft_* writes go through
# db.upsert_audio_draft / db.save_draft_scripts; the published columns
# bot/visitor.py reads are only ever touched by db.promote_draft_to_live.

async def generate_scripts_draft(exhibit_id: str) -> None:
    """Stage 2 — one OpenAI call, all 10 languages' draft scripts at once,
    exactly like the old pipeline's translate step, just not auto-triggered."""
    exhibit = await db.get_exhibit(exhibit_id)
    scripts = await _generate_scripts(exhibit_id, exhibit["title_am"], exhibit["fact_sheet_am"])
    await db.save_draft_scripts(exhibit_id, scripts)


async def generate_audio_draft(bot, exhibit_id: str, langs: list[str]) -> list[str]:
    """Stage 3 — TTS only for the given languages (the caller is expected to
    have already filtered to missing-or-stale ones). Returns the subset that
    failed. Reuses draft_script_text/draft_title_translated as the input —
    whatever Stage 2 (or a manual per-language edit) most recently set."""
    rows = await db.get_all_audio_rows(exhibit_id)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TTS)

    async def bound(lang: str):
        row = rows[lang]
        async with semaphore:
            await _tts_and_cache_draft(bot, exhibit_id, lang, row["draft_title_translated"], row["draft_script_text"])

    results = await asyncio.gather(*(bound(lang) for lang in langs), return_exceptions=True)
    return [lang for lang, r in zip(langs, results) if isinstance(r, Exception)]


def publish_readiness(rows: dict) -> list[str]:
    """Checks every expected language's draft against the staleness rules
    and returns a human-readable reason per language that isn't ready to
    publish yet — empty list means Publish may proceed."""
    problems = []
    for code, name in LANGUAGES.items():
        row = rows.get(code)
        if row is None or row["draft_status"] != "ready":
            problems.append(f"{name} ({code}): audio missing")
        elif row["script_stale"]:
            problems.append(f"{name} ({code}): script outdated — generate scripts")
        elif row["audio_stale"]:
            problems.append(f"{name} ({code}): audio outdated — generate audio")
    return problems


async def publish_draft(exhibit_id: str) -> list[str]:
    """Publish — promotes every language's draft onto the published columns
    in one shot, then flips the exhibit live. Refuses (returning the reasons)
    if anything is stale or missing, so visitors can never get a half-updated
    exhibit. Works identically whether this is the first publish or a
    republish of an already-live exhibit."""
    rows = await db.get_all_audio_rows(exhibit_id)
    problems = publish_readiness(rows)
    if problems:
        return problems
    await db.promote_draft_to_live(exhibit_id)
    await db.set_exhibit_status(exhibit_id, "live")
    return []


# ── TTS provider adapters — fill in real SDK calls once vendor is picked ───
ARMENIAN_TTS_VOICE = "biverman"  # starting default from the hayq.ican24.net apittshy voices — swap here if the client picks differently


async def _synthesize_local_am(text: str, dst_path: Path) -> tuple[Path, str]:
    # synthesize_apittshy is a blocking `requests` call — run off the event loop
    await asyncio.to_thread(synthesize_apittshy, text, ARMENIAN_TTS_VOICE, dst_path)
    return dst_path, ARMENIAN_TTS_VOICE


async def _synthesize_openai(text: str, lang: str, dst_path: Path) -> tuple[Path, str]:
    client = _get_openai_client()
    response = await client.audio.speech.create(
        model=OPENAI_TTS_MODEL,
        voice=OPENAI_TTS_VOICE,
        input=text,
        response_format="mp3",
    )
    await response.astream_to_file(dst_path)
    return dst_path, OPENAI_TTS_VOICE


async def _synthesize_elevenlabs(text: str, lang: str, dst_path: Path) -> tuple[Path, str]:
    """TODO: ElevenLabs multilingual model call, if used instead of/alongside OpenAI."""
    raise NotImplementedError


async def notify_owners(bot, message: str) -> None:
    """Pipeline results and /retry outcomes only go to owners — they're the
    ones who can act on them via /review, /publish, and /retry."""
    rows = await db.get_staff_by_role("owner")
    for row in rows:
        try:
            await bot.send_message(row["telegram_user_id"], message)
        except Exception:  # noqa: BLE001 — one unreachable owner must not block the rest
            pass
