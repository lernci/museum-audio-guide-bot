"""Second dry-run scenario: one TTS language fails. Confirms partial failure
is isolated (other 9 languages still cache correctly) and is reported/
retryable rather than silently corrupting state.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from db import db
from worker import pipeline
from dry_run import FakeBot, fake_synthesize, fake_generate_scripts


async def flaky_openai(text, lang, dst):
    if lang == "fr":
        raise RuntimeError("mock OpenAI TTS outage")
    return await fake_synthesize(text, dst, lang)


async def main():
    pipeline.LOG_CHANNEL_ID = -100123456789
    pipeline.BOT_USERNAME = "DryRunMuseumBot"
    pipeline.TMP_DIR.mkdir(exist_ok=True)
    pipeline.QR_DIR.mkdir(exist_ok=True)

    bot = FakeBot()

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (999, "Dry Run Curator", "admin"),
        )
        await conn.commit()

    await db.create_exhibit(
        exhibit_id="DRY02",
        title_am="Երկրորդ փորձնական ցուցանմուշ",
        fact_sheet_am="Ստեղծվել է 1920 թվականին։",
        photo_file_id="FAKE_PHOTO_FILE_ID_2",
        created_by=999,
    )

    with mock.patch.object(pipeline, "_generate_scripts", fake_generate_scripts), \
         mock.patch.object(pipeline, "_synthesize_local_am", lambda text, dst: fake_synthesize(text, dst, "am")), \
         mock.patch.object(pipeline, "_synthesize_openai", flaky_openai), \
         mock.patch.object(pipeline, "_synthesize_elevenlabs", lambda text, lang, dst: fake_synthesize(text, dst, lang)):

        await pipeline.enqueue_exhibit("DRY02")
        exhibit_id = await pipeline._queue.get()
        await pipeline.process_exhibit(bot, exhibit_id)
        pipeline._queue.task_done()

    exhibit = await db.get_exhibit("DRY02")
    print("exhibit status (expect 'failed'):", exhibit["status"])

    async with db.get_conn() as conn:
        cur = await conn.execute(
            "SELECT language_code, status FROM audio_cache WHERE exhibit_id = 'DRY02' ORDER BY language_code"
        )
        rows = await cur.fetchall()
    ready = [r["language_code"] for r in rows if r["status"] == "ready"]
    failed = [r["language_code"] for r in rows if r["status"] == "failed"]
    print("ready languages:", ready)
    print("failed languages:", failed)

    admin_messages = [c for c in bot.sent if c[0] == "message"]
    print("\nadmin notifications sent:")
    for m in admin_messages:
        print(" ", m)

    assert exhibit["status"] == "failed"
    assert failed == ["fr"], f"expected only 'fr' to fail, got {failed}"
    assert len(ready) == 9, f"expected 9 languages still ready despite fr failing, got {len(ready)}"
    assert any("fr" in m[2] for m in admin_messages), "admin should be told which language failed"
    print("\nFAILURE-PATH DRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
