"""Round-2 tests: text-alternative button (both entry paths), exhibit-list pagination,
non-numeric hint, categories (migration on an existing DB, on-the-fly creation, filter).
Runs against a throwaway SQLite file — never touches museum_bot.sqlite3.
"""
import asyncio, os, shutil, sqlite3, sys, tempfile
from types import SimpleNamespace
from unittest import mock

TMP = tempfile.mkdtemp()
os.environ.update(
    WEBAPP_ADMIN_USER="t", WEBAPP_SESSION_SECRET="s",
    WEBAPP_ADMIN_PASSWORD_HASH=__import__("bcrypt").hashpw(b"pw", __import__("bcrypt").gensalt()).decode(),
)
from db import db, migrate
from bot import visitor


def use_db(path):
    db.DB_PATH = path
    migrate.DB_PATH = path


class Msg:
    def __init__(self, text="", uid=1):
        self.text, self.from_user, self.chat = text, SimpleNamespace(id=uid), SimpleNamespace(type="private")
        self.out = []

    async def answer(self, text, reply_markup=None, **kw): self.out.append(("text", text, reply_markup))
    async def answer_photo(self, photo, caption=None, **kw): self.out.append(("photo", caption, None))
    async def answer_voice(self, voice, reply_markup=None, **kw): self.out.append(("voice", voice, reply_markup))
    async def edit_reply_markup(self, reply_markup=None): self.out.append(("edit", None, reply_markup))


def cb(data, uid=1):
    m = Msg(uid=uid)
    async def _ans(*a, **k): pass
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=m, answer=_ans), m


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


async def add_live(eid, title_en, script="Script.", lang="en", with_title=True):
    await db.create_exhibit(eid, f"AM-{eid}", "facts", created_by=None)
    await db.set_exhibit_status(eid, "live")
    await db.replace_exhibit_photos(eid, [f"photo-{eid}"])
    async with db.get_conn() as c:
        await c.execute(
            "INSERT INTO audio_cache (exhibit_id, language_code, script_text, title_translated, telegram_file_id, status)"
            " VALUES (?,?,?,?,?, 'ready')", (eid, lang, script, title_en if with_title else None, f"voice-{eid}"))
        await c.commit()


async def test_migration():
    legacy = os.path.join(TMP, "legacy.sqlite3")
    if os.path.exists("museum_bot.sqlite3"):
        shutil.copy("museum_bot.sqlite3", legacy)          # real existing-shaped DB (copy)
    else:
        raise SystemExit("need museum_bot.sqlite3 to copy")
    use_db(legacy)
    c = sqlite3.connect(legacy)
    c.execute("DROP TABLE IF EXISTS categories")
    cols = [r[1] for r in c.execute("PRAGMA table_info(exhibits)")]
    if "category_id" in cols:      # make it look pre-categories: rebuild exhibits without the column
        c.executescript("""PRAGMA foreign_keys=OFF;
          CREATE TABLE ex_old AS SELECT id,title_am,fact_sheet_am,qr_code_path,deep_link,status,created_by,created_at,updated_at FROM exhibits;
          DROP TABLE exhibits; ALTER TABLE ex_old RENAME TO exhibits;""")
    before = c.execute("SELECT id,title_am,status FROM exhibits ORDER BY id").fetchall()
    c.commit(); c.close()
    await migrate.migrate(); await migrate.migrate()        # twice = idempotent
    c = sqlite3.connect(legacy)
    assert "category_id" in [r[1] for r in c.execute("PRAGMA table_info(exhibits)")]
    assert c.execute("SELECT name FROM sqlite_master WHERE name='categories'").fetchone()
    assert c.execute("SELECT id,title_am,status FROM exhibits ORDER BY id").fetchall() == before
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
    fresh = os.path.join(TMP, "fresh.sqlite3"); use_db(fresh); await migrate.migrate()
    c = sqlite3.connect(fresh)
    assert "category_id" in [r[1] for r in c.execute("PRAGMA table_info(exhibits)")]
    c.close()
    print(f"migration: legacy DB ({len(before)} exhibits kept) + fresh DB + re-run OK")


async def test_visitor():
    use_db(os.path.join(TMP, "visitor.sqlite3")); await migrate.migrate()
    visitor._user_lang.clear()
    long_script = "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(12))   # ~13k chars
    await add_live("001", "Idol", script=long_script)
    for i in range(2, 13):
        await add_live(f"{i:03d}", f"Title {i}", with_title=(i != 5))

    # --- text button, entry path 1: deep link -> language pick
    c, m = cb("lang:001:en", uid=10)
    await visitor.deliver_guide(c)
    voice = [o for o in m.out if o[0] == "voice"][0]
    btn = buttons(voice[2])[0]
    assert btn.text == "📖 Read text" and btn.callback_data == "txt:001:en", btn
    # --- entry path 2: typed number (language already known)
    msg = Msg("1", uid=10); await visitor.typed_exhibit_number(msg)
    assert buttons([o for o in msg.out if o[0] == "voice"][0][2])[0].callback_data == "txt:001:en"
    # --- tapping it: split safely under 4096
    c, m = cb("txt:001:en", uid=10); await visitor.send_script_text(c)
    chunks = [o[1] for o in m.out]
    assert len(chunks) >= 3 and all(len(x) <= 4096 for x in chunks), [len(x) for x in chunks]
    assert "".join(chunks).replace("\n", "").replace(" ", "") == long_script.replace("\n", "").replace(" ", "")
    # every language label exists, Armenian shown for am
    assert visitor.READ_TEXT_LABELS["am"] == "📖 Կարդալ տեքստը"
    # unbroken run longer than limit still splits
    assert [len(x) for x in visitor.split_text("x" * 9000)] == [4096, 4096, 808]
    print("text button: both entry paths + >4096 split OK (", [len(x) for x in chunks], ")")

    # --- welcome has list button
    c, m = cb("welcome:en", uid=11); await visitor.welcome_after_language(c)
    assert buttons(m.out[0][2])[0].text == "🗂 Exhibit list" and buttons(m.out[0][2])[0].callback_data == "list:en"

    # --- list pagination: 12 live exhibits -> 8 + 4
    c, m = cb("list:en", uid=11); await visitor.show_exhibit_list(c)
    kb = m.out[0][2]; b = buttons(kb)
    ex = [x for x in b if x.callback_data.startswith("ex:")]
    nav = [x for x in b if not x.callback_data.startswith("ex:")]
    assert len(ex) == 8 and ex[0].text == "001 — Idol" and ex[1].text == "002 — Title 2"
    assert next(x for x in b if x.callback_data == "ex:005:en").text == "005 — AM-005"   # no translation -> Armenian fallback
    assert [x.text for x in nav] == ["1/2", "▶"] and nav[1].callback_data == "listp:en:1"
    c, m = cb("listp:en:1", uid=11); await visitor.show_exhibit_list(c)
    assert m.out[0][0] == "edit"
    b = buttons(m.out[0][2]); ex = [x for x in b if x.callback_data.startswith("ex:")]
    assert len(ex) == 4 and [x.text for x in b if not x.callback_data.startswith("ex:")] == ["◀", "2/2"]
    # tap an exhibit from the list -> same delivery (photo+voice+text button)
    c, m = cb("ex:012:en", uid=11); await visitor.deliver_from_list(c)
    assert [o[0] for o in m.out] == ["photo", "voice"] and buttons(m.out[1][2])[0].callback_data == "txt:012:en"
    # non-live exhibits never listed
    await db.set_exhibit_status("012", "unpublished")
    assert len(await db.list_live_exhibits_for_lang("en")) == 11
    print("list pagination (12 -> 8+4, prev/next, fallback title, delivery from list) OK")

    # --- hints
    msg = Msg("hello", uid=11); await visitor.non_numeric_text(msg)
    assert msg.out[0][1] == visitor.HINT_MESSAGES["en"] and buttons(msg.out[0][2])[0].callback_data == "list:en"
    visitor._user_lang.clear(); msg = Msg("hello", uid=99); await visitor.non_numeric_text(msg)
    assert msg.out[0][1] == "Please choose your language:"
    print("non-numeric hint (known + unknown language) OK")


async def test_categories_web():
    use_db(os.path.join(TMP, "web.sqlite3")); await migrate.migrate()
    from fastapi.testclient import TestClient
    from webapp import app as webapp_app
    async def fake_upload(bot, files): return ["fid"] * len(files)
    with mock.patch.object(webapp_app, "_upload_photos", fake_upload):
        app = webapp_app.create_app(bot=None)
        cl = TestClient(app, follow_redirects=False)
        # TestClient is sync; run it in a thread so our event loop (aiosqlite) stays free
        run = lambda f, *a, **k: asyncio.to_thread(f, *a, **k)
        assert (await run(cl.post, "/login", data={"username": "t", "password": "pw"})).status_code in (303, 302)
        files = lambda: [("photos", ("p.jpg", b"x", "image/jpeg"))]
        form = lambda **kw: {"title_am": "Վերնագիր", "fact_sheet_text": "փաստեր", **kw}

        r = await run(cl.post, "/exhibits/new", data=form(category="__new__", new_category="Հանքանյութեր"), files=files())
        assert r.status_code == 303, r.text[:300]
        r = await run(cl.post, "/exhibits/new", data=form(category="__new__", new_category="  ՀԱՆՔԱՆՅՈՒԹԵՐ "), files=files())
        cats = await db.list_categories()
        assert [c["name_hy"] for c in cats] == ["Հանքանյութեր"], "same name (case/space-insens.) must reuse"
        e1, e2 = await db.get_exhibit("001"), await db.get_exhibit("002")
        assert e1["category_id"] == e2["category_id"] == cats[0]["id"]
        # blank new name -> 400, nothing created
        r = await run(cl.post, "/exhibits/new", data=form(category="__new__", new_category=" "), files=files())
        assert r.status_code == 400 and len(await db.list_categories()) == 1 and await db.get_exhibit("003") is None
        # none
        await run(cl.post, "/exhibits/new", data=form(category=""), files=files())
        assert (await db.get_exhibit("003"))["category_id"] is None
        # rejected form must not leave an orphan category
        r = await run(cl.post, "/exhibits/new", data=form(exhibit_id="001", category="__new__", new_category="Orphan"), files=files())
        assert r.status_code == 400 and len(await db.list_categories()) == 1
        # edit: new category on existing exhibit; staleness untouched
        async with db.get_conn() as c:
            await c.execute("UPDATE exhibits SET updated_at='2000-01-01 00:00:00' WHERE id='003'"); await c.commit()
        r = await run(cl.post, "/exhibits/003/category", data={"category": "__new__", "new_category": "Մետաղներ"})
        assert r.status_code == 303
        cats = await db.list_categories()
        assert [c["name_hy"] for c in cats] == ["Հանքանյութեր", "Մետաղներ"]
        e3 = await db.get_exhibit("003")
        assert e3["category_id"] == cats[1]["id"] and e3["updated_at"].startswith("2000")
        # pages render: dashboard column + filter, new form, detail form
        html = (await run(cl.get, "/")).text
        assert "<th>Category</th>" in html and "Մետաղներ" in html
        only = (await run(cl.get, f"/?category={cats[1]['id']}")).text
        assert "003" in only and ">001<" not in only and ">002<" not in only
        none_html = (await run(cl.get, "/?category=none")).text
        assert ">003<" not in none_html
        assert "Add new category" in (await run(cl.get, "/exhibits/new")).text
        d = (await run(cl.get, "/exhibits/003")).text
        assert "Save category" in d and f'value="{cats[1]["id"]}" selected' in d
    print("categories: on-the-fly create/reuse, blank-name + orphan guards, edit w/o staleness, column+filter OK")


async def main():
    await test_migration(); await test_visitor(); await test_categories_web()
    print("\nROUND-2 TESTS PASSED")

asyncio.run(main())
