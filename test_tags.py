"""Tags system tests (throwaway DBs only): migration categories->tags on a copy of the real DB
and on a fresh DB, CRUD / merge / rename-retranslation / delete-with-links, soft-limit advisory,
translation failure + fallback, admin pages, visitor browse (All-default, tag filter, empty-tag
hiding, back buttons), reply-keyboard handlers, setMyCommands."""
import asyncio, os, shutil, sqlite3, tempfile
from types import SimpleNamespace
from unittest import mock
import bcrypt

TMP = tempfile.mkdtemp()
os.environ.update(WEBAPP_ADMIN_USER="t", WEBAPP_SESSION_SECRET="s",
                  WEBAPP_ADMIN_PASSWORD_HASH=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
os.environ.pop("TAGS_SOFT_LIMIT", None)
from db import db, migrate
from bot import visitor
from worker import tags as tagmod
from webapp.strings import S
from urllib.parse import unquote_plus
import json

OTHERS = db.TRANSLATION_LANGS


def use(path): db.DB_PATH = path; migrate.DB_PATH = path
def sql(path, q, *a):
    c = sqlite3.connect(path); r = c.execute(q, a).fetchall(); c.commit(); c.close(); return r
def cols(path, table): return [r[1] for r in sqlite3.connect(path).execute(f"PRAGMA table_info({table})")]
def tables(path): return {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ── translation mock ────────────────────────────────────────────────────────
class FakeTranslator:
    def __init__(self): self.calls, self.fail, self.partial = [], False, False
    async def __call__(self, name_hy, codes):
        self.calls.append(name_hy)
        if self.fail: raise RuntimeError("API down")
        out = {c: f"{name_hy}~{c}" for c in codes}
        if self.partial: out["de"] = ""
        return out
FT = FakeTranslator()


# ── visitor fakes ───────────────────────────────────────────────────────────
class Msg:
    def __init__(self, text="", uid=1):
        self.text, self.from_user, self.chat = text, SimpleNamespace(id=uid), SimpleNamespace(type="private")
        self.out = []
    async def answer(self, text, reply_markup=None, **kw): self.out.append(("text", text, reply_markup))
    async def answer_photo(self, photo, caption=None, **kw): self.out.append(("photo", caption, None))
    async def answer_voice(self, voice, reply_markup=None, **kw): self.out.append(("voice", voice, reply_markup))
    async def edit_text(self, text, reply_markup=None): self.out.append(("edit", text, reply_markup))
def cb(data, uid=1):
    m = Msg(uid=uid)
    async def _a(*a, **k): pass
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=m, answer=_a), m
def btns(markup): return [b for row in markup.inline_keyboard for b in row]


async def add_exhibit(eid, status="live", title_en=None, script="Script."):
    await db.create_exhibit(eid, f"AM-{eid}", "facts", created_by=None)
    await db.set_exhibit_status(eid, status)
    await db.replace_exhibit_photos(eid, [f"photo-{eid}"])
    async with db.get_conn() as c:
        await c.execute("INSERT INTO audio_cache (exhibit_id, language_code, script_text, title_translated, telegram_file_id, status)"
                        " VALUES (?,?,?,?,?,'ready')", (eid, "en", script, title_en or f"EN-{eid}", f"voice-{eid}"))
        await c.commit()


# ════════════════════════════════════════════════════════════════════════════
async def test_migration():
    # (a) copy of the real (dev) DB with categories data -> tags
    src = "museum_bot.sqlite3"
    p = os.path.join(TMP, "realcopy.sqlite3"); shutil.copy(src, p); use(p)
    await migrate.migrate()                                   # bring the copy to current shape first
    c = sqlite3.connect(p)                                    # then recreate the OLD category design on top
    c.executescript("""PRAGMA foreign_keys=OFF;
        DROP TABLE IF EXISTS tag_translations; DROP TABLE IF EXISTS exhibit_tags; DROP TABLE IF EXISTS tags;
        CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name_hy TEXT NOT NULL UNIQUE, sort_order INTEGER NOT NULL DEFAULT 0);
        ALTER TABLE exhibits ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
        INSERT INTO categories (id, name_hy, sort_order) VALUES (7, 'Հանքանյութեր', 1), (9, 'Գործիքներ', 2);""")
    ids = [r[0] for r in c.execute("SELECT id FROM exhibits ORDER BY id")]
    assert len(ids) >= 2, "dev DB needs >=2 exhibits for this test"
    c.execute("UPDATE exhibits SET category_id = 7 WHERE id = ?", (ids[0],))
    c.execute("UPDATE exhibits SET category_id = 9 WHERE id = ?", (ids[1],))
    before = c.execute("SELECT id,title_am,fact_sheet_am,status,deep_link,created_at,updated_at FROM exhibits ORDER BY id").fetchall()
    n_audio = c.execute("SELECT COUNT(*) FROM audio_cache").fetchone()[0]
    n_photos = c.execute("SELECT COUNT(*) FROM exhibit_photos").fetchone()[0]
    c.commit(); c.close()

    await migrate.migrate(); await migrate.migrate()           # twice = idempotent
    c = sqlite3.connect(p)
    assert "categories" not in tables(p) and "category_id" not in cols(p, "exhibits")
    assert {"tags", "exhibit_tags", "tag_translations"} <= tables(p)
    assert c.execute("SELECT id, name_hy, sort_order FROM tags ORDER BY id").fetchall() == [(7, "Հանքանյութեր", 1), (9, "Գործիքներ", 2)]
    assert sorted(c.execute("SELECT exhibit_id, tag_id FROM exhibit_tags").fetchall()) == sorted([(ids[0], 7), (ids[1], 9)])
    assert c.execute("SELECT id,title_am,fact_sheet_am,status,deep_link,created_at,updated_at FROM exhibits ORDER BY id").fetchall() == before
    assert c.execute("SELECT COUNT(*) FROM audio_cache").fetchone()[0] == n_audio
    assert c.execute("SELECT COUNT(*) FROM exhibit_photos").fetchone()[0] == n_photos
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
    # the migrated copy is fully usable: FKs enforced, new tag works
    tid, created = await db.get_or_create_tag("Նոր"); assert created
    print(f"migration (copy of real DB, {len(ids)} exhibits): categories+links -> tags, old structures dropped, data intact, idempotent OK")

    # (b) DB that never had categories (pre-round-2 shape)
    p2 = os.path.join(TMP, "pre_categories.sqlite3"); shutil.copy(src, p2); use(p2)
    await migrate.migrate()
    c = sqlite3.connect(p2)
    c.executescript("PRAGMA foreign_keys=OFF; DROP TABLE IF EXISTS tag_translations; DROP TABLE IF EXISTS exhibit_tags; DROP TABLE IF EXISTS tags; DROP TABLE IF EXISTS categories;")
    if "category_id" in cols(p2, "exhibits"):
        c.executescript("""CREATE TABLE ex_old AS SELECT id,title_am,fact_sheet_am,qr_code_path,deep_link,status,created_by,created_at,updated_at FROM exhibits;
                           DROP TABLE exhibits; ALTER TABLE ex_old RENAME TO exhibits;""")
    c.commit(); c.close()
    await migrate.migrate()
    assert {"tags", "exhibit_tags", "tag_translations"} <= tables(p2) and "categories" not in tables(p2)
    assert sqlite3.connect(p2).execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # (c) fresh DB
    p3 = os.path.join(TMP, "fresh.sqlite3"); use(p3); await migrate.migrate()
    assert {"tags", "exhibit_tags", "tag_translations"} <= tables(p3) and "categories" not in tables(p3)
    assert "category_id" not in cols(p3, "exhibits")
    print("migration (pre-categories DB, fresh DB) OK")


async def test_crud():
    use(os.path.join(TMP, "crud.sqlite3")); await migrate.migrate()
    for e in ("001", "002", "003"): await add_exhibit(e)
    a, created = await db.get_or_create_tag("Հանքանյութեր"); assert created
    again, created = await db.get_or_create_tag("  ՀԱՆՔԱՆՅՈՒԹԵՐ "); assert (again, created) == (a, False), "Armenian case-insensitive dup (Python casefold)"
    b, _ = await db.get_or_create_tag("Գործիքներ")
    c_, _ = await db.get_or_create_tag("Բրածոներ")
    await db.set_exhibit_tags("001", [a, b]); await db.set_exhibit_tags("002", [a]); await db.set_exhibit_tags("003", [b, c_])
    rows = {r["name_hy"]: r for r in await db.list_tags()}
    assert (rows["Հանքանյութեր"]["exhibit_count"], rows["Գործիքներ"]["exhibit_count"], rows["Բրածոներ"]["exhibit_count"]) == (2, 2, 1)
    # filter: match ANY
    got = {r["id"] for r in await db.list_exhibits([a, c_])}; assert got == {"001", "002", "003"}
    got = {r["id"] for r in await db.list_exhibits([c_])}; assert got == {"003"}
    assert {r["id"]: r["tag_names"] for r in await db.list_exhibits()}["001"].count(",") == 1
    # set_exhibit_tags ignores bogus ids and doesn't touch updated_at
    await db.set_exhibit_tags("002", [a, 9999]); assert await db.get_exhibit_tag_ids("002") == [a]

    # rename: dup refused (case-insens.), old translations dropped
    await db.save_tag_translations(a, {c: f"old-{c}" for c in OTHERS})
    try: await db.rename_tag(a, "գործիքներ"); raise SystemExit("rename to existing name must fail")
    except ValueError: pass
    await db.rename_tag(a, "Օգտակար հանածոներ")
    assert (await db.get_tag(a))["name_hy"] == "Օգտակար հանածոներ"
    assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM tag_translations WHERE tag_id=?", (a,)).fetchone()[0] == 0
    await db.rename_tag(a, "ՕԳՏԱԿԱՐ ՀԱՆԱԾՈՆԵՐ")                   # renaming to a case-variant of ITSELF is allowed
    # merge b into a: links reassigned, no duplicate (001 had both), src removed
    n = await db.merge_tags(b, a)
    assert n == 2 and await db.get_tag(b) is None
    assert sorted(await db.get_exhibit_tag_ids("001")) == [a] and sorted(await db.get_exhibit_tag_ids("003")) == sorted([a, c_])
    assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM exhibit_tags WHERE tag_id=?", (b,)).fetchone()[0] == 0
    try: await db.merge_tags(a, a); raise SystemExit("self-merge must fail")
    except ValueError: pass
    # delete with links in use: only links go, exhibits stay
    n = await db.delete_tag(a)
    assert n == 3 and await db.get_tag(a) is None
    assert {r["id"] for r in await db.list_exhibits()} == {"001", "002", "003"}
    assert await db.get_exhibit_tag_ids("001") == [] and await db.get_exhibit_tag_ids("003") == [c_]
    print("tags CRUD: casefold dup, ANY-filter, rename (+dup guard, translations dropped), merge (no dup links), delete-with-links keeps exhibits OK")


async def test_translation_and_limit():
    use(os.path.join(TMP, "tr.sqlite3")); await migrate.migrate()
    with mock.patch.object(tagmod, "_translate", FT):
        t, _ = await db.get_or_create_tag("Հանքանյութեր")
        assert await tagmod.translate_tag(t) is True and FT.calls[-1] == "Հանքանյութեր"
        cnt = sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM tag_translations WHERE tag_id=?", (t,)).fetchone()[0]
        assert cnt == 9
        # rename -> old translations gone, retranslated from the NEW name
        await db.rename_tag(t, "Ապարներ"); assert await tagmod.translate_tag(t)
        assert sqlite3.connect(db.DB_PATH).execute("SELECT name FROM tag_translations WHERE tag_id=? AND lang='en'", (t,)).fetchone()[0] == "Ապարներ~en"
        # failure never raises; tag stays, untranslated; fallback to Armenian for visitors
        FT.fail = True
        u, _ = await db.get_or_create_tag("Գործիքներ")
        assert await tagmod.translate_tag(u) is False
        assert u in await db.list_untranslated_tag_ids() and (await db.get_tag(u))["name_hy"] == "Գործիքներ"
        await db.rename_tag(t, "Ապարներ 2"); assert await tagmod.translate_tag(t) is False      # rename + failure: old translations NOT kept
        assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM tag_translations WHERE tag_id=?", (t,)).fetchone()[0] == 0
        assert await db.get_tag_label(t, "en") == "Ապարներ 2" and await db.get_tag_label(t, "am") == "Ապարներ 2"
        # incomplete answer = failure, nothing half-stored
        FT.fail, FT.partial = False, True
        assert await tagmod.translate_tag(u) is False
        assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM tag_translations WHERE tag_id=?", (u,)).fetchone()[0] == 0
        # retry succeeds -> translate_missing handles all untranslated
        FT.partial = False
        res = await tagmod.translate_missing()
        assert res == {t: True, u: True} and await db.list_untranslated_tag_ids() == []
        assert await db.get_tag_label(u, "en") == "Գործիքներ~en" and await db.get_tag_label(u, "am") == "Գործիքներ"
        print("translation: stored 9, rename retranslates (old dropped), failure never raises, partial rejected, retry/translate-missing, Armenian fallback OK")

        # soft limit (default 12; env override)
        assert tagmod.tags_soft_limit() == 12
        os.environ["TAGS_SOFT_LIMIT"] = "3"
        try:
            assert await tagmod.over_soft_limit() is False           # 2 tags
            await db.get_or_create_tag("Երրորդ"); assert await tagmod.over_soft_limit() is False   # 3 == limit: still fine
            await db.get_or_create_tag("Չորրորդ"); assert await tagmod.over_soft_limit() is True    # 4 > 3
        finally: os.environ.pop("TAGS_SOFT_LIMIT")
        assert tagmod.tags_soft_limit() == 12
        assert tagmod.TAGS_ADVISORY.startswith("Խորհուրդ ենք տալիս հատկանիշները պահել քիչ ու ընդհանրական (մինչև ~12)")
        print("soft limit: default 12, env override, over-limit detection, advisory text OK")


async def test_web():
    use(os.path.join(TMP, "web.sqlite3")); await migrate.migrate()
    from fastapi.testclient import TestClient
    from webapp import app as webapp_app
    async def fake_upload(bot, files): return ["fid"] * len(files)
    FT.fail = FT.partial = False; FT.calls.clear()
    with mock.patch.object(webapp_app, "_upload_photos", fake_upload), mock.patch.object(tagmod, "_translate", FT):
        cl = TestClient(webapp_app.create_app(bot=None), follow_redirects=False)
        run = lambda f, *a, **k: asyncio.to_thread(f, *a, **k)
        assert (await run(cl.post, "/login", data={"username": "t", "password": "pw"})).status_code == 303
        files = lambda: [("photos", ("p.jpg", b"x", "image/jpeg"))]
        form = lambda **kw: {"title_am": "Վերնագիր", "fact_sheet_text": "փաստեր", **kw}

        # create exhibit with brand-new tags (comma separated; dup within the input collapsed)
        r = await run(cl.post, "/exhibits/new", data=form(new_tags="Հանքանյութեր, ՀԱՆՔԱՆՅՈՒԹԵՐ ,Գործիքներ"), files=files())
        assert r.status_code == 303 and "notice" not in r.headers["location"]
        tags = {t["name_hy"]: t["id"] for t in await db.list_tags()}
        assert set(tags) == {"Հանքանյութեր", "Գործիքներ"} and sorted(await db.get_exhibit_tag_ids("001")) == sorted(tags.values())
        assert sorted(FT.calls) == ["Գործիքներ", "Հանքանյութեր"], "new tags translated once each"
        # second exhibit: pick existing + reuse by name (case-insens.) -> no new tag, no new translation call
        FT.calls.clear()
        await run(cl.post, "/exhibits/new", data=form(tags=[str(tags["Հանքանյութեր"])], new_tags="գործիքներ"), files=files())
        assert len(await db.list_tags()) == 2 and FT.calls == [] and len(await db.get_exhibit_tag_ids("002")) == 2
        # rejected form (duplicate ID) leaves no orphan tag
        r = await run(cl.post, "/exhibits/new", data=form(exhibit_id="001", new_tags="Orphan"), files=files())
        assert r.status_code == 400 and len(await db.list_tags()) == 2
        # translation API down: exhibit + tag still created, tag is untranslated
        FT.fail = True
        r = await run(cl.post, "/exhibits/new", data=form(new_tags="Բրածոներ"), files=files())
        assert r.status_code == 303 and (await db.get_exhibit("003")) is not None
        fos = (await db.find_tag_by_name("Բրածոներ")); assert fos in await db.list_untranslated_tag_ids()
        FT.fail = False

        # edit tags on an exhibit: replaces set, does not stale/touch exhibit
        async with db.get_conn() as c:
            await c.execute("UPDATE exhibits SET updated_at='2000-01-01 00:00:00' WHERE id='003'"); await c.commit()
        r = await run(cl.post, "/exhibits/003/tags", data={"tags": [str(tags["Գործիքներ"])], "new_tags": ""})
        assert r.status_code == 303 and await db.get_exhibit_tag_ids("003") == [tags["Գործիքներ"]]
        assert (await db.get_exhibit("003"))["updated_at"].startswith("2000")
        assert S("save_tags") in (await run(cl.get, "/exhibits/003")).text

        # dashboard: tags column + any-match filter
        html = (await run(cl.get, "/")).text
        assert f"<th>{S('col_tags')}</th>" in html and "Հանքանյութեր" in html
        only = (await run(cl.get, "/", params=[("tag", fos)])).text
        assert ">001<" not in only and ">003<" not in only                      # fossils tag has no exhibits now
        any_ = (await run(cl.get, "/", params=[("tag", tags["Հանքանյութեր"]), ("tag", tags["Գործիքներ"])])).text
        assert all(f">{e}<" in any_ for e in ("001", "002", "003"))
        mi = (await run(cl.get, "/", params=[("tag", tags["Հանքանյութեր"])])).text
        assert ">001<" in mi and ">002<" in mi and ">003<" not in mi

        # tag management page: counts, translation status, confirm text with usage count
        page = (await run(cl.get, "/tags")).text
        assert S("untranslated", n=0, total=9) in page and S("translated") in page and S("translate_missing") in page
        # confirm dialogs state the usage count (JSON-escaped inside the onsubmit attribute)
        t_min = (await db.find_tag_by_name("Հանքանյութեր"))
        assert json.dumps(S("confirm_delete", name="Հանքանյութեր", n=2)) in page
        assert json.dumps(S("confirm_merge", name="Հանքանյութեր", n=2)) in page
        # rename (dup refused), merge, delete, translate-missing, per-tag retry
        r = await run(cl.post, f"/tags/{tags['Գործիքներ']}/rename", data={"name_hy": "ՀԱՆՔԱՆՅՈՒԹԵՐ"})
        assert S("err_tag_dup", name="ՀԱՆՔԱՆՅՈՒԹԵՐ") in unquote_plus(r.headers["location"])
        r = await run(cl.post, f"/tags/{tags['Գործիքներ']}/rename", data={"name_hy": "Գործիքներ և սարքեր"})
        assert (await db.get_tag(tags["Գործիքներ"]))["name_hy"] == "Գործիքներ և սարքեր" and FT.calls[-1] == "Գործիքներ և սարքեր"
        r = await run(cl.post, "/tags/translate-missing")
        assert await db.list_untranslated_tag_ids() == []
        r = await run(cl.post, f"/tags/{fos}/merge", data={"into": str(tags["Հանքանյութեր"])})
        assert await db.get_tag(fos) is None
        r = await run(cl.post, f"/tags/{tags['Հանքանյութեր']}/delete")
        assert S("msg_tag_deleted", n=2) in unquote_plus(r.headers["location"])
        assert await db.get_tag(tags["Հանքանյութեր"]) is None and {e["id"] for e in await db.list_exhibits()} == {"001", "002", "003"}

        # soft-limit advisory shown after creating beyond the limit
        os.environ["TAGS_SOFT_LIMIT"] = "2"
        try:
            r = await run(cl.post, "/tags/new", data={"name_hy": "Նոր մեկը"})        # 1 left + this = 2 -> within
            assert "notice" not in r.headers["location"]
            r = await run(cl.post, "/tags/new", data={"name_hy": "Երրորդը"})         # 3 > 2
            assert "notice=tags_limit" in r.headers["location"]
            assert "Խորհուրդ ենք տալիս" in (await run(cl.get, "/tags?notice=tags_limit")).text
            r = await run(cl.post, "/exhibits/001/tags", data={"new_tags": "Չորրորդը"})
            assert r.headers["location"].endswith("?notice=tags_limit")
            assert "Խորհուրդ ենք տալիս" in (await run(cl.get, "/exhibits/001?notice=tags_limit")).text
            r = await run(cl.post, "/tags/new", data={"name_hy": "երրորդը"})        # reuse of existing -> not a creation -> no advisory
            assert "notice" not in r.headers["location"]
        finally: os.environ.pop("TAGS_SOFT_LIMIT")
    print("web: create/edit with tags (+orphan guard, translation outage), column + ANY filter, tags page (counts/status/confirm),"
          " rename/merge/delete/translate-missing, soft-limit advisory OK")


async def test_visitor_browse():
    use(os.path.join(TMP, "browse.sqlite3")); await migrate.migrate()
    visitor._user_lang.clear()
    for i in range(1, 11): await add_exhibit(f"{i:03d}")
    await add_exhibit("011", status="draft")
    t1, _ = await db.get_or_create_tag("Հանքանյութեր")     # 9 live  -> 2 pages
    t2, _ = await db.get_or_create_tag("Գործիքներ")        # 1 live + 1 draft
    t3, _ = await db.get_or_create_tag("Թաքնված")          # only a draft exhibit -> hidden
    t4, _ = await db.get_or_create_tag("Դատարկ")           # no exhibits -> hidden
    for i in range(1, 10): await db.set_exhibit_tags(f"{i:03d}", [t1])
    await db.set_exhibit_tags("010", [t2]); await db.set_exhibit_tags("011", [t2, t3])
    await db.save_tag_translations(t1, {c: f"Minerals-{c}" for c in OTHERS})      # t2 deliberately untranslated

    # welcome -> reply keyboard
    c, m = cb("welcome:en", uid=5); await visitor.welcome_after_language(c)
    assert [b.text for b in m.out[-1][2].keyboard[0]] == ["🗂 Browse", "🌐 Language"]
    # reply-keyboard Browse -> lands on ALL list directly
    msg = Msg("🗂 Browse", uid=5); await visitor.menu_browse(msg)
    header, kb = msg.out[0][1], msg.out[0][2]
    assert header == "🗂 Exhibit list"
    b = btns(kb)
    chips = [x for x in b if x.callback_data.startswith("br:") and x.callback_data.endswith(":0") and not x.text.startswith("◀")][:3]
    texts = [x.text for x in b]
    assert "✅ All (10)" in texts and "Minerals-en (9)" in texts and "Գործիքներ (1)" in texts, texts      # counts, visitor lang, Armenian fallback
    assert not any("Թաքնված" in t or "Դատարկ" in t for t in texts), "tags without a live exhibit are hidden"
    ex = [x for x in b if x.callback_data.startswith("ex:")]; assert len(ex) == 8 and ex[0].text == "001 — EN-001"
    assert texts[-1] == "🔙 Back" and b[-1].callback_data == "bk:en"
    # tag filter -> in-place edit, only that tag's exhibits, marked, with pagination keeping the tag
    c, m = cb(f"br:en:{t1}:0", uid=5); await visitor.browse_navigate(c)
    kind, header, kb = m.out[0]; b = btns(kb); texts = [x.text for x in b]
    assert kind == "edit" and header == "🗂 Exhibit list · Minerals-en"
    assert "Minerals-en (9)".join(["✅ ", ""]) in texts and "All (10)" in texts
    assert len([x for x in b if x.callback_data.startswith("ex:")]) == 8
    nxt = next(x for x in b if x.text == "▶"); assert nxt.callback_data == f"br:en:{t1}:1"
    assert b[-1].text == "🔙 Back" and b[-1].callback_data == "br:en:0:0", "Back from a tag view returns to All"
    c, m = cb(nxt.callback_data, uid=5); await visitor.browse_navigate(c)
    assert [x.text for x in btns(m.out[0][2]) if x.callback_data.startswith("ex:")] == ["009 — EN-009"]
    # untranslated tag + Armenian visitor
    c, m = cb(f"br:en:{t2}:0", uid=5); await visitor.browse_navigate(c)
    assert m.out[0][1] == "🗂 Exhibit list · Գործիքներ" and [x.text for x in btns(m.out[0][2]) if x.callback_data.startswith("ex:")] == ["010 — EN-010"]
    # tap exhibit in the filtered list -> full delivery
    c, m = cb("ex:010:en", uid=5); await visitor.deliver_from_list(c)
    assert [o[0] for o in m.out] == ["photo", "voice"]
    c, m = cb("txt:010:en", uid=5); await visitor.send_script_text(c); assert m.out[0][1] == "Script."
    # "All" returns to full list; Back from All -> welcome text
    c, m = cb("br:en:0:0", uid=5); await visitor.browse_navigate(c)
    assert m.out[0][1] == "🗂 Exhibit list" and "✅ All (10)" in [x.text for x in btns(m.out[0][2])]
    c, m = cb("bk:en", uid=5); await visitor.back_to_welcome(c)
    assert m.out[0][0] == "edit" and m.out[0][1] == visitor.WELCOME_MESSAGES["en"] and btns(m.out[0][2])[0].callback_data == "list:en"
    # Armenian visitor sees Armenian tag names (no translation lookup for 'am')
    msg = Msg("🗂 Ցանկ / Browse", uid=6); await visitor.menu_browse(msg)
    assert any(x.text == "Հանքանյութեր (9)" for x in btns(msg.out[0][2])) and msg.out[0][1] == visitor.LIST_LABELS["am"]
    assert visitor._user_lang[6] == "am", "language inferred from the reply-keyboard label"
    # empty-tag hiding is live: unpublish the only exhibit of t2 -> chip disappears, stale tap falls back to All
    await db.set_exhibit_status("010", "unpublished")
    c, m = cb(f"br:en:{t2}:0", uid=5); await visitor.browse_navigate(c)
    assert m.out[0][1] == "🗂 Exhibit list" and not any("Գործիքներ" in x.text for x in btns(m.out[0][2]))
    # garbage callbacks are ignored, not crashes
    for bad in ("br:xx:0:0", "br:en:abc:0", "br:en"):
        c, m = cb(bad); await visitor.browse_navigate(c); assert m.out == []
    # nothing live at all -> polite message, no crash
    async with db.get_conn() as cn: await cn.execute("UPDATE exhibits SET status='draft'"); await cn.commit()
    msg = Msg("🗂 Browse", uid=7); await visitor.menu_browse(msg)
    assert msg.out[0][1] == visitor.NOT_READY_MESSAGES["en"]
    print("browse: All-default, tag chips (counts, visitor lang, Armenian fallback), filter+pagination, back buttons, delivery, empty-tag hiding OK")

    # reply-keyboard handlers: every language's labels route correctly
    for lang in visitor.LANGUAGE_LABELS:
        assert visitor.MENU_BROWSE_LABELS[lang] in visitor._BROWSE_LABEL_TO_LANG
        k = visitor.reply_menu(lang).keyboard[0]
        assert [b.text for b in k] == [visitor.MENU_BROWSE_LABELS[lang], visitor.MENU_LANGUAGE_LABELS[lang]]
    msg = Msg(visitor.MENU_LANGUAGE_LABELS["ru"]); await visitor.menu_language(msg)
    assert btns(msg.out[0][2])[0].callback_data == "welcome:am" and len(btns(msg.out[0][2])) == 10
    # the labels are really wired into the router filters (not just defined)
    import inspect
    router_src = inspect.getsource(visitor)
    assert "menu_browse" in router_src and "menu_language" in router_src
    # setMyCommands + menu button (details: test_ux.py)
    sent = []
    async def smc(cmds, language_code=None): sent.append((language_code, [c.command for c in cmds]))
    async def scmb(menu_button=None): sent.append(("menu", type(menu_button).__name__))
    bot = SimpleNamespace(set_my_commands=smc, set_chat_menu_button=scmb)
    await visitor.setup_bot_menu(bot)
    assert sent[0] == (None, ["start", "language", "list"]) and sent[-1] == ("menu", "MenuButtonCommands")
    print("reply keyboard (10 languages) + language button + setMyCommands/menu button OK")


async def test_dispatch():
    """Real aiogram Dispatcher (admin + visitor routers, as in main.py) fed real Update objects:
    proves the filters and router ORDER send each text to the right handler."""
    import datetime
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import SendMessage, SendPhoto, SendVoice, AnswerCallbackQuery
    from aiogram.types import Update, Message as TgMessage, Chat, User, CallbackQuery as TgCb
    from bot.admin import router as admin_router
    from bot.visitor import router as visitor_router

    use(os.path.join(TMP, "dispatch.sqlite3")); await migrate.migrate(); visitor._user_lang.clear()
    await add_exhibit("001"); await add_exhibit("002")
    t, _ = await db.get_or_create_tag("Հանքանյութեր"); await db.set_exhibit_tags("001", [t])
    now = datetime.datetime.now(); user = User(id=77, is_bot=False, first_name="V"); chat = Chat(id=77, type="private")

    class Rec(BaseSession):
        def __init__(self): super().__init__(); self.calls = []
        async def make_request(self, bot, method, timeout=None):
            self.calls.append(method)
            if isinstance(method, (SendMessage, SendPhoto, SendVoice)):
                return TgMessage(message_id=len(self.calls), date=now, chat=chat)
            return True
        async def stream_content(self, *a, **k): yield b""
        async def close(self): pass
    sess = Rec(); bot = Bot("123:abc", session=sess)
    dp = Dispatcher(); dp.include_router(admin_router); dp.include_router(visitor_router)
    n = [0]
    async def say(text):
        n[0] += 1; sess.calls.clear()
        await dp.feed_update(bot, Update(update_id=n[0], message=TgMessage(message_id=n[0], date=now, chat=chat, from_user=user, text=text)))
        return [c for c in sess.calls if isinstance(c, (SendMessage, SendPhoto, SendVoice))]
    async def tap(data):
        n[0] += 1; sess.calls.clear()
        base = TgMessage(message_id=500, date=now, chat=chat, from_user=User(id=1, is_bot=True, first_name="B"), text="list")
        await dp.feed_update(bot, Update(update_id=n[0], callback_query=TgCb(id=str(n[0]), from_user=user, chat_instance="x", message=base, data=data)))
        return sess.calls

    out = await say("hello"); assert out[0].text == "Please choose your language:"          # unknown language -> keyboard
    await tap("welcome:en"); await say("/start")                                             # language known now
    out = await say("hello"); assert out[0].text == visitor.HINT_MESSAGES["en"]
    out = await say("🗂 Browse"); assert out[0].text == "🗂 Exhibit list" and "ex:001:en" in str(out[0].reply_markup)
    out = await say("🌐 Language"); assert out[0].text == "Please choose your language:" and "welcome:am" in str(out[0].reply_markup)
    out = await say(" 001 "); assert [type(c).__name__ for c in out] == ["SendPhoto", "SendVoice"]
    out = await say("999"); assert out[0].text == visitor.NOT_FOUND_MESSAGES["en"]
    out = await say("🗂 Ցանկ / Browse"); assert out[0].text == visitor.LIST_LABELS["am"]       # label switches language
    calls = await tap("br:am:0:0"); assert any(type(c).__name__ == "EditMessageText" for c in calls)
    calls = await tap("bk:am"); assert any(type(c).__name__ == "EditMessageText" for c in calls)
    calls = await tap("ex:001:am")      # exhibit 001 only has an English recording -> polite "not available" text
    assert any(type(c).__name__ == "SendMessage" and "isn't available" in c.text for c in calls)
    print("dispatcher: menu labels, numbers, hint, language key, callbacks routed correctly with admin router first OK")


async def main():
    await test_migration(); await test_crud(); await test_translation_and_limit()
    await test_web(); await test_visitor_browse(); await test_dispatch()
    print("\nTAGS TESTS PASSED")

asyncio.run(main())
