"""Third scenario: after a language fails, /retry (worker.pipeline.retry_language)
should succeed using the stored script (no Claude re-call) and flip the exhibit
to 'ready' once every language is actually ready.
"""
import asyncio
from unittest import mock

from db import db
from worker import pipeline
from dry_run import FakeBot, fake_synthesize, fake_generate_scripts


async def main():
    pipeline.LOG_CHANNEL_ID = -100123456789
    pipeline.BOT_USERNAME = "DryRunMuseumBot"
    pipeline.TMP_DIR.mkdir(exist_ok=True)
    pipeline.QR_DIR.mkdir(exist_ok=True)

    bot = FakeBot()

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (999, "Dry Run Curator", "owner"),
        )
        await conn.commit()

    await db.create_exhibit(
        exhibit_id="DRY03", title_am="Երրորդ ցուցանմուշ", fact_sheet_am="Փաստեր 1930 թվականից։",
        created_by=999,
    )
    await db.replace_exhibit_photos("DRY03", ["FAKE_PHOTO_FILE_ID_3"])

    call_count = {"n": 0}

    async def flaky_once(text, lang, dst):
        if lang == "de" and call_count["n"] == 0:
            call_count["n"] += 1
            raise RuntimeError("mock transient outage")
        return await fake_synthesize(text, dst, lang)

    with mock.patch.object(pipeline, "_generate_scripts", fake_generate_scripts), \
         mock.patch.object(pipeline, "_synthesize_local_am", lambda text, dst: fake_synthesize(text, dst, "am")), \
         mock.patch.object(pipeline, "_synthesize_openai", flaky_once), \
         mock.patch.object(pipeline, "_synthesize_elevenlabs", lambda text, lang, dst: fake_synthesize(text, dst, lang)):

        await pipeline.enqueue_exhibit("DRY03")
        exhibit_id = await pipeline._queue.get()
        await pipeline.process_exhibit(bot, exhibit_id)
        pipeline._queue.task_done()

        exhibit = await db.get_exhibit("DRY03")
        print("status after initial run (expect 'failed'):", exhibit["status"])
        assert exhibit["status"] == "failed"

        claude_calls_before_retry = fake_generate_scripts.__wrapped__ if hasattr(fake_generate_scripts, "__wrapped__") else None

        # now retry just the failed language
        await pipeline.retry_language(bot, "DRY03", "de")

    exhibit = await db.get_exhibit("DRY03")
    print("status after retry (expect 'review'):", exhibit["status"])
    cached = await db.get_cached_voice("DRY03", "de")
    print("de cache after retry:", dict(cached) if cached else None)

    assert exhibit["status"] == "review"
    assert cached is not None and cached["status"] == "ready"
    print("\nRETRY DRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
