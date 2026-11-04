"""Visitor-side: deep link -> language picker -> instant cached voice + photo."""
import re

from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import db

router = Router()

LANGUAGE_LABELS = {
    "am": "🇦🇲 Հայերեն", "en": "🇬🇧 English", "ru": "🇷🇺 Русский", "fr": "🇫🇷 Français",
    "es": "🇪🇸 Español", "de": "🇩🇪 Deutsch", "fa": "🇮🇷 فارسی", "zh": "🇨🇳 中文",
    "it": "🇮🇹 Italiano", "el": "🇬🇷 Ελληνικά",
}


def build_language_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    """The single language picker used by the visitor flow and admin /review.
    Each button's callback_data is f"{callback_prefix}:{lang_code}"."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{callback_prefix}:{code}")]
        for code, label in LANGUAGE_LABELS.items()
    ])


WELCOME_MESSAGES = {
    "am": "Բարի գալուստ Գորիսի երկրաբանական թանգարանի աուդիո ուղեցույց։ Սկանավորեք ցուցանմուշի կողքի QR կոդը կամ մուտքագրեք դրա համարը (օրինակ՝ 001)։",
    "en": "Welcome to the Goris Geological Museum audio guide. Scan the QR code next to an exhibit, or type its number (e.g. 001).",
    "ru": "Добро пожаловать в аудиогид Горисского геологического музея. Отсканируйте QR-код рядом с экспонатом или введите его номер (например, 001).",
    "fr": "Bienvenue dans l'audioguide du Musée géologique de Goris. Scannez le code QR à côté d'une pièce ou saisissez son numéro (par ex. 001).",
    "es": "Bienvenido a la audioguía del Museo Geológico de Goris. Escanee el código QR junto a una pieza o escriba su número (por ejemplo, 001).",
    "de": "Willkommen beim Audioguide des Geologischen Museums von Goris. Scannen Sie den QR-Code neben einem Exponat oder geben Sie dessen Nummer ein (z. B. 001).",
    "fa": "به راهنمای صوتی موزه زمین‌شناسی گوریس خوش آمدید. کد QR کنار هر نمایه را اسکن کنید یا شماره آن را وارد کنید (مثلاً 001).",
    "zh": "欢迎使用戈里斯地质博物馆语音导览。请扫描展品旁的二维码，或输入展品编号（例如 001）。",
    "it": "Benvenuto nell'audioguida del Museo Geologico di Goris. Inquadra il codice QR accanto a un reperto oppure digita il suo numero (ad es. 001).",
    "el": "Καλώς ήρθατε στον ηχητικό οδηγό του Γεωλογικού Μουσείου Γκόρις. Σαρώστε τον κωδικό QR δίπλα σε ένα έκθεμα ή πληκτρολογήστε τον αριθμό του (π.χ. 001).",
}

NOT_FOUND_MESSAGES = {
    "am": "Ցուցանմուշը չի գտնվել կամ դեռ հասանելի չէ։ Խնդրում ենք ստուգել համարը և փորձել կրկին։",
    "en": "Exhibit not found or not yet available. Please check the number and try again.",
    "ru": "Экспонат не найден или пока недоступен. Проверьте номер и попробуйте снова.",
    "fr": "Pièce introuvable ou pas encore disponible. Vérifiez le numéro et réessayez.",
    "es": "Pieza no encontrada o aún no disponible. Compruebe el número e inténtelo de nuevo.",
    "de": "Exponat nicht gefunden oder noch nicht verfügbar. Bitte prüfen Sie die Nummer und versuchen Sie es erneut.",
    "fa": "نمایه پیدا نشد یا هنوز در دسترس نیست. لطفاً شماره را بررسی کنید و دوباره تلاش کنید.",
    "zh": "未找到该展品，或暂未开放。请检查编号后重试。",
    "it": "Reperto non trovato o non ancora disponibile. Controlla il numero e riprova.",
    "el": "Το έκθεμα δεν βρέθηκε ή δεν είναι ακόμη διαθέσιμο. Ελέγξτε τον αριθμό και δοκιμάστε ξανά.",
}

# Visitor's last chosen language (in-memory; falls back to exhibit_views after a restart).
_user_lang: dict[int, str] = {}

NUMBER_RE = re.compile(r"\s*\d+\s*")


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
        await start_general(message)
        return
    exhibit_id = payload.removeprefix("exh_")
    exhibit = await db.get_exhibit(exhibit_id)
    if exhibit is None or exhibit["status"] != "live":
        # no language picked yet — show every language's "coming soon" so any visitor understands
        await message.answer("\n\n".join(NOT_READY_MESSAGES.values()))
        return

    await message.answer("Please choose your language:", reply_markup=build_language_keyboard(f"lang:{exhibit_id}"))


@router.message(CommandStart())
async def start_general(message: Message):
    """/start with no exhibit: language first, then a welcome (see welcome_after_language)."""
    await message.answer("Please choose your language:", reply_markup=build_language_keyboard("welcome"))


@router.callback_query(F.data.startswith("welcome:"))
async def welcome_after_language(callback: CallbackQuery):
    lang = callback.data.split(":")[1]
    if lang not in LANGUAGE_LABELS:
        await callback.answer()
        return
    _user_lang[callback.from_user.id] = lang
    await callback.answer()
    await callback.message.answer(WELCOME_MESSAGES[lang])


async def _find_live_exhibit(number_text: str):
    """'1', '01', ' 001 ' all resolve to exhibit '001'. Returns the row or None."""
    digits = number_text.strip()
    n = str(int(digits))
    for candidate in dict.fromkeys([n.zfill(3), digits, n]):
        exhibit = await db.get_exhibit(candidate)
        if exhibit is not None:
            return exhibit
    return None


async def _known_language(user_id: int) -> str | None:
    if user_id in _user_lang:
        return _user_lang[user_id]
    async with db.get_conn() as conn:
        cur = await conn.execute(
            "SELECT language_code FROM exhibit_views WHERE telegram_user_id = ? ORDER BY rowid DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


@router.message(F.chat.type == "private", F.text.regexp(NUMBER_RE.pattern, mode="fullmatch"))
async def typed_exhibit_number(message: Message):
    lang = await _known_language(message.from_user.id)
    exhibit = await _find_live_exhibit(message.text)
    if exhibit is None or exhibit["status"] != "live":
        text = NOT_FOUND_MESSAGES[lang] if lang in NOT_FOUND_MESSAGES else "\n\n".join(NOT_FOUND_MESSAGES.values())
        await message.answer(text)
        return
    if lang is None:
        await message.answer("Please choose your language:",
                             reply_markup=build_language_keyboard(f"lang:{exhibit['id']}"))
        return
    await _send_guide(message, message.from_user.id, exhibit, lang)


@router.callback_query(lambda c: c.data and c.data.startswith("lang:"))
async def deliver_guide(callback: CallbackQuery):
    _, exhibit_id, lang = callback.data.split(":")
    _user_lang[callback.from_user.id] = lang
    exhibit = await db.get_exhibit(exhibit_id)
    await callback.answer()
    await _send_guide(callback.message, callback.from_user.id, exhibit, lang)


async def _send_guide(target: Message, user_id: int, exhibit, lang: str):
    """Photo + translated title + cached voice note. `target` is any message to answer into."""
    if exhibit is None or exhibit["status"] != "live":
        await target.answer(NOT_READY_MESSAGES.get(lang, NOT_READY_MESSAGES["en"]))
        return
    exhibit_id = exhibit["id"]

    cached = await db.get_cached_voice(exhibit_id, lang)
    if cached is None:
        await target.answer("Sorry, this language isn't available for this exhibit yet.")
        return

    photos = await db.get_exhibit_photos(exhibit_id)
    if photos:
        caption = cached["title_translated"] or exhibit["title_am"]
        await target.answer_photo(photos[0]["telegram_file_id"], caption=caption)
    await target.answer_voice(cached["telegram_file_id"])

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT INTO exhibit_views (exhibit_id, telegram_user_id, language_code) VALUES (?, ?, ?)",
            (exhibit_id, user_id, lang),
        )
        await conn.commit()
