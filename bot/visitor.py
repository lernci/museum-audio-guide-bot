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


@router.message(CommandStart(deep_link=True))
async def start_with_exhibit(message: Message, command: CommandObject):
    payload = command.args or ""
    if not payload.startswith("exh_"):
        await message.answer("Welcome! Scan an exhibit's QR code to begin.")
        return
    exhibit_id = payload.removeprefix("exh_")
    exhibit = await db.get_exhibit(exhibit_id)
    if exhibit is None or exhibit["status"] != "ready":
        await message.answer("This exhibit's guide isn't ready yet — please ask a staff member.")
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
    cached = await db.get_cached_voice(exhibit_id, lang)

    if cached is None:
        await callback.message.answer("Sorry, this language isn't available for this exhibit yet.")
        return

    if exhibit["photo_file_id"]:
        await callback.message.answer_photo(exhibit["photo_file_id"], caption=exhibit["title_am"])
    await callback.message.answer_voice(cached["telegram_file_id"])

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT INTO exhibit_views (exhibit_id, telegram_user_id, language_code) VALUES (?, ?, ?)",
            (exhibit_id, callback.from_user.id, lang),
        )
        await conn.commit()
