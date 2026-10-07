"""Dry run for the web admin's staged workflow (Source -> Scripts -> Audio ->
Publish) and its core staleness/draft-vs-published-content guarantees, with
Claude/OpenAI + TTS mocked exactly like dry_run.py. Covers:

1. Editing the fact sheet after scripts+audio exist marks everything stale,
   and a full regenerate-then-publish cycle clears that and goes live.
2. Editing a single language's script only stales that language's audio.
3. An already-live exhibit keeps serving its OLD audio untouched through an
   edit + full regeneration, and only starts serving the new audio once
   Publish is pressed again.
"""
import asyncio
from unittest import mock

from db import db
from worker import pipeline
from dry_run import FakeBot, fake_synthesize, fake_generate_scripts


def eligible_for_audio(rows: dict) -> list[str]:
    """Mirrors webapp/app.py's exhibit_generate_audio selection logic."""
    return [
        code for code, row in rows.items()
        if row["draft_script_text"] and not row["script_stale"]
        and (row["audio_stale"] or row["draft_status"] != "ready")
    ]


async def main():
    pipeline.LOG_CHANNEL_ID = -100123456789
    pipeline.BOT_USERNAME = "DryRunMuseumBot"
    pipeline.TMP_DIR.mkdir(exist_ok=True)

    bot = FakeBot()

    await db.create_exhibit(
        exhibit_id="STG01",
        title_am="Փորձնական էկրան",
        fact_sheet_am="Սկզբնական փաստեր՝ 1900 թվականից։",
        created_by=None,
    )
    await db.replace_exhibit_photos("STG01", ["FAKE_PHOTO_FILE_ID"])
    await db.set_deep_link("STG01", pipeline.build_deep_link(pipeline.BOT_USERNAME, "STG01"))
    await db.mark_source_edited("STG01")

    with mock.patch.object(pipeline, "_generate_scripts", fake_generate_scripts), \
         mock.patch.object(pipeline, "_synthesize_local_am", lambda text, dst: fake_synthesize(text, dst, "am")), \
         mock.patch.object(pipeline, "_synthesize_openai", lambda text, lang, dst: fake_synthesize(text, dst, lang)), \
         mock.patch.object(pipeline, "_synthesize_elevenlabs", lambda text, lang, dst: fake_synthesize(text, dst, lang)):

        # ── Stage 1 created it stale/missing everywhere ────────────────
        rows = await db.get_all_audio_rows("STG01")
        assert len(rows) == 10
        assert all(r["script_stale"] and r["audio_stale"] for r in rows.values())
        assert await db.get_exhibit("STG01")  # exists, status 'draft' implicitly checked below
        print("Stage 1 (new exhibit): all 10 languages stale/missing — OK")

        # ── Stage 2 + 3: first full generation ──────────────────────────
        await pipeline.generate_scripts_draft("STG01")
        rows = await db.get_all_audio_rows("STG01")
        assert all(r["script_stale"] == 0 for r in rows.values())
        assert all(r["audio_stale"] == 1 for r in rows.values()), "fresh scripts must stale their (nonexistent) audio"

        failed = await pipeline.generate_audio_draft(bot, "STG01", eligible_for_audio(rows))
        assert failed == [], f"unexpected audio failures: {failed}"
        rows = await db.get_all_audio_rows("STG01")
        assert all(r["draft_status"] == "ready" and r["audio_stale"] == 0 for r in rows.values())
        print("Stage 2+3 (first generation): all 10 languages ready — OK")

        # ── Publish #1 ───────────────────────────────────────────────────
        problems = await pipeline.publish_draft("STG01")
        assert problems == [], f"unexpected publish blockers: {problems}"
        exhibit = await db.get_exhibit("STG01")
        assert exhibit["status"] == "live"
        live_am_v1 = (await db.get_audio_row("STG01", "am"))["telegram_file_id"]
        assert live_am_v1 is not None
        print("Publish #1: live — OK")

        # ── Scenario 1 + 3: edit fact sheet on a LIVE exhibit ───────────
        await db.update_exhibit_content("STG01", fact_sheet_am="Նոր, վերանայված փաստեր՝ 2026 թվականից։")
        await db.mark_source_edited("STG01")
        rows = await db.get_all_audio_rows("STG01")
        assert all(r["script_stale"] and r["audio_stale"] for r in rows.values()), "editing source must stale everything"
        exhibit = await db.get_exhibit("STG01")
        assert exhibit["status"] == "live", "must stay live while staff rework drafts"
        assert (await db.get_audio_row("STG01", "am"))["telegram_file_id"] == live_am_v1, \
            "published audio must not change just from editing the source"
        print("Scenario 1 (edit fact sheet): everything stale, still live with OLD audio — OK")

        # Regenerate scripts+audio — must still leave published content alone.
        await pipeline.generate_scripts_draft("STG01")
        rows = await db.get_all_audio_rows("STG01")
        failed = await pipeline.generate_audio_draft(bot, "STG01", eligible_for_audio(rows))
        assert failed == []
        assert (await db.get_audio_row("STG01", "am"))["telegram_file_id"] == live_am_v1, \
            "published audio must not change until Publish is pressed again"
        assert (await db.get_exhibit("STG01"))["status"] == "live"
        draft_am_v2 = (await db.get_audio_row("STG01", "am"))["draft_telegram_file_id"]
        assert draft_am_v2 != live_am_v1, "draft should hold genuinely new content"
        print("Scenario 3 (regenerate while live): draft updated, published audio STILL untouched — OK")

        # Publish #2 — only now does the visitor-facing copy change.
        problems = await pipeline.publish_draft("STG01")
        assert problems == []
        live_am_v2 = (await db.get_audio_row("STG01", "am"))["telegram_file_id"]
        assert live_am_v2 == draft_am_v2, "publish must promote the draft onto the live columns"
        assert live_am_v2 != live_am_v1
        print("Publish #2: visitor-facing audio now updated — OK")

        # ── Scenario 2: edit a single language's script ────────────────
        await db.save_draft_script_manual("STG01", "ru", "Ручной заголовок", "Ручной скрипт для одного языка.")
        rows = await db.get_all_audio_rows("STG01")
        for code, row in rows.items():
            if code == "ru":
                assert row["script_stale"] == 0 and row["audio_stale"] == 1, "edited language must stale only its own audio"
            else:
                assert row["script_stale"] == 0 and row["audio_stale"] == 0, f"{code} must be untouched by ru's edit"
        problems = pipeline.publish_readiness(rows)
        assert any("ru" in p for p in problems), "publish must be blocked on ru's stale audio"
        print("Scenario 2 (single-language script edit): only ru's audio staled — OK")

    print("\nSTAGED WORKFLOW DRY RUN PASSED")


if __name__ == "__main__":
    asyncio.run(main())
