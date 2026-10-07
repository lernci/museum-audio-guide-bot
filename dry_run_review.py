"""Fourth scenario: the full review workflow end to end — draft -> processing
-> review -> publish -> live — exercising the actual bot.admin/bot.visitor
handlers (not just worker.pipeline), with Claude + TTS mocked as in dry_run.py.
Confirms the visitor deep-link is gated on status == 'live' at every earlier
stage, and that /publish is what flips it.
"""
import asyncio
from types import SimpleNamespace
from unittest import mock

from db import db
from worker import pipeline
from bot import admin, visitor
from dry_run import FakeBot, fake_synthesize, fake_generate_scripts


class FakeMessage:
    def __init__(self, user_id, text=""):
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.sent = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)


async def main():
    pipeline.LOG_CHANNEL_ID = -100123456789
    pipeline.BOT_USERNAME = "DryRunMuseumBot"
    pipeline.TMP_DIR.mkdir(exist_ok=True)
    pipeline.QR_DIR.mkdir(exist_ok=True)

    bot = FakeBot()

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO staff_users (telegram_user_id, full_name, role) VALUES (?, ?, ?)",
            (999, "Dry Run Owner", "owner"),
        )
        await conn.commit()

    await db.create_exhibit(
        exhibit_id="DRY04", title_am="Չորրորդ ցուցանմուշ", fact_sheet_am="Փաստեր 1950 թվականից։",
        created_by=999,
    )
    await db.replace_exhibit_photos("DRY04", ["FAKE_PHOTO_FILE_ID_4"])

    exhibit = await db.get_exhibit("DRY04")
    print("status after create (expect 'draft'):", exhibit["status"])
    assert exhibit["status"] == "draft"

    # visitor tries before the pipeline has even started -> not-ready message, no language picker
    msg = FakeMessage(user_id=1001)
    await visitor.start_with_exhibit(msg, SimpleNamespace(args="exh_DRY04"))
    assert msg.sent and "Please choose your language" not in msg.sent[0]
    print("visitor pre-pipeline: not-ready message, no language picker (expected)")

    with mock.patch.object(pipeline, "_generate_scripts", fake_generate_scripts), \
         mock.patch.object(pipeline, "_synthesize_local_am", lambda text, dst: fake_synthesize(text, dst, "am")), \
         mock.patch.object(pipeline, "_synthesize_openai", lambda text, lang, dst: fake_synthesize(text, dst, lang)), \
         mock.patch.object(pipeline, "_synthesize_elevenlabs", lambda text, lang, dst: fake_synthesize(text, dst, lang)):

        await pipeline.enqueue_exhibit("DRY04")
        exhibit_id = await pipeline._queue.get()
        await pipeline.process_exhibit(bot, exhibit_id)
        pipeline._queue.task_done()

    exhibit = await db.get_exhibit("DRY04")
    print("status after pipeline (expect 'review'):", exhibit["status"])
    assert exhibit["status"] == "review"

    # visitor still blocked while it's in review, awaiting proofreading
    msg = FakeMessage(user_id=1001)
    await visitor.start_with_exhibit(msg, SimpleNamespace(args="exh_DRY04"))
    assert msg.sent and "Please choose your language" not in msg.sent[0]
    print("visitor during review: still not-ready (expected)")

    # owner publishes
    msg = FakeMessage(user_id=999, text="/publish DRY04")
    await admin.publish_exhibit(msg, SimpleNamespace(args="DRY04"))
    print("publish response:", msg.sent)
    assert msg.sent and "live" in msg.sent[0].lower()

    exhibit = await db.get_exhibit("DRY04")
    print("status after publish (expect 'live'):", exhibit["status"])
    assert exhibit["status"] == "live"

    # visitor now gets the language picker
    msg = FakeMessage(user_id=1001)
    await visitor.start_with_exhibit(msg, SimpleNamespace(args="exh_DRY04"))
    print("visitor post-publish:", msg.sent)
    assert msg.sent and "Please choose your language" in msg.sent[0]

    print("\nFULL REVIEW-WORKFLOW DRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
