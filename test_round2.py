"""Round-2 tests: text-alternative button (both entry paths), exhibit-list pagination,
non-numeric hint. (Tags, browse-by-tag and migrations: see test_tags.py.)
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
    async def edit_text(self, text, reply_markup=None): self.out.append(("edit", text, reply_markup))


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

    # --- welcome now carries the persistent reply keyboard (Browse + Language)
    c, m = cb("welcome:en", uid=11); await visitor.welcome_after_language(c)
    assert m.out[0][:2] == ("edit", "🇬🇧 English ✓") and m.out[0][2] is None        # picker collapsed
    kb = m.out[-1][2]
    assert [b.text for b in kb.keyboard[0]] == ["🗂 Browse", "🌐 Language"] and kb.is_persistent

    # --- list pagination: 12 live exhibits -> 8 + 4 (no tags yet -> no tag row)
    c, m = cb("list:en", uid=11); await visitor.show_exhibit_list(c)
    b = buttons(m.out[0][2])
    ex = [x for x in b if x.callback_data.startswith("ex:")]
    nav = [x for x in b if not x.callback_data.startswith("ex:")]
    assert len(ex) == 8 and ex[0].text == "001 — Idol" and ex[1].text == "002 — Title 2"
    assert next(x for x in b if x.callback_data == "ex:005:en").text == "005 — AM-005"   # no translation -> Armenian fallback
    assert [x.text for x in nav] == ["1/2", "▶", "🔙 Back"] and nav[1].callback_data == "br:en:0:1" and nav[2].callback_data == "bk:en"
    c, m = cb("br:en:0:1", uid=11); await visitor.browse_navigate(c)
    assert m.out[0][0] == "edit"
    b = buttons(m.out[0][2]); ex = [x for x in b if x.callback_data.startswith("ex:")]
    assert len(ex) == 4 and [x.text for x in b if not x.callback_data.startswith("ex:")] == ["◀", "2/2", "🔙 Back"]
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


async def main():
    await test_visitor()
    print("\nROUND-2 TESTS PASSED")

asyncio.run(main())
