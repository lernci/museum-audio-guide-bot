"""Admin-side FSM: staff add/edit exhibits, owners review + publish them.

Roles: 'content' can add and edit exhibits; 'owner' additionally gets
/review, /publish, and /retry. A user with no staff_users row is a public
visitor and every admin command silently no-ops for them — this bot also
serves the visitor flow in bot/visitor.py on the same token.
"""
import io

from aiogram import Router, F
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import db
from worker.pipeline import enqueue_exhibit, retry_language
from bot.visitor import LANGUAGE_LABELS
from utils.fact_sheet import extract_fact_sheet_text

router = Router()

MAX_PHOTOS = 3
CONTENT_ROLES = ("content", "owner")   # anyone on staff can add/edit
OWNER_ROLES = ("owner",)               # review/publish/retry are owner-only


class ExhibitForm(StatesGroup):
    waiting_number = State()
    waiting_title = State()
    waiting_photos = State()
    waiting_facts = State()
    confirm = State()


class EditForm(StatesGroup):
    waiting_photos = State()
    waiting_facts = State()
    confirm = State()


# ── permission helpers ──────────────────────────────────────────────────
async def _role(user_id: int):
    return await db.get_staff_role(user_id)


async def _require_role(message: Message, allowed_roles: tuple) -> bool:
    """Returns True if allowed. Silently no-ops for non-staff (public visitor
    traffic shares this bot); tells staff with the wrong role why they were
    refused, since that's an actionable, unambiguous case."""
    role = await _role(message.from_user.id)
    if role is None:
        return False
    if role not in allowed_roles:
        await message.answer(f"This command needs the '{'/'.join(allowed_roles)}' role.")
        return False
    return True


def _photos_keyboard(count: int) -> InlineKeyboardMarkup:
    buttons = []
    if count < MAX_PHOTOS:
        buttons.append(InlineKeyboardButton(text="Add another photo", callback_data="photos_more"))
    buttons.append(InlineKeyboardButton(text=f"Done ({count} photo{'s' if count != 1 else ''})", callback_data="photos_done"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def _extract_fact_sheet_text(message: Message) -> str | None:
    """Accepts either a plain text message or an attached .txt/.docx file."""
    if message.text:
        return extract_fact_sheet_text(message.text)
    if message.document:
        name = message.document.file_name or ""
        tg_file = await message.bot.get_file(message.document.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(tg_file.file_path, destination=buf)
        return extract_fact_sheet_text(None, name, buf.getvalue())
    return None


# ── new exhibit ─────────────────────────────────────────────────────────
@router.message(Command("new_exhibit"))
async def start_new_exhibit(message: Message, state: FSMContext):
    if not await _require_role(message, CONTENT_ROLES):
        return
    await state.set_state(ExhibitForm.waiting_number)
    await message.answer("New exhibit — send the exhibit number/ID (e.g. 007):")


@router.message(StateFilter(ExhibitForm.waiting_number))
async def got_number(message: Message, state: FSMContext):
    await state.update_data(exhibit_id=message.text.strip())
    await state.set_state(ExhibitForm.waiting_title)
    await message.answer("Title in Armenian:")


@router.message(StateFilter(ExhibitForm.waiting_title))
async def got_title(message: Message, state: FSMContext):
    await state.update_data(title_am=message.text.strip(), photos=[])
    await state.set_state(ExhibitForm.waiting_photos)
    await message.answer("Send 1–3 exhibit photos (one by one, or as an album):")


@router.message(StateFilter(ExhibitForm.waiting_photos), F.photo)
async def got_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    if len(photos) >= MAX_PHOTOS:
        await state.set_state(ExhibitForm.waiting_facts)
        await message.answer(f"Got {MAX_PHOTOS} photos. Now send the core fact sheet in Armenian "
                              f"(plain text, or attach a .txt/.docx file):")
    else:
        await message.answer(f"Photo {len(photos)}/{MAX_PHOTOS} received.", reply_markup=_photos_keyboard(len(photos)))


@router.callback_query(StateFilter(ExhibitForm.waiting_photos), F.data == "photos_more")
async def photos_more(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Send the next photo.")


@router.callback_query(StateFilter(ExhibitForm.waiting_photos), F.data == "photos_done")
async def photos_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("photos"):
        await callback.answer("Send at least one photo first.", show_alert=True)
        return
    await state.set_state(ExhibitForm.waiting_facts)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Core fact sheet in Armenian (plain text, or attach a .txt/.docx file):")


@router.message(StateFilter(ExhibitForm.waiting_facts))
async def got_facts(message: Message, state: FSMContext):
    fact_sheet_am = await _extract_fact_sheet_text(message)
    if not fact_sheet_am:
        await message.answer("Send the fact sheet as plain text, or attach a .txt/.docx file.")
        return
    await state.update_data(fact_sheet_am=fact_sheet_am)
    data = await state.get_data()
    await state.set_state(ExhibitForm.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="exhibit_confirm"),
        InlineKeyboardButton(text="Cancel", callback_data="exhibit_cancel"),
    ]])
    await message.answer(
        f"Exhibit {data['exhibit_id']}\nTitle: {data['title_am']}\nPhotos: {len(data['photos'])}\n\n"
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
        created_by=callback.from_user.id,
    )
    await db.replace_exhibit_photos(data["exhibit_id"], data["photos"])
    await enqueue_exhibit(data["exhibit_id"])
    await state.clear()
    await callback.message.edit_text(
        f"Exhibit {data['exhibit_id']} queued. QR code, translations, and audio "
        f"are generating in the background — you'll get a message when it's ready for review."
    )


@router.callback_query(StateFilter(ExhibitForm.confirm), F.data == "exhibit_cancel")
async def cancel_exhibit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Cancelled.")


# ── edit existing exhibit ───────────────────────────────────────────────
@router.message(Command("edit_exhibit"))
async def start_edit_exhibit(message: Message, command: CommandObject, state: FSMContext):
    if not await _require_role(message, CONTENT_ROLES):
        return
    exhibit_id = (command.args or "").strip()
    exhibit = await db.get_exhibit(exhibit_id) if exhibit_id else None
    if exhibit is None:
        await message.answer("Usage: /edit_exhibit <exhibit_id>")
        return
    await state.set_state(EditForm.waiting_photos)
    await state.update_data(exhibit_id=exhibit_id, photos=[])
    await message.answer(
        f"Editing exhibit {exhibit_id} ({exhibit['title_am']}).\n"
        f"Send 1–3 new photos to replace the existing ones, or /skip to keep them:"
    )


@router.message(StateFilter(EditForm.waiting_photos), Command("skip"))
async def edit_skip_photos(message: Message, state: FSMContext):
    await state.set_state(EditForm.waiting_facts)
    await message.answer("Keeping existing photos. Now send the updated fact sheet in Armenian "
                          "(plain text, or attach a .txt/.docx file), or /skip to keep the existing text:")


@router.message(StateFilter(EditForm.waiting_photos), F.photo)
async def edit_got_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if len(photos) >= MAX_PHOTOS:
        return
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    if len(photos) >= MAX_PHOTOS:
        await state.set_state(EditForm.waiting_facts)
        await message.answer(f"Got {MAX_PHOTOS} photos. Now send the updated fact sheet in Armenian "
                              f"(plain text, or attach a .txt/.docx file), or /skip to keep the existing text:")
    else:
        await message.answer(f"Photo {len(photos)}/{MAX_PHOTOS} received.", reply_markup=_photos_keyboard(len(photos)))


@router.callback_query(StateFilter(EditForm.waiting_photos), F.data == "photos_more")
async def edit_photos_more(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Send the next photo.")


@router.callback_query(StateFilter(EditForm.waiting_photos), F.data == "photos_done")
async def edit_photos_done(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditForm.waiting_facts)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Now send the updated fact sheet in Armenian "
                                   "(plain text, or attach a .txt/.docx file), or /skip to keep the existing text:")


@router.message(StateFilter(EditForm.waiting_facts), Command("skip"))
async def edit_skip_facts(message: Message, state: FSMContext):
    await state.update_data(fact_sheet_am=None)
    await _edit_show_confirm(message, state)


@router.message(StateFilter(EditForm.waiting_facts))
async def edit_got_facts(message: Message, state: FSMContext):
    fact_sheet_am = await _extract_fact_sheet_text(message)
    if not fact_sheet_am:
        await message.answer("Send the fact sheet as plain text, attach a .txt/.docx file, or /skip.")
        return
    await state.update_data(fact_sheet_am=fact_sheet_am)
    await _edit_show_confirm(message, state)


async def _edit_show_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(EditForm.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="edit_confirm"),
        InlineKeyboardButton(text="Cancel", callback_data="edit_cancel"),
    ]])
    summary = (
        f"Exhibit {data['exhibit_id']}\n"
        f"Photos: {'replace with ' + str(len(data['photos'])) + ' new' if data.get('photos') else 'unchanged'}\n"
        f"Fact sheet: {'updated' if data.get('fact_sheet_am') else 'unchanged'}\n\n"
        f"Confirming resets this exhibit to draft and regenerates every language."
    )
    await message.answer(summary, reply_markup=keyboard)


@router.callback_query(StateFilter(EditForm.confirm), F.data == "edit_confirm")
async def edit_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    exhibit_id = data["exhibit_id"]
    await db.update_exhibit_content(exhibit_id, fact_sheet_am=data.get("fact_sheet_am"))
    if data.get("photos"):
        await db.replace_exhibit_photos(exhibit_id, data["photos"])
    await db.clear_exhibit_generation_state(exhibit_id)
    await db.set_exhibit_status(exhibit_id, "draft")
    await enqueue_exhibit(exhibit_id)
    await state.clear()
    await callback.message.edit_text(
        f"Exhibit {exhibit_id} resubmitted — regenerating every language, "
        f"you'll get a message when it's ready for review again."
    )


@router.callback_query(StateFilter(EditForm.confirm), F.data == "edit_cancel")
async def edit_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Cancelled.")


# ── owner review flow ───────────────────────────────────────────────────
@router.message(Command("review"))
async def review_exhibit(message: Message, command: CommandObject):
    if not await _require_role(message, OWNER_ROLES):
        return
    exhibit_id = (command.args or "").strip()
    exhibit = await db.get_exhibit(exhibit_id) if exhibit_id else None
    if exhibit is None:
        await message.answer("Usage: /review <exhibit_id>")
        return

    photos = await db.get_exhibit_photos(exhibit_id)
    caption = f"Exhibit {exhibit_id} — {exhibit['title_am']} (status: {exhibit['status']})"
    if photos:
        await message.answer_photo(photos[0]["telegram_file_id"], caption=caption)
    else:
        await message.answer(caption)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"adminlang:{exhibit_id}:{code}")]
        for code, label in LANGUAGE_LABELS.items()
    ])
    await message.answer("Pick a language to proofread:", reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("adminlang:"))
async def review_language(callback: CallbackQuery):
    role = await _role(callback.from_user.id)
    if role not in OWNER_ROLES:
        await callback.answer("Owner role required.", show_alert=True)
        return
    _, exhibit_id, lang = callback.data.split(":")
    row = await db.get_audio_row(exhibit_id, lang)
    if row is None:
        await callback.message.answer("No script generated for this language yet.")
        return
    title = row["title_translated"] or "(no title)"
    script = row["script_text"] or "(no script)"
    await callback.message.answer(f"[{lang}] {title}\n\n{script}\n\n(status: {row['status']})")
    if row["status"] == "ready" and row["telegram_file_id"]:
        await callback.message.answer_voice(row["telegram_file_id"])


@router.message(Command("publish"))
async def publish_exhibit(message: Message, command: CommandObject):
    if not await _require_role(message, OWNER_ROLES):
        return
    exhibit_id = (command.args or "").strip()
    exhibit = await db.get_exhibit(exhibit_id) if exhibit_id else None
    if exhibit is None:
        await message.answer("Usage: /publish <exhibit_id>")
        return
    if exhibit["status"] != "review":
        await message.answer(f"Exhibit {exhibit_id} is '{exhibit['status']}', not 'review' — nothing to publish.")
        return
    await db.set_exhibit_status(exhibit_id, "live")
    await message.answer(f"Exhibit {exhibit_id} is now live.")


@router.message(Command("retry"))
async def retry_exhibit_language(message: Message):
    if not await _require_role(message, OWNER_ROLES):
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
