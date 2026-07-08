"""
Background async worker: runs the Claude-translate + TTS + ffmpeg + cache
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
from pathlib import Path

import anthropic

from db import db
from utils.audio import to_telegram_voice, probe_duration_seconds
from utils.qr import build_deep_link, generate_qr_png
from worker.prompts import SYSTEM_PROMPT, build_user_message, SUBMIT_SCRIPTS_TOOL

TMP_DIR = Path("tmp_audio")
QR_DIR = Path("qr_codes")
LOG_CHANNEL_ID = None  # set from config: a private channel used as blob storage
BOT_USERNAME = None    # set from config

TTS_ROUTING = {
    "am": "local_am",
    "en": "openai", "ru": "openai", "fr": "openai", "es": "openai",
    "de": "openai", "fa": "openai", "zh": "openai", "it": "openai", "el": "openai",
}

MAX_CONCURRENT_TTS = 3  # respects provider rate limits while still parallelizing

_queue: asyncio.Queue[str] = asyncio.Queue()
_claude = anthropic.AsyncAnthropic()  # picks up ANTHROPIC_API_KEY from env


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
            await notify_admins(bot, f"Exhibit {exhibit_id} pipeline crashed: {exc}")
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
    await bot.send_photo(
        exhibit["created_by"],
        photo=open(qr_path, "rb"),
        caption=f"QR code for exhibit {exhibit_id} — print and place next to the object.",
    )
    await db.mark_jobs_by_type(exhibit_id, "qr", status="success")

    # 2) One Claude call -> narration script for all 10 languages at once.
    try:
        scripts = await _generate_scripts(exhibit_id, exhibit["title_am"], exhibit["fact_sheet_am"])
    except Exception as exc:
        await db.mark_jobs_by_type(exhibit_id, "translate", status="failed", error_message=str(exc))
        await db.set_exhibit_status(exhibit_id, "failed")
        await notify_admins(bot, f"Exhibit {exhibit_id}: translation step failed — {exc}")
        return
    await db.mark_jobs_by_type(exhibit_id, "translate", status="success")

    # 3) Fan out TTS + ffmpeg + upload per language, bounded concurrency.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TTS)

    async def bound_tts(lang: str, text: str):
        async with semaphore:
            return await _tts_and_cache(bot, exhibit_id, lang, text)

    results = await asyncio.gather(
        *(bound_tts(lang, text) for lang, text in scripts.items()),
        return_exceptions=True,
    )

    failures = [lang for lang, r in zip(scripts, results) if isinstance(r, Exception)]
    if failures:
        await db.set_exhibit_status(exhibit_id, "failed")
        await notify_admins(
            bot,
            f"Exhibit {exhibit_id}: {len(failures)} language(s) failed "
            f"({', '.join(failures)}). Retry with /retry {exhibit_id} <lang>.",
        )
    else:
        await db.set_exhibit_status(exhibit_id, "ready")
        await notify_admins(bot, f"Exhibit {exhibit_id} is live — QR code is ready to print.")


async def _generate_scripts(exhibit_id: str, title_am: str, fact_sheet_am: str) -> dict:
    response = await _claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[SUBMIT_SCRIPTS_TOOL],
        tool_choice={"type": "tool", "name": "submit_docent_scripts"},
        messages=[{"role": "user", "content": build_user_message(exhibit_id, title_am, fact_sheet_am)}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_docent_scripts":
            return block.input["scripts"]
    raise RuntimeError("Claude response missing submit_docent_scripts tool call")


async def _tts_and_cache(bot, exhibit_id: str, lang: str, script_text: str) -> None:
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

        sent = await bot.send_voice(LOG_CHANNEL_ID, voice=open(ogg_path, "rb"))
        await db.upsert_audio_cache(
            exhibit_id, lang,
            script_text=script_text,
            tts_provider=provider,
            voice_id=voice_id,
            telegram_file_id=sent.voice.file_id,
            telegram_file_unique_id=sent.voice.file_unique_id,
            duration_seconds=duration,
            status="ready",
            error_message=None,  # clear any stale error from a prior failed attempt
        )
        await db.mark_jobs_by_type(exhibit_id, "tts", status="success", language_code=lang)
    except Exception as exc:
        # keep script_text on failure too — it's the expensive-to-regenerate part
        # (a Claude call), TTS is what actually failed, so /retry can skip straight to TTS.
        await db.upsert_audio_cache(
            exhibit_id, lang, script_text=script_text, tts_provider=provider,
            status="failed", error_message=str(exc),
        )
        await db.mark_jobs_by_type(exhibit_id, "tts", status="failed", language_code=lang, error_message=str(exc))
        raise
    finally:
        raw_path.unlink(missing_ok=True)
        ogg_path.unlink(missing_ok=True)


async def retry_language(bot, exhibit_id: str, lang: str) -> None:
    """Handler for the admin `/retry <exhibit_id> <lang>` command. Reuses the
    already-generated script (no Claude call) and retries just the TTS step."""
    cached = await db.get_audio_row(exhibit_id, lang)
    if cached is None or not cached["script_text"]:
        raise RuntimeError(f"No stored script for {exhibit_id}/{lang} — retry full exhibit instead")

    await _tts_and_cache(bot, exhibit_id, lang, cached["script_text"])

    if await db.all_languages_ready(exhibit_id, expected_count=len(TTS_ROUTING)):
        await db.set_exhibit_status(exhibit_id, "ready")
        await notify_admins(bot, f"Exhibit {exhibit_id} is now fully ready after retrying {lang}.")


# ── TTS provider adapters — fill in real SDK calls once vendor is picked ───
async def _synthesize_local_am(text: str, dst_path: Path) -> tuple[Path, str]:
    """TODO: call the museum's proprietary Armenian TTS algorithm/binary."""
    raise NotImplementedError


async def _synthesize_openai(text: str, lang: str, dst_path: Path) -> tuple[Path, str]:
    """TODO: openai.audio.speech.create(model='tts-1', voice=..., input=text)."""
    raise NotImplementedError


async def _synthesize_elevenlabs(text: str, lang: str, dst_path: Path) -> tuple[Path, str]:
    """TODO: ElevenLabs multilingual model call, if used instead of/alongside OpenAI."""
    raise NotImplementedError


async def notify_admins(bot, message: str) -> None:
    async with db.get_conn() as conn:
        cur = await conn.execute("SELECT telegram_user_id FROM staff_users")
        rows = await cur.fetchall()
    for row in rows:
        await bot.send_message(row["telegram_user_id"], message)
