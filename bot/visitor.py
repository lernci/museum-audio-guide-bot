"""Visitor-side: deep link -> language picker -> instant cached voice + photo."""
import re

from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, MenuButtonCommands, ReplyKeyboardMarkup)

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

READ_TEXT_LABELS = {
    "am": "📖 Կարդալ տեքստը", "en": "📖 Read text", "ru": "📖 Читать текст", "fr": "📖 Lire le texte",
    "es": "📖 Leer el texto", "de": "📖 Text lesen", "fa": "📖 خواندن متن", "zh": "📖 阅读文字",
    "it": "📖 Leggi il testo", "el": "📖 Ανάγνωση κειμένου",
}

LIST_LABELS = {
    "am": "🗂 Ցուցանմուշների ցանկ", "en": "🗂 Exhibit list", "ru": "🗂 Список экспонатов", "fr": "🗂 Liste des pièces",
    "es": "🗂 Lista de piezas", "de": "🗂 Liste der Exponate", "fa": "🗂 فهرست نمایه‌ها", "zh": "🗂 展品列表",
    "it": "🗂 Elenco dei reperti", "el": "🗂 Λίστα εκθεμάτων",
}

HINT_MESSAGES = {
    "am": "Մուտքագրեք ցուցանմուշի համարը (օրինակ՝ 001) կամ օգտվեք ցանկի կոճակից։",
    "en": "Type an exhibit number (e.g. 001) or use the list button.",
    "ru": "Введите номер экспоната (например, 001) или воспользуйтесь кнопкой списка.",
    "fr": "Saisissez un numéro de pièce (par ex. 001) ou utilisez le bouton de la liste.",
    "es": "Escriba el número de una pieza (por ejemplo, 001) o use el botón de la lista.",
    "de": "Geben Sie eine Exponatnummer ein (z. B. 001) oder nutzen Sie die Listen-Schaltfläche.",
    "fa": "شماره یک نمایه را وارد کنید (مثلاً 001) یا از دکمه فهرست استفاده کنید.",
    "zh": "请输入展品编号（例如 001），或使用列表按钮。",
    "it": "Digita il numero di un reperto (ad es. 001) oppure usa il pulsante dell'elenco.",
    "el": "Πληκτρολογήστε τον αριθμό ενός εκθέματος (π.χ. 001) ή χρησιμοποιήστε το κουμπί της λίστας.",
}

BACK_LABELS = {
    "am": "🔙 Հետ", "en": "🔙 Back", "ru": "🔙 Назад", "fr": "🔙 Retour", "es": "🔙 Volver",
    "de": "🔙 Zurück", "fa": "🔙 بازگشت", "zh": "🔙 返回", "it": "🔙 Indietro", "el": "🔙 Πίσω",
}

ALL_LABELS = {
    "am": "Բոլորը", "en": "All", "ru": "Все", "fr": "Tous", "es": "Todos",
    "de": "Alle", "fa": "همه", "zh": "全部", "it": "Tutti", "el": "Όλα",
}

# Persistent reply-keyboard buttons: native word, plus " / English" for every language but English
# (so a visitor who picked the wrong language can still find them).
_BROWSE_WORD = {"am": "Ցանկ", "ru": "Список", "fr": "Liste", "es": "Lista", "de": "Liste",
                "fa": "فهرست", "zh": "列表", "it": "Elenco", "el": "Λίστα"}
_LANGUAGE_WORD = {"am": "Լեզու", "ru": "Язык", "fr": "Langue", "es": "Idioma", "de": "Sprache",
                  "fa": "زبان", "zh": "语言", "it": "Lingua", "el": "Γλώσσα"}
MENU_BROWSE_LABELS = {c: (f"🗂 {_BROWSE_WORD[c]} / Browse" if c in _BROWSE_WORD else "🗂 Browse") for c in LANGUAGE_LABELS}
MENU_LANGUAGE_LABELS = {c: (f"🌐 {_LANGUAGE_WORD[c]} / Language" if c in _LANGUAGE_WORD else "🌐 Language") for c in LANGUAGE_LABELS}
_BROWSE_LABEL_TO_LANG = {v: k for k, v in MENU_BROWSE_LABELS.items()}
_LANGUAGE_LABELS_SET = set(MENU_LANGUAGE_LABELS.values())

PAGE_SIZE = 8
TELEGRAM_TEXT_LIMIT = 4096

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
    await callback.message.answer(WELCOME_MESSAGES[lang], reply_markup=reply_menu(lang))


def _list_button(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=LIST_LABELS[lang], callback_data=f"list:{lang}")
    ]])


def reply_menu(lang: str) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard: visitors never need slash commands."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BROWSE_LABELS[lang]), KeyboardButton(text=MENU_LANGUAGE_LABELS[lang])]],
        resize_keyboard=True, is_persistent=True,
    )


async def setup_bot_menu(bot) -> None:
    """Registers /start so Telegram's Menu button appears (called once at startup)."""
    await bot.set_my_commands([BotCommand(command="start", description="Start · Սկսել · Старт")])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def _browse_keyboard(items, all_count: int, tags, lang: str, tag_id: int, page: int) -> InlineKeyboardMarkup:
    """One message, three levels of buttons: tag filters (All + tags with >=1 live exhibit, with
    counts, in the visitor's language), the current exhibit page, and navigation/back."""
    rows = []
    if tags:
        chips = [InlineKeyboardButton(text=("✅ " if not tag_id else "") + f"{ALL_LABELS[lang]} ({all_count})",
                                      callback_data=f"br:{lang}:0:0")]
        chips += [InlineKeyboardButton(text=("✅ " if t["id"] == tag_id else "") + f"{t['label']} ({t['live_count']})"[:60],
                                       callback_data=f"br:{lang}:{t['id']}:0") for t in tags]
        rows += [chips[i:i + 2] for i in range(0, len(chips), 2)]
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows += [
        [InlineKeyboardButton(text=f"{row['id']} — {row['title']}"[:64], callback_data=f"ex:{row['id']}:{lang}")]
        for row in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"br:{lang}:{tag_id}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"br:{lang}:{tag_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    # Back: from a tag view -> the All list; from the All list -> the welcome screen.
    rows.append([InlineKeyboardButton(text=BACK_LABELS[lang],
                                      callback_data=f"br:{lang}:0:0" if tag_id else f"bk:{lang}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _browse_view(lang: str, tag_id: int, page: int):
    """-> (header_text, keyboard) or None when nothing is live at all."""
    all_items = await db.list_live_exhibits_for_lang(lang)
    if not all_items:
        return None
    tags = await db.list_live_tags_for_lang(lang)
    if tag_id and tag_id not in {t["id"] for t in tags}:
        tag_id = 0                                        # tag vanished / has no live exhibits
    items = await db.list_live_exhibits_for_lang(lang, tag_id) if tag_id else all_items
    header = LIST_LABELS[lang]
    if tag_id:
        header += " · " + next(t["label"] for t in tags if t["id"] == tag_id)
    return header, _browse_keyboard(items, len(all_items), tags, lang, tag_id, page)


async def _open_browse(message: Message, user_id: int, lang: str):
    """Entry point ('🗂 Browse'): lands directly on the All list as a new message."""
    _user_lang[user_id] = lang
    view = await _browse_view(lang, 0, 0)
    if view is None:
        await message.answer(NOT_READY_MESSAGES[lang])
        return
    await message.answer(view[0], reply_markup=view[1])


@router.callback_query(F.data.startswith("list:"))
async def show_exhibit_list(callback: CallbackQuery):
    lang = callback.data.split(":")[1]
    await callback.answer()
    if lang in LANGUAGE_LABELS:
        await _open_browse(callback.message, callback.from_user.id, lang)


@router.callback_query(F.data.startswith("br:"))
async def browse_navigate(callback: CallbackQuery):
    """Tag filter / pagination / back-to-All: edits the list message in place."""
    try:
        _, lang, tag_s, page_s = callback.data.split(":")
        tag_id, page = int(tag_s), int(page_s)
    except ValueError:
        await callback.answer()
        return
    if lang not in LANGUAGE_LABELS:
        await callback.answer()
        return
    _user_lang[callback.from_user.id] = lang
    await callback.answer()
    view = await _browse_view(lang, tag_id, page)
    if view is None:
        await callback.message.answer(NOT_READY_MESSAGES[lang])
        return
    try:
        await callback.message.edit_text(view[0], reply_markup=view[1])
    except TelegramBadRequest:
        pass                                              # "message is not modified" (same view tapped again)


@router.callback_query(F.data.startswith("bk:"))
async def back_to_welcome(callback: CallbackQuery):
    lang = callback.data.split(":")[1]
    await callback.answer()
    if lang not in LANGUAGE_LABELS:
        return
    try:
        await callback.message.edit_text(WELCOME_MESSAGES[lang], reply_markup=_list_button(lang))
    except TelegramBadRequest:
        pass


@router.message(F.chat.type == "private", F.text.in_(set(MENU_BROWSE_LABELS.values())))
async def menu_browse(message: Message):
    lang = _BROWSE_LABEL_TO_LANG[message.text]
    await _open_browse(message, message.from_user.id, lang)


@router.message(F.chat.type == "private", F.text.in_(_LANGUAGE_LABELS_SET))
async def menu_language(message: Message):
    await message.answer("Please choose your language:", reply_markup=build_language_keyboard("welcome"))


@router.callback_query(F.data == "noop")
async def _noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("ex:"))
async def deliver_from_list(callback: CallbackQuery):
    _, exhibit_id, lang = callback.data.split(":")
    _user_lang[callback.from_user.id] = lang
    await callback.answer()
    await _send_guide(callback.message, callback.from_user.id, await db.get_exhibit(exhibit_id), lang)


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split on paragraph, then line, then sentence/space boundaries so no chunk exceeds
    `limit`; hard-cuts only a single unbroken run longer than the limit."""
    text = text.strip()
    chunks = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "), window.rfind("։ "), window.rfind(" "))
        if cut < limit // 2:
            cut = limit  # no sensible boundary in the second half of the window
        else:
            cut += 1
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


@router.callback_query(F.data.startswith("txt:"))
async def send_script_text(callback: CallbackQuery):
    _, exhibit_id, lang = callback.data.split(":")
    await callback.answer()
    exhibit = await db.get_exhibit(exhibit_id)
    if exhibit is None or exhibit["status"] != "live":
        await callback.message.answer(NOT_READY_MESSAGES.get(lang, NOT_READY_MESSAGES["en"]))
        return
    cached = await db.get_cached_voice(exhibit_id, lang)
    if cached is None or not cached["script_text"]:
        await callback.message.answer(NOT_READY_MESSAGES.get(lang, NOT_READY_MESSAGES["en"]))
        return
    for chunk in split_text(cached["script_text"]):
        await callback.message.answer(chunk)


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
    await target.answer_voice(
        cached["telegram_file_id"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=READ_TEXT_LABELS[lang], callback_data=f"txt:{exhibit_id}:{lang}")
        ]]),
    )

    async with db.get_conn() as conn:
        await conn.execute(
            "INSERT INTO exhibit_views (exhibit_id, telegram_user_id, language_code) VALUES (?, ?, ?)",
            (exhibit_id, user_id, lang),
        )
        await conn.commit()


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def non_numeric_text(message: Message):
    """Registered after typed_exhibit_number, so only non-numeric text lands here."""
    lang = await _known_language(message.from_user.id)
    if lang not in LANGUAGE_LABELS:
        await message.answer("Please choose your language:", reply_markup=build_language_keyboard("welcome"))
        return
    await message.answer(HINT_MESSAGES[lang], reply_markup=_list_button(lang))
