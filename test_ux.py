"""'Small UX fixes' tests, through a REAL aiogram Dispatcher (admin + visitor routers, as in main.py):
1) reply keyboard on exhibit delivery (deep link / typed number / list), 2) language picker collapses
after a pick, 3) /language + /list commands and per-language setMyCommands."""
import asyncio, datetime, os, tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp()
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (SendMessage, SendPhoto, SendVoice, EditMessageText, SetMyCommands,
                             SetChatMenuButton, AnswerCallbackQuery)
from aiogram.types import Update, Message as TgMessage, Chat, User, CallbackQuery as TgCb, ReplyKeyboardMarkup, InlineKeyboardMarkup
from db import db, migrate
from bot import visitor
from bot.admin import router as admin_router
from bot.visitor import router as visitor_router

now = datetime.datetime.now()
chat = Chat(id=77, type="private")


class Rec(BaseSession):
    def __init__(self): super().__init__(); self.calls = []
    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendPhoto, SendVoice, EditMessageText)):
            return TgMessage(message_id=len(self.calls), date=now, chat=chat)
        return True
    async def stream_content(self, *a, **k): yield b""
    async def close(self): pass


async def add_exhibit(eid, photo=True):
    await db.create_exhibit(eid, f"AM-{eid}", "facts", created_by=None)
    await db.set_exhibit_status(eid, "live")
    if photo: await db.replace_exhibit_photos(eid, [f"photo-{eid}"])
    async with db.get_conn() as c:
        for lang in ("en", "ru"):
            await c.execute("INSERT INTO audio_cache (exhibit_id, language_code, script_text, title_translated, telegram_file_id, status)"
                            " VALUES (?,?,?,?,?,'ready')", (eid, lang, "Script.", f"{lang.upper()}-{eid}", f"voice-{eid}-{lang}"))
        await c.commit()


def names(calls): return [type(c).__name__ for c in calls]
def is_reply_kb(m, texts): return isinstance(m, ReplyKeyboardMarkup) and [b.text for b in m.keyboard[0]] == texts and m.is_persistent


async def main():
    db.DB_PATH = migrate.DB_PATH = os.path.join(TMP, "ux.sqlite3"); await migrate.migrate()
    visitor._user_lang.clear()
    await add_exhibit("001"); await add_exhibit("002", photo=False)

    sess = Rec(); bot = Bot("123:abc", session=sess)
    dp = Dispatcher(); dp.include_router(admin_router); dp.include_router(visitor_router)
    n = [0]; user = User(id=77, is_bot=False, first_name="V")
    async def say(text, uid=77):
        n[0] += 1; sess.calls.clear()
        u = User(id=uid, is_bot=False, first_name="V")
        await dp.feed_update(bot, Update(update_id=n[0], message=TgMessage(message_id=n[0], date=now, chat=chat, from_user=u, text=text)))
        return list(sess.calls)
    async def tap(data, uid=77):
        n[0] += 1; sess.calls.clear()
        base = TgMessage(message_id=500, date=now, chat=chat, from_user=User(id=1, is_bot=True, first_name="B"), text="Please choose your language:")
        await dp.feed_update(bot, Update(update_id=n[0], callback_query=TgCb(id=str(n[0]), from_user=User(id=uid, is_bot=False, first_name="V"), chat_instance="x", message=base, data=data)))
        return list(sess.calls)

    # ── 1+2: deep link end-to-end: /start exh_001 -> language keyboard -> pick -> collapse + delivery with reply keyboard
    calls = await say("/start exh_001")
    assert names(calls) == ["SendMessage"] and calls[0].text == "Please choose your language:"
    assert len(calls[0].reply_markup.inline_keyboard) == 10 and "🇬🇧 English" in str(calls[0].reply_markup)
    calls = await tap("lang:001:en")
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.text == "🇬🇧 English ✓" and edit.reply_markup is None, "picker collapsed to a confirmation, buttons removed"
    assert names(calls).index("EditMessageText") < names(calls).index("SendPhoto"), "collapse happens before the exhibit arrives"
    photo = next(c for c in calls if isinstance(c, SendPhoto)); voice = next(c for c in calls if isinstance(c, SendVoice))
    assert is_reply_kb(photo.reply_markup, ["🗂 Browse", "🌐 Language"]), "deep-link visitor gets the persistent menu on delivery"
    assert isinstance(voice.reply_markup, InlineKeyboardMarkup) and voice.reply_markup.inline_keyboard[0][0].callback_data == "txt:001:en"
    assert photo.caption == "EN-001"
    # localized: Russian pick -> Russian labels on the reply keyboard + confirmation
    calls = await tap("lang:001:ru", uid=78)
    assert next(c for c in calls if isinstance(c, EditMessageText)).text == "🇷🇺 Русский ✓"
    assert is_reply_kb(next(c for c in calls if isinstance(c, SendPhoto)).reply_markup, ["🗂 Список / Browse", "🌐 Язык / Language"])
    # exhibit without a photo: the keyboard rides on a title message instead
    calls = await tap("lang:002:en", uid=79)
    msg = next(c for c in calls if isinstance(c, SendMessage))
    assert msg.text == "EN-002" and is_reply_kb(msg.reply_markup, ["🗂 Browse", "🌐 Language"]) and "SendPhoto" not in names(calls)
    # typed number + list delivery carry it too
    calls = await say("001"); assert is_reply_kb(next(c for c in calls if isinstance(c, SendPhoto)).reply_markup, ["🗂 Browse", "🌐 Language"])
    calls = await tap("ex:001:en"); assert is_reply_kb(next(c for c in calls if isinstance(c, SendPhoto)).reply_markup, ["🗂 Browse", "🌐 Language"])
    # typed number with no known language -> keyboard -> pick -> collapse + delivery
    calls = await say("001", uid=90); assert calls[0].text == "Please choose your language:"
    calls = await tap("lang:001:en", uid=90); assert any(isinstance(c, EditMessageText) for c in calls) and "SendVoice" in names(calls)
    # tapping a stale language button twice is harmless (edit failure swallowed); invalid language ignored
    calls = await tap("lang:001:xx", uid=91); assert "SendPhoto" not in names(calls) and "EditMessageText" not in names(calls)
    # welcome flow also collapses
    calls = await tap("welcome:en", uid=92)
    assert next(c for c in calls if isinstance(c, EditMessageText)).text == "🇬🇧 English ✓"
    assert is_reply_kb(next(c for c in calls if isinstance(c, SendMessage)).reply_markup, ["🗂 Browse", "🌐 Language"])
    print("deep link end-to-end: picker collapses (EN/RU), reply keyboard on photo / title message / typed / list, invalid lang ignored OK")

    # ── 3: /language and /list behave like the reply-keyboard buttons
    calls = await say("/language"); assert calls[0].text == "Please choose your language:" and "welcome:am" in str(calls[0].reply_markup)
    reply_buttons = await say("🌐 Language"); assert reply_buttons[0].text == calls[0].text and str(reply_buttons[0].reply_markup) == str(calls[0].reply_markup)
    calls = await say("/list"); assert calls[0].text == "🗂 Exhibit list" and "ex:001:en" in str(calls[0].reply_markup)
    same = await say("🗂 Browse"); assert str(same[0].reply_markup) == str(calls[0].reply_markup)
    # /list with no language yet: language first, then the LIST (not the welcome); picker collapses
    calls = await say("/list", uid=555); assert "listfirst:ru" in str(calls[0].reply_markup)
    calls = await tap("listfirst:ru", uid=555)
    assert next(c for c in calls if isinstance(c, EditMessageText)).text == "🇷🇺 Русский ✓"
    assert next(c for c in calls if isinstance(c, SendMessage)).text == visitor.LIST_LABELS["ru"]
    # staff command namespace untouched: /review etc still go to the admin router (role-gated, silent for visitors)
    calls = await say("/review 001"); assert calls == [] or all(not isinstance(c, SendPhoto) for c in calls)
    print("/language and /list == reply-keyboard buttons (incl. no-language path) OK")

    # ── setMyCommands: default English + 10 localized sets, Armenian under Telegram's 'hy'
    sess.calls.clear(); await visitor.setup_bot_menu(bot)
    sets = [c for c in sess.calls if isinstance(c, SetMyCommands)]
    by_lang = {c.language_code: {x.command: x.description for x in c.commands} for c in sets}
    assert len(sets) == 11 and set(by_lang) == {None, "hy", "en", "ru", "fr", "es", "de", "fa", "zh", "it", "el"}
    assert all(list(d) == ["start", "language", "list"] for d in by_lang.values())
    assert by_lang[None]["language"] == "Change language" and by_lang["hy"]["list"] == "Ցուցանմուշների ցանկ" and by_lang["ru"]["start"] == "Начать"
    assert all(1 <= len(x) <= 256 for d in by_lang.values() for x in d.values())
    assert any(isinstance(c, SetChatMenuButton) for c in sess.calls)
    # a language Telegram rejects doesn't stop the rest
    class Flaky(Rec):
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, SetMyCommands) and method.language_code == "fa": raise RuntimeError("unsupported")
            return await super().make_request(bot, method, timeout)
    fs = Flaky(); await visitor.setup_bot_menu(Bot("123:abc", session=fs))
    assert len([c for c in fs.calls if isinstance(c, SetMyCommands)]) == 10 and any(isinstance(c, SetChatMenuButton) for c in fs.calls)
    print("setMyCommands: default + 10 localized (hy for Armenian), resilient to one failure OK")
    print("\nUX TESTS PASSED")

asyncio.run(main())
