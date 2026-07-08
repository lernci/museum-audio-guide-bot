"""Admin-side FSM: staff add a new exhibit, pipeline runs in the background."""
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import db
from worker.pipeline import enqueue_exhibit, retry_language

router = Router()


class ExhibitForm(StatesGroup):
    waiting_number = State()
    waiting_title = State()
    waiting_photo = State()
    waiting_facts = State()
    confirm = State()


async def _is_staff(user_id: int) -> bool:
    async with db.get_conn() as conn:
        cur = await conn.execute("SELECT 1 FROM staff_users WHERE telegram_user_id = ?", (user_id,))
        return await cur.fetchone() is not None


@router.message(Command("new_exhibit"))
async def start_new_exhibit(message: Message, state: FSMContext):
    if not await _is_staff(message.from_user.id):
        return  # silently ignore — this bot also serves the public visitor flow
    await state.set_state(ExhibitForm.waiting_number)
    await message.answer("New exhibit — send the exhibit number/ID (e.g. 007):")


@router.message(StateFilter(ExhibitForm.waiting_number))
async def got_number(message: Message, state: FSMContext):
    await state.update_data(exhibit_id=message.text.strip())
    await state.set_state(ExhibitForm.waiting_title)
    await message.answer("Title in Armenian:")


@router.message(StateFilter(ExhibitForm.waiting_title))
async def got_title(message: Message, state: FSMContext):
    await state.update_data(title_am=message.text.strip())
    await state.set_state(ExhibitForm.waiting_photo)
    await message.answer("Send the exhibit photo:")


@router.message(StateFilter(ExhibitForm.waiting_photo), F.photo)
async def got_photo(message: Message, state: FSMContext):
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await state.set_state(ExhibitForm.waiting_facts)
    await message.answer("Core fact sheet in Armenian (plain text, staff notes are fine):")


@router.message(StateFilter(ExhibitForm.waiting_facts))
async def got_facts(message: Message, state: FSMContext):
    await state.update_data(fact_sheet_am=message.text.strip())
    data = await state.get_data()
    await state.set_state(ExhibitForm.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="exhibit_confirm"),
        InlineKeyboardButton(text="Cancel", callback_data="exhibit_cancel"),
    ]])
    await message.answer(
        f"Exhibit {data['exhibit_id']}\nTitle: {data['title_am']}\n\n"
        f"{data['fact_sheet_am']}\n\nConfirm to start translation + audio generation?",
        reply_markup=keyboard,
    )


@router.callback_query(StateFilter(ExhibitForm.confirm), F.data == "exhibit_confirm")
async def confirm_exhibit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await db.create_exhibit(
        exhibit_id=data["exhibit_id"],
        title_am=data["title_am"],
        fact_sheet_am=data["fact_sheet_am"],
        photo_file_id=data["photo_file_id"],
        created_by=callback.from_user.id,
    )
    await enqueue_exhibit(data["exhibit_id"])
    await state.clear()
    await callback.message.edit_text(
        f"Exhibit {data['exhibit_id']} queued. QR code, translations, and audio "
        f"are generating in the background — you'll get a message when it's ready."
    )


@router.callback_query(StateFilter(ExhibitForm.confirm), F.data == "exhibit_cancel")
async def cancel_exhibit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Cancelled.")


@router.message(Command("retry"))
async def retry_exhibit_language(message: Message):
    if not await _is_staff(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Usage: /retry <exhibit_id> <language_code>")
        return
    _, exhibit_id, lang = parts
    try:
        await retry_language(message.bot, exhibit_id, lang)
        await message.answer(f"Retried {exhibit_id}/{lang}.")
    except Exception as exc:  # noqa: BLE001 — surface the failure to the admin, don't crash the bot
        await message.answer(f"Retry failed: {exc}")
