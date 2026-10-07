"""Visitor-side: deep link -> language picker -> instant cached voice + photo."""
from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import db

router = Router()

LANGUAGE_LABELS = {
    "am": "Հայերեն", "en": "English", "ru": "Русский", "fr": "Français",
    "es": "Español", "de": "Deutsch", "fa": "فارسی", "zh": "中文",
    "it": "Italiano", "el": "Ελληνικά",
}


NOT_READY_MESSAGES = {
    "am": "Այս ցուցանմուշի աուդիո ուղեցույցը դեռ պատրաստ չէ։ Խնդրում ենք փորձել ավելի ուշ։",
    "en": "This exhibit's audio guide will be available soon. Please check back later.",
    "ru": "Аудиогид для этого экспоната скоро появится. Пожалуйста, зайдите позже.",
    "fr": "Le guide audio de cette pièce sera bientôt disponible. Merci de revenir plus tard.",
    "es": "La audioguía de esta pieza estará disponible pronto. Vuelva más tarde.",
    "de": "Der Audioguide für dieses Exponat ist bald verfügbar. Bitte später erneut versuchen.",
    "fa": "راهنمای صوتی این نمایشگاه به‌زودی در دسترس خواهد بود. لطفاً بعداً دوباره سر بزنید.",
    "zh": "该展品的语音导览即将上线，请稍后再来查看。",
    "it": "L'audioguida di questo reperto sarà presto disponibile. Torna più tardi.",
    "el": "Ο ηχητικός οδηγός αυτού του εκθέματος θα είναι σύντομα διαθέσιμος. Παρακαλώ ξαναδοκιμάστε αργότερα.",
}


@router.message(CommandStart(deep_link=True))
async def start_with_exhibit(message: Message, command: CommandObject):
    payload = command.args or ""
    if not payload.startswith("exh_"):
        await message.answer("Welcome! Scan an exhibit's QR code to begin.")
        return
    exhibit_id = payload.removeprefix("exh_")
    exhibit = await db.get_exhibit(exhibit_id)
    if exhibit is None or exhibit["status"] != "live":
        # no language picked yet — show every language's "coming soon" so any visitor understands
        await message.answer("\n\n".join(NOT_READY_MESSAGES.values()))
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"lang:{exhibit_id}:{code}")]
        for code, label in LANGUAGE_LABELS.items()
    ])
    await message.answer("Please choose your language:", reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("lang:"))
async def deliver_guide(callback: CallbackQuery):
    _, exhibit_id, lang = callback.data.split(":")
    exhibit = await db.get_exhibit(exhibit_id)

    if exhibit is None or exhibit["status"] != "live":
        await callback.message.answer(NOT_READY_MESSAGES.get(lang, NOT_READY_MESSAGES["en"]))
        return

    cached = await db.get_cached_voice(exhibit_id, lang)
    if cached is None:
        await callback.message.answer("Sorry, this language isn't available for this exhibit yet.")
        return

    photos = await db.get_exhibit_photos(exhibit_id)
    if photos:
        caption = cached["title_translated"] or exhibit["title_am"]
        await callback.message.answer_photo(photos[0]["telegram_file_id"], caption=caption)
    await callback.message.answer_voice(cached["telegram_file_id"])

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT INTO exhibit_views (exhibit_id, telegram_user_id, language_code) VALUES (?, ?, ?)",
            (exhibit_id, callback.from_user.id, lang),
        )
        await conn.commit()
