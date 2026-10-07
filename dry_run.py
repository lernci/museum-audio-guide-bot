"""Dry run: exercises the real pipeline code (DB, QR, ffmpeg, caching,
concurrency, job recovery) with Claude + all three TTS providers mocked out,
and a fake Bot standing in for Telegram. No network calls, no API keys.
"""
import asyncio
import wave
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from db import db
from worker import pipeline


# ── Fakes ───────────────────────────────────────────────────────────────
class FakeBot:
    def __init__(self):
        self.sent = []
        self._voice_counter = 0

    async def send_photo(self, chat_id, photo, caption=None):
        self.sent.append(("photo", chat_id, caption))
        return SimpleNamespace()

    async def send_voice(self, chat_id, voice):
        self._voice_counter += 1
        self.sent.append(("voice", chat_id))
        return SimpleNamespace(voice=SimpleNamespace(
            file_id=f"FAKE_FILE_ID_{self._voice_counter}",
            file_unique_id=f"FAKE_UNIQUE_{self._voice_counter}",
        ))

    async def send_message(self, chat_id, text):
        self.sent.append(("message", chat_id, text))


def make_silence_wav(dst: Path, seconds: float = 1.0):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))
    return dst


async def fake_synthesize(text: str, dst_path: Path, tag: str):
    raw = dst_path.with_suffix(".wav")
    make_silence_wav(raw, seconds=1.0)
    return raw, f"mock-voice-{tag}"


async def fake_generate_scripts(exhibit_id, title_am, fact_sheet_am):
    from worker.prompts import LANGUAGES
    return {
        code: {
            "title": f"[{name}] {title_am}",
            "narration": f"[{name} mock narration for {title_am}] {fact_sheet_am[:40]}",
        }
        for code, name in LANGUAGES.items()
    }


# ── Dry run ──────────────────────────────────────────────────────────────
async def main():
    pipeline.LOG_CHANNEL_ID = -100123456789
    pipeline.BOT_USERNAME = "DryRunMuseumBot"
    pipeline.TMP_DIR.mkdir(exist_ok=True)
    pipeline.QR_DIR.mkdir(exist_ok=True)

    bot = FakeBot()

    # seed a staff user (FK target for created_by, and notify_owners recipient)
    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (999, "Dry Run Curator", "owner"),
        )
        await conn.commit()

    await db.create_exhibit(
        exhibit_id="DRY01",
        title_am="Փորձնական ցուցանմուշ",
        fact_sheet_am="Այս իրը պատրաստվել է 1890 թվականին, նվիրաբերվել է թանգարանին 1975 թ.",
        created_by=999,
    )
    await db.replace_exhibit_photos("DRY01", ["FAKE_PHOTO_FILE_ID"])

    with mock.patch.object(pipeline, "_generate_scripts", fake_generate_scripts), \
         mock.patch.object(pipeline, "_synthesize_local_am", lambda text, dst: fake_synthesize(text, dst, "am")), \
         mock.patch.object(pipeline, "_synthesize_openai", lambda text, lang, dst: fake_synthesize(text, dst, lang)), \
         mock.patch.object(pipeline, "_synthesize_elevenlabs", lambda text, lang, dst: fake_synthesize(text, dst, lang)):

        await pipeline.enqueue_exhibit("DRY01")

        # run one worker iteration directly instead of the infinite loop
        exhibit_id = await pipeline._queue.get()
        await pipeline.process_exhibit(bot, exhibit_id)
        pipeline._queue.task_done()

    # ── Assertions / report ──────────────────────────────────────────────
    exhibit = await db.get_exhibit("DRY01")
    print("exhibit status:", exhibit["status"])
    print("qr_code_path:", exhibit["qr_code_path"], "exists:", Path(exhibit["qr_code_path"]).exists())
    print("deep_link:", exhibit["deep_link"])

    async with db.get_conn() as conn:
        cur = await conn.execute("SELECT language_code, status, tts_provider, telegram_file_id, duration_seconds FROM audio_cache WHERE exhibit_id = 'DRY01' ORDER BY language_code")
        rows = await cur.fetchall()
    print(f"\naudio_cache rows: {len(rows)}")
    for r in rows:
        print(" ", dict(r))

    async with db.get_conn() as conn:
        cur = await conn.execute("SELECT job_type, language_code, status, attempts FROM generation_jobs WHERE exhibit_id = 'DRY01' ORDER BY id")
        jobs = await cur.fetchall()
    print(f"\ngeneration_jobs rows: {len(jobs)}, all success: {all(j['status'] == 'success' for j in jobs)}")
    for j in jobs:
        print(" ", dict(j))

    unfinished = await db.get_unfinished_exhibit_ids()
    print("\nunfinished exhibit ids after full success (should be empty):", unfinished)

    print("\nFakeBot calls:", len(bot.sent))
    for call in bot.sent[:5]:
        print(" ", call)

    cached = await db.get_cached_voice("DRY01", "en")
    print("\nvisitor-side lookup for 'en':", dict(cached) if cached else None)

    assert exhibit["status"] == "review", "expected exhibit status to be review"
    assert len(rows) == 10, f"expected 10 cached languages, got {len(rows)}"
    assert all(r["status"] == "ready" for r in rows), "not all languages cached as ready"
    assert len(jobs) == 12, f"expected 12 job rows, got {len(jobs)}"
    assert all(j["status"] == "success" for j in jobs), "not all jobs marked success"
    assert unfinished == [], f"expected no unfinished exhibits after success, got {unfinished}"
    print("\nDRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
