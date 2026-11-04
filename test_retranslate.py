"""Translate-from-Armenian tests (throwaway DB): edit Armenian -> 9 outdated -> translate ->
only the 9 replaced, Armenian intact -> audio staleness right -> publish blocking; plus the
'Generate scripts' overwrite confirmation. OpenAI + TTS are mocked."""
import asyncio, os, tempfile
from unittest import mock

TMP = tempfile.mkdtemp()
import bcrypt
os.environ.update(WEBAPP_ADMIN_USER="t", WEBAPP_SESSION_SECRET="s",
                  WEBAPP_ADMIN_PASSWORD_HASH=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
from db import db, migrate
from worker import pipeline
from worker.prompts import LANGUAGES
from dry_run import FakeBot, fake_synthesize

OTHERS = [c for c in LANGUAGES if c != "am"]
pipeline.LOG_CHANNEL_ID = -1
use = lambda p: (setattr(db, "DB_PATH", p), setattr(migrate, "DB_PATH", p))


async def fake_generate(exhibit_id, title_am, fact_sheet_am):
    return {c: {"title": f"T-{c}", "narration": f"original narration {c}"} for c in LANGUAGES}

translate_calls = []
async def fake_translate(exhibit_id, title_am, script_am, codes):
    translate_calls.append((title_am, script_am, list(codes)))
    return {c: {"title": f"TT-{c}", "narration": f"translated from: {script_am} [{c}]"} for c in codes}

MOCKS = dict(
    _generate_scripts=fake_generate, _translate_scripts=fake_translate,
    _synthesize_local_am=lambda t, d: fake_synthesize(t, d, "am"),
    _synthesize_openai=lambda t, l, d: fake_synthesize(t, d, l),
    _synthesize_elevenlabs=lambda t, l, d: fake_synthesize(t, d, l),
)


async def clean_exhibit(eid, bot):
    """Exhibit with all 10 scripts + audio generated: publish-ready."""
    await db.create_exhibit(eid, "Վերնագիր", "փաստեր", created_by=None)
    await db.mark_source_edited(eid)
    await pipeline.generate_scripts_draft(eid)
    failed = await pipeline.generate_audio_draft(bot, eid, list(LANGUAGES))
    assert failed == []
    rows = await db.get_all_audio_rows(eid)
    assert pipeline.publish_readiness(rows) == []
    return rows


async def test_core():
    use(os.path.join(TMP, "core.sqlite3")); await migrate.migrate()
    pipeline.TMP_DIR.mkdir(exist_ok=True)
    bot = FakeBot()
    with mock.patch.multiple(pipeline, **MOCKS):
        rows0 = await clean_exhibit("001", bot)

        # 1) identical re-save of the Armenian script: nothing becomes outdated
        am = rows0["am"]
        await db.save_draft_script_manual("001", "am", am["draft_title_translated"], am["draft_script_text"])
        rows = await db.get_all_audio_rows("001")
        assert all(rows[c]["script_stale"] == 0 for c in OTHERS), "unchanged Armenian must not stale the others"
        await pipeline.generate_audio_draft(bot, "001", ["am"])   # (am audio was marked stale by the save; redo)

        # 2) editing a NON-Armenian script only stales its own audio
        await db.save_draft_script_manual("001", "ru", "T-ru", "hand-edited russian")
        rows = await db.get_all_audio_rows("001")
        assert rows["ru"]["audio_stale"] == 1 and rows["ru"]["script_stale"] == 0
        assert all(rows[c]["script_stale"] == 0 and rows[c]["audio_stale"] == 0 for c in LANGUAGES if c != "ru")
        await pipeline.generate_audio_draft(bot, "001", ["ru"])

        # 2b) translating without touching Armenian leaves the Armenian row byte-identical
        before_am = dict((await db.get_all_audio_rows("001"))["am"])
        await pipeline.translate_from_armenian_draft("001")
        after = await db.get_all_audio_rows("001")
        assert dict(after["am"]) == before_am, "Armenian row must be untouched by translation"
        assert after["am"]["audio_stale"] == 0
        await pipeline.generate_audio_draft(bot, "001", OTHERS)
        translate_calls.clear()

        # 3) hand-edit the Armenian script
        edited = "Ձեռքով խմբագրված հայերեն տեքստ։"
        await db.save_draft_script_manual("001", "am", "Նոր վերնագիր", edited)
        rows = await db.get_all_audio_rows("001")
        for c in OTHERS:
            assert rows[c]["script_stale"] == 1 and rows[c]["script_stale_reason"] == "armenian", c
            assert rows[c]["draft_script_text"].startswith("translated from"), "old text kept until retranslated"
        assert rows["am"]["script_stale"] == 0 and rows["am"]["audio_stale"] == 1
        problems = pipeline.publish_readiness(rows)
        assert sum("Armenian changed — retranslate" in p for p in problems) == 9, problems
        assert any(p.startswith("Armenian (am)") and "audio outdated" in p for p in problems)
        assert await pipeline.publish_draft("001") == problems and (await db.get_exhibit("001"))["status"] != "live"
        print("edit Armenian -> 9 outdated (reason 'armenian'), am audio stale, publish blocked OK")

        # 4) translate: only the 9 replaced, from the EDITED Armenian; Armenian intact
        am_before = dict(rows["am"])
        await pipeline.translate_from_armenian_draft("001")
        assert translate_calls == [("Նոր վերնագիր", edited, OTHERS)], translate_calls
        rows = await db.get_all_audio_rows("001")
        assert dict(rows["am"]) == am_before, "Armenian intact"
        for c in OTHERS:
            r = rows[c]
            assert r["draft_script_text"] == f"translated from: {edited} [{c}]" and r["draft_title_translated"] == f"TT-{c}"
            assert r["script_stale"] == 0 and r["script_stale_reason"] is None and r["audio_stale"] == 1, c
        problems = pipeline.publish_readiness(rows)
        assert len(problems) == 10 and not any("retranslate" in p for p in problems), problems   # now just audio
        print("translate: 9 replaced from edited Armenian, am untouched, audio flagged outdated OK")

        # 5) audio regenerated -> publish unblocks; published columns only change on publish
        assert await pipeline.generate_audio_draft(bot, "001", list(LANGUAGES)) == []
        rows = await db.get_all_audio_rows("001")
        assert pipeline.publish_readiness(rows) == []
        assert rows["en"]["script_text"] is None, "published columns untouched until publish"
        assert await pipeline.publish_draft("001") == []
        rows = await db.get_all_audio_rows("001")
        assert rows["en"]["script_text"].startswith("translated from") and (await db.get_exhibit("001"))["status"] == "live"
        print("audio regenerated -> publish allowed, goes live OK")

        # 6) failure modes
        await db.create_exhibit("002", "x", "y", created_by=None); await db.mark_source_edited("002")
        try: await pipeline.translate_from_armenian_draft("002"); raise SystemExit("should refuse: no Armenian script")
        except pipeline.TranslateError as e: assert "no Armenian script" in str(e)
        await pipeline.generate_scripts_draft("002")
        await db.mark_source_edited("002")                       # fact sheet changed -> am script outdated
        try: await pipeline.translate_from_armenian_draft("002"); raise SystemExit("should refuse: stale am")
        except pipeline.TranslateError as e: assert "outdated" in str(e)
        await pipeline.generate_scripts_draft("002")
        snap = {c: r["draft_script_text"] for c, r in (await db.get_all_audio_rows("002")).items()}
        async def partial(eid, t, s, codes): return {c: {"title": "t", "narration": "n"} for c in codes if c != "de"}
        with mock.patch.object(pipeline, "_translate_scripts", partial):
            try: await pipeline.translate_from_armenian_draft("002"); raise SystemExit("should refuse partial")
            except pipeline.TranslateError as e: assert "de" in str(e)
        assert snap == {c: r["draft_script_text"] for c, r in (await db.get_all_audio_rows("002")).items()}, "all-or-nothing"
        # source edit beats everything: reason becomes 'source'
        await db.save_draft_script_manual("002", "am", "t", "edited"); await db.mark_source_edited("002")
        r = await db.get_all_audio_rows("002")
        assert all(r[c]["script_stale_reason"] == "source" for c in LANGUAGES)
        assert not any("retranslate" in p for p in pipeline.publish_readiness(r))
        # (and with audio present, a source-stale language is reported as "generate scripts")
        await db.upsert_audio_draft("002", "en", status="ready")
        assert any(p.startswith("English (en): script outdated — generate scripts")
                   for p in pipeline.publish_readiness(await db.get_all_audio_rows("002")))
        print("refusals (no Armenian / stale Armenian / partial result all-or-nothing) + source reason OK")


async def test_web():
    use(os.path.join(TMP, "web.sqlite3")); await migrate.migrate()
    from fastapi.testclient import TestClient
    from webapp import app as webapp_app
    bot = FakeBot()
    with mock.patch.multiple(pipeline, **MOCKS):
        await clean_exhibit("001", bot)
        await db.create_exhibit("002", "x", "y", created_by=None)
        cl = TestClient(webapp_app.create_app(bot=None), follow_redirects=False)
        run = lambda f, *a, **k: asyncio.to_thread(f, *a, **k)
        await run(cl.post, "/login", data={"username": "t", "password": "pw"})

        gen = mock.AsyncMock()
        with mock.patch.object(pipeline, "generate_scripts_draft", gen):
            r = await run(cl.post, "/exhibits/001/generate-scripts", data={})
            assert r.status_code == 303 and "generate_error" in r.headers["location"] and not gen.called
            assert "Translate" in r.headers["location"] or "overwrite" in r.headers["location"].lower() or "overwrite" in __import__("urllib.parse").parse.unquote(r.headers["location"]).lower()
            r = await run(cl.post, "/exhibits/001/generate-scripts", data={"confirm_overwrite": "1"})
            assert gen.called and "generate_error" not in r.headers["location"]
            gen.reset_mock()
            r = await run(cl.post, "/exhibits/002/generate-scripts", data={})      # nothing exists yet -> no confirm needed
            assert gen.called and "generate_error" not in r.headers["location"]

        page = (await run(cl.get, "/exhibits/001")).text
        assert "/translate-from-armenian" in page and "full redo" in page and "confirm(" in page
        assert "/translate-from-armenian" not in (await run(cl.get, "/exhibits/002")).text   # no Armenian script yet
        await db.save_draft_script_manual("001", "am", "T", "edited am")
        page = (await run(cl.get, "/exhibits/001")).text
        assert page.count('badge-stale">Armenian changed — retranslate') == 9      # 9 card badges
        assert page.count("Armenian changed — retranslate") == 18                  # + 9 publish-blocking list entries
        r = await run(cl.post, "/exhibits/001/translate-from-armenian")
        assert r.status_code == 303 and "generate_error" not in r.headers["location"]
        rows = await db.get_all_audio_rows("001")
        assert all(rows[c]["script_stale"] == 0 for c in OTHERS) and rows["am"]["draft_script_text"] == "edited am"
        assert "Armenian changed" not in (await run(cl.get, "/exhibits/001")).text
        r = await run(cl.post, "/exhibits/002/translate-from-armenian")
        assert "generate_error" in r.headers["location"]
    print("web: overwrite confirmation (server-side guard), Translate button visibility, badges, route OK")


async def main():
    await test_core(); await test_web(); print("\nRETRANSLATE TESTS PASSED")

asyncio.run(main())
