"""Admin panel = Armenian UI tests (throwaway DB): string registry integrity, rendered pages contain no
leftover English, status/staleness/readiness labels, tag chips markup, server-side messages."""
import asyncio, os, re, tempfile, glob
from unittest import mock
from urllib.parse import unquote_plus
import bcrypt

TMP = tempfile.mkdtemp()
os.environ.update(WEBAPP_ADMIN_USER="t", WEBAPP_SESSION_SECRET="s",
                  WEBAPP_ADMIN_PASSWORD_HASH=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
from db import db, migrate
from worker import pipeline, tags as tagmod
from webapp.strings import STRINGS, S, status_label

ARMENIAN = re.compile(r"[Ա-֏]")
# English-looking words that are allowed to remain: file extension + native language names (technical labels)
ALLOWED = {"boom", "docx", "English", "Deutsch", "Italiano", "Español", "Français", "Русский"}


def visible_english(html):
    html = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    return sorted(set(re.findall(r"\b[A-Za-z]{4,}\b", text)) - ALLOWED)


def test_registry():
    # every key used in templates / app code exists (static scan), values are Armenian, placeholders format
    used = set()
    for f in glob.glob("webapp/templates/*.html") + ["webapp/app.py"]:
        used |= set(re.findall(r"""\bS\(\s*["']([a-z0-9_]+)["']""", open(f).read()))
    used = {k for k in used if not k.endswith("_")}          # dynamic prefixes (issue_<kind>) are checked below
    missing = used - set(STRINGS)
    assert not missing, f"keys used but undefined: {missing}"
    for kind in ("audio_missing", "armenian_changed", "script_outdated", "audio_outdated"):
        assert f"issue_{kind}" in STRINGS
    for st in ("draft", "processing", "review", "live", "unpublished", "failed"):
        assert f"status_{st}" in STRINGS
    for key, val in STRINGS.items():
        assert ARMENIAN.search(val) or key == "col_id", f"{key} has no Armenian text: {val!r}"   # "ID" stays as-is
        fields = set(re.findall(r"\{(\w+)\}", val))
        S(key, **{f: "x" for f in fields})                      # formats without KeyError
    try: S("no_such_key"); raise SystemExit("S() must fail loudly on a typo")
    except KeyError: pass
    assert status_label("draft") == "Սևագիր" and status_label("review") == "Ստուգման" and status_label("live") == "Հրապարակված"
    assert S("stale_source").startswith("Հնացած — վերագեներացրեք") and S("stale_armenian") == "Հայերենը փոխվել է — վերաթարգմանեք"
    assert S("save_tags") == "Պահպանել հատկանիշները" and S("tags_note") == "Հատկանիշները չեն ազդում տեքստերի ու ձայների վրա"
    assert S("add_tag") == "➕ Նոր հատկանիշ" and S("tag_untranslated_tip") == "Թարգմանությունը ընթացքի մեջ է"
    print(f"registry: {len(STRINGS)} keys, {len(used)} used by code/templates, all Armenian, placeholders valid, typo fails loudly OK")


async def main():
    test_registry()
    db.DB_PATH = migrate.DB_PATH = os.path.join(TMP, "panel.sqlite3"); await migrate.migrate()
    from fastapi.testclient import TestClient
    from webapp import app as W
    async def fake_upload(bot, files): return ["fid"] * len(files)

    # seed: exhibits in every status, tags (one untranslated), stale/missing states
    for eid, st in (("001", "draft"), ("002", "review"), ("003", "live"), ("004", "unpublished"), ("005", "failed"), ("006", "processing")):
        await db.create_exhibit(eid, f"Վերնագիր {eid}", "փաստեր", None); await db.set_exhibit_status(eid, st)
    t1, _ = await db.get_or_create_tag("Հանքանյութեր"); t2, _ = await db.get_or_create_tag("Գործիքներ")
    await db.save_tag_translations(t1, {c: "x" for c in db.TRANSLATION_LANGS})            # t1 translated, t2 not
    await db.set_exhibit_tags("001", [t1])

    with mock.patch.object(W, "_upload_photos", fake_upload):
        cl = TestClient(W.create_app(bot=None), follow_redirects=False)
        run = lambda f, *a, **k: asyncio.to_thread(f, *a, **k)

        # login page (logged out) + wrong password message
        page = (await run(cl.get, "/login")).text
        assert '<html lang="hy">' in page and S("login_button") in page and visible_english(page) == []
        r = await run(cl.post, "/login", data={"username": "t", "password": "nope"})
        assert r.status_code == 401 and S("login_wrong") in r.text
        await run(cl.post, "/login", data={"username": "t", "password": "pw"})

        # dashboard: Armenian chrome + a badge label per status
        page = (await run(cl.get, "/")).text
        assert visible_english(page) == [], visible_english(page)
        for st in ("draft", "processing", "review", "live", "unpublished", "failed"):
            assert f'badge-{st}">{S("status_" + st)}<' in page, st
        for k in ("exhibits", "new_exhibit_btn", "col_status", "entrance_qr_title", "download_png", "nav_tags", "logout"):
            assert S(k) in page, k

        # tags page + new form: Armenian, nothing English
        for url in ("/tags", "/exhibits/new"):
            h = (await run(cl.get, url)).text
            assert visible_english(h) == [], (url, visible_english(h))

        # ── exhibit page with all staleness states ─────────────────────────
        await db.ensure_audio_rows("002")
        for code in pipeline.LANGUAGES:
            await db.upsert_audio_draft("002", code, status="ready", script_text="x")
        async with db.get_conn() as c:
            await c.execute("UPDATE audio_cache SET draft_script_text='t', draft_title_translated='T', script_stale=0, audio_stale=0 WHERE exhibit_id='002'")
            await c.execute("UPDATE audio_cache SET script_stale=1, script_stale_reason='armenian' WHERE exhibit_id='002' AND language_code='ru'")
            await c.execute("UPDATE audio_cache SET script_stale=1, script_stale_reason='source' WHERE exhibit_id='002' AND language_code='fr'")
            await c.execute("UPDATE audio_cache SET audio_stale=1 WHERE exhibit_id='002' AND language_code='de'")
            await c.execute("UPDATE audio_cache SET draft_status='failed', draft_error_message='boom' WHERE exhibit_id='002' AND language_code='es'")
            await c.execute("UPDATE audio_cache SET draft_script_text=NULL WHERE exhibit_id='002' AND language_code='it'")
            await c.commit()
        page = (await run(cl.get, "/exhibits/002")).text
        assert visible_english(page) == [], visible_english(page)
        for k in ("stale_armenian", "stale_source", "stale_audio", "no_script", "stage1_title", "stage23_title", "stage4_title",
                  "gen_scripts_redo", "translate_from_am", "gen_audio", "not_ready", "save_lang_script", "publish_hint"):
            assert S(k) in page, k
        assert S("audio_failed", error="boom") in page and S("exhibit_heading", id="002") in page
        assert f'badge-review">{S("status_review")}<' in page
        # publish-blocking reasons, one Armenian line per kind
        ru, fr, de, es = ("🇷🇺 Русский (ru)", "🇫🇷 Français (fr)", "🇩🇪 Deutsch (de)", "🇪🇸 Español (es)")
        assert S("issue_armenian_changed", lang=ru) in page and S("issue_script_outdated", lang=fr) in page
        assert S("issue_audio_outdated", lang=de) in page and S("issue_audio_missing", lang=es) in page
        # …and English publish_readiness() text is unchanged for non-panel callers
        eng = pipeline.publish_readiness(await db.get_all_audio_rows("002"))
        assert "Russian (ru): Armenian changed — retranslate" in eng and "Spanish (es): audio missing" in eng
        # confirm dialogs are Armenian and JS-safe in the attribute
        import json
        assert json.dumps(S("confirm_overwrite")) in page and json.dumps(S("confirm_translate")) in page
        print("pages: login/dashboard/tags/new/exhibit have zero English; statuses, stale reasons, publish blockers, confirms OK")

        # ── tag chips markup ───────────────────────────────────────────────
        page = (await run(cl.get, "/exhibits/001")).text
        field = page[page.index('data-tags-field'):page.index("</fieldset>")]
        assert field.count('<label class="chip"') == 2 and 'name="tags"' in field
        assert re.search(rf'<input type="checkbox" name="tags" value="{t1}" checked>', field), "attached tag is selected"
        assert not re.search(rf'value="{t2}" checked', field), "unattached tag is unselected"
        assert field.count("chip-dot") == 1 and f'title="{S("tag_untranslated_tip")}"' in field, "only the untranslated tag has the indicator"
        assert S("add_tag") in field and "data-add-input" in field and "data-add-ok" in field
        # single save action + muted caption + no bare checkbox outside a chip
        assert page.count(S("save_tags")) == 1 and f'class="hint caption">{S("tags_note")}<' in page
        for m in re.finditer(r'<input type="checkbox"', page):
            before = page[max(0, m.start() - 80):m.start()]
            assert "<label class=\"chip" in before, "checkbox outside a chip: " + before
        dash = (await run(cl.get, "/")).text
        for m in re.finditer(r'<input type="checkbox"', dash):
            assert "<label class=\"chip" in dash[max(0, m.start() - 80):m.start()]
        # failed form re-renders typed new tags as selected pending chips (and creates nothing)
        before = len(await db.list_tags())
        r = await run(cl.post, "/exhibits/new", data={"exhibit_id": "001", "title_am": "x", "fact_sheet_text": "y",
                      "tags": [str(t2)], "new_tags": ["Նոր մեկը, Մեկ ուրիշ"]}, files=[("photos", ("p.jpg", b"x", "image/jpeg"))])
        assert r.status_code == 400 and S("err_id_taken", id="001") in r.text
        assert r.text.count('<label class="chip pending"') == 2 and 'value="Նոր մեկը" checked' in r.text and 'value="Մեկ ուրիշ" checked' in r.text
        assert re.search(rf'value="{t2}" checked', r.text) and len(await db.list_tags()) == before
        # saving pending chips creates/reuses tags case-insensitively (translation mocked)
        async def fake_tr(name, codes): return {c: f"{name}~{c}" for c in codes}
        with mock.patch.object(tagmod, "_translate", fake_tr):
            r = await run(cl.post, "/exhibits/001/tags", data={"tags": [str(t1)], "new_tags": ["ԳՈՐԾԻՔՆԵՐ", "Թարմ"]})
        assert r.status_code == 303
        names = {t["name_hy"] for t in await db.list_tags()}
        assert names == {"Հանքանյութեր", "Գործիքներ", "Թարմ"}, "ԳՈՐԾԻՔՆԵՐ reused the existing tag"
        assert len(await db.get_exhibit_tag_ids("001")) == 3
        print("tag chips: toggle markup, indicator only on untranslated, single save + caption, no bare checkboxes, pending chips (error re-render, save, casefold reuse) OK")

        # ── server-side messages ───────────────────────────────────────────
        assert S("err_exhibit_not_found") in (await run(cl.get, "/exhibits/nope")).text
        r = await run(cl.post, "/exhibits/new", data={"title_am": "x", "fact_sheet_text": ""}, files=[("photos", ("p.jpg", b"x", "image/jpeg"))])
        assert S("err_fact_required") in r.text
        r = await run(cl.post, "/exhibits/new", data={"title_am": "x", "fact_sheet_text": "y"})
        assert S("err_photo_required") in r.text
        r = await run(cl.post, "/exhibits/new", data={"exhibit_id": "bad id!", "title_am": "x", "fact_sheet_text": "y"}, files=[("photos", ("p.jpg", b"x", "image/jpeg"))])
        assert S("err_id_chars", max=32) in r.text
        # translate-from-Armenian errors localized (no Armenian script / stale / incomplete)
        r = await run(cl.post, "/exhibits/001/translate-from-armenian")
        assert S("tr_no_armenian") in unquote_plus(r.headers["location"])
        page = (await run(cl.get, "/exhibits/001?generate_error=" + S("tr_no_armenian"))).text
        assert S("step_failed", error=S("tr_no_armenian")) in page
        async with db.get_conn() as c:
            await c.execute("UPDATE audio_cache SET draft_script_text='t', draft_title_translated='T', script_stale=1 WHERE exhibit_id='002' AND language_code='am'"); await c.commit()
        r = await run(cl.post, "/exhibits/002/translate-from-armenian")
        assert S("tr_armenian_stale") in unquote_plus(r.headers["location"])
        async with db.get_conn() as c:
            await c.execute("UPDATE audio_cache SET script_stale=0 WHERE exhibit_id='002'"); await c.commit()
        async def partial(eid, t, s, codes): return {c: {"title": "t", "narration": "n"} for c in codes if c != "de"}
        with mock.patch.object(pipeline, "_translate_scripts", partial):
            r = await run(cl.post, "/exhibits/002/translate-from-armenian")
        assert S("tr_incomplete", missing="de") in unquote_plus(r.headers["location"])
        # tag page messages
        r = await run(cl.post, "/tags/new", data={"name_hy": "  "}); assert S("err_tag_name") in unquote_plus(r.headers["location"])
        r = await run(cl.post, "/tags/99999/rename", data={"name_hy": "x"}); assert S("err_tag_not_found") in unquote_plus(r.headers["location"])
        r = await run(cl.post, f"/tags/{t1}/merge", data={"into": str(t1)}); assert S("err_merge_pick") in unquote_plus(r.headers["location"])
        print("server messages (login, not found, form errors, translate errors, tag actions) localized OK")
    print("\nPANEL (ARMENIAN UI) TESTS PASSED")

asyncio.run(main())
