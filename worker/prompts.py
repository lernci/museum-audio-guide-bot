"""Prompt + structured-output schema for the docent-script pipeline.

One OpenAI call per exhibit produces narration scripts for all 10 languages
at once (Armenian included — the staff's raw fact sheet gets turned into a
proper narrative too, it just gets routed to the local TTS engine afterward
instead of OpenAI/ElevenLabs). The schema below is provider-agnostic JSON
Schema; worker/pipeline.py wraps it in whichever shape the active provider's
tool-calling API expects.
"""

LANGUAGES = {
    "am": "Armenian",
    "en": "English",
    "ru": "Russian",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "fa": "Persian (Farsi)",
    "zh": "Chinese (Simplified Mandarin)",
    "it": "Italian",
    "el": "Greek",
}

SYSTEM_PROMPT = """\
You are a professional museum docent and audio-guide scriptwriter. You turn \
short, factual notes written by museum staff into natural, spoken-word \
narration scripts for a multilingual audio guide. Visitors will hear these \
scripts read aloud by text-to-speech immediately after scanning a QR code \
next to the physical exhibit.

INPUT you receive: an exhibit title and a raw fact sheet, both in Armenian, \
written by museum staff as informal notes or bullet points.

OUTPUT you produce: for each requested target language, a localized exhibit \
title and a narration script.

Hard rules:

1. FACTUAL ACCURACY IS NON-NEGOTIABLE. Only use facts present in the given \
fact sheet. Never invent dates, names, attributions, materials, prices, or \
history that were not provided. If the fact sheet is thin, write a shorter \
script rather than padding it with invented detail.

2. WRITE FOR THE EAR, NOT THE EYE. This text is spoken by TTS, never shown \
as subtitles. So:
   - No markdown, headers, bullet points, emoji, or parenthetical asides.
   - No abbreviations that sound wrong spoken aloud (write "for example" not \
"e.g.", write out units and centuries in words when it reads more naturally).
   - Spell out numbers/dates in the natural spoken form for that language \
(e.g. "in the late eighteen hundreds" rather than "in the 1800s", unless the \
target language conventionally reads digits aloud directly).
   - Use punctuation deliberately to control TTS pacing: short sentences and \
commas create natural breathing pauses; avoid long nested clauses.

3. TONE: warm, welcoming, knowledgeable museum docent speaking directly to a \
visitor standing in front of the object. Second person ("you'll notice...") \
is welcome. Avoid academic or catalog-entry phrasing.

4. LENGTH: aim for a 60–90 second spoken narration (roughly 130–210 words in \
most languages; adjust proportionally for languages that are naturally \
denser or sparser per word, e.g. Chinese).

5. LOCALIZE, DON'T JUST TRANSLATE. Adapt idioms, measurement conventions, \
date formats, and honorifics to what sounds natural to a native speaker of \
the target language, while preserving every fact exactly. Proper nouns \
(place names, personal names) should be transliterated the way that is \
standard/recognizable in the target language, not left in Armenian script, \
unless there is no standard equivalent.

6. Produce EVERY requested language, including Armenian itself — turn the \
staff's raw notes into the same polished narrative style, in Armenian.

7. TITLE: alongside the narration, give a short localized exhibit title for \
each language — the same title the staff gave, adapted the way rule 5 \
describes (transliterated proper nouns, natural phrasing), not a summary or \
a translation of the narration script.

8. Output nothing except the title and narration for each language via the \
provided tool call. No preamble, no notes to the developer, no explanations.
"""

USER_MESSAGE_TEMPLATE = """\
Exhibit ID: {exhibit_id}
Title (Armenian): {title_am}

Fact sheet (Armenian, raw staff notes):
\"\"\"
{fact_sheet_am}
\"\"\"

Target languages (code: name): {language_list}

Generate the docent narration script for each of these languages.
"""

def build_user_message(exhibit_id: str, title_am: str, fact_sheet_am: str) -> str:
    language_list = ", ".join(f"{code}: {name}" for code, name in LANGUAGES.items())
    return USER_MESSAGE_TEMPLATE.format(
        exhibit_id=exhibit_id,
        title_am=title_am,
        fact_sheet_am=fact_sheet_am,
        language_list=language_list,
    )

# Forced tool-use schema — this is what makes the response reliably parseable
# JSON instead of free text we'd have to regex out of a chat reply.
SUBMIT_SCRIPTS_TOOL = {
    "name": "submit_docent_scripts",
    "description": (
        "Submit the finished localized title and narration script for every requested language."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scripts": {
                "type": "object",
                "description": "Map of language code -> {title, narration}.",
                "properties": {
                    code: {
                        "type": "object",
                        "description": f"{name} title + narration script",
                        "properties": {
                            "title": {"type": "string", "description": f"{name} localized exhibit title"},
                            "narration": {"type": "string", "description": f"{name} narration script"},
                        },
                        "required": ["title", "narration"],
                    }
                    for code, name in LANGUAGES.items()
                },
                "required": list(LANGUAGES.keys()),
            }
        },
        "required": ["scripts"],
    },
}


# ── Translate-from-Armenian variant ────────────────────────────────────────
TRANSLATE_SYSTEM_PROMPT = """\
You are a professional translator for a museum audio guide. You receive an \
APPROVED Armenian narration script (it may have been hand-edited by the \
museum's owner) and its exhibit title, and you translate them faithfully \
into each requested target language. The translations will be read aloud by \
text-to-speech to visitors standing in front of the exhibit.

Hard rules:

1. THE ARMENIAN TEXT IS THE SOLE SOURCE OF TRUTH. This is a translation, not \
a rewrite: do not add, remove, merge, reorder, or "improve" facts, claims, \
numbers, names, or dates. Do not add background knowledge of your own. If \
the Armenian text is short or odd, the translation is equally short or odd.

2. PRESERVE TONE AND STRUCTURE. Keep the register, warmth, and sentence-by-\
sentence flow of the Armenian script (second person stays second person). \
Do not make it more formal, more academic, or more flowery.

3. KEEP IT TTS-FRIENDLY. The output is spoken, never shown: no markdown, \
headers, bullet points, emoji, or parenthetical asides; no abbreviations that \
sound wrong aloud; write numbers and dates the way that language is naturally \
spoken; keep sentences short enough to breathe.

4. NAMES: transliterate proper nouns (people, places) the way that is \
standard and recognizable in the target language, not left in Armenian \
script unless there is no standard equivalent. Adapt measurement and date \
conventions only when that does not change the fact stated.

5. TITLE: also translate the exhibit title into each target language — the \
same title, naturally phrased, not a summary of the narration.

6. Output nothing except the title and translation for each requested \
language via the provided tool call. No preamble, no notes, no explanations.
"""

TRANSLATE_USER_MESSAGE_TEMPLATE = """\
Exhibit ID: {exhibit_id}
Title (Armenian): {title_am}

Approved Armenian narration script:
\"\"\"
{script_am}
\"\"\"

Target languages (code: name): {language_list}

Translate the title and the script faithfully into each of these languages.
"""


def build_translate_user_message(exhibit_id: str, title_am: str, script_am: str, codes) -> str:
    return TRANSLATE_USER_MESSAGE_TEMPLATE.format(
        exhibit_id=exhibit_id, title_am=title_am, script_am=script_am,
        language_list=", ".join(f"{c}: {LANGUAGES[c]}" for c in codes),
    )


def build_submit_tool(codes) -> dict:
    """The same structured-output tool as SUBMIT_SCRIPTS_TOOL, restricted to `codes`."""
    import copy
    tool = copy.deepcopy(SUBMIT_SCRIPTS_TOOL)
    scripts = tool["input_schema"]["properties"]["scripts"]
    scripts["properties"] = {c: v for c, v in scripts["properties"].items() if c in codes}
    scripts["required"] = [c for c in scripts["required"] if c in codes]
    return tool


# ── Tag-label translation ──────────────────────────────────────────────────
TAG_TRANSLATE_SYSTEM_PROMPT = """\
You translate very short museum category labels (tags) from Armenian into \
other languages. The labels are shown as buttons in a visitor's phone app \
(for example "Minerals", "Tools", "Fossils"), so they must be short.

Rules:
1. Translate the label's meaning faithfully; do not add, explain, or expand it.
2. Keep it as short as the original: a short noun or noun phrase, in the most \
natural wording a native speaker would expect on a museum button. Plural vs. \
singular should follow the Armenian.
3. Capitalize the way that language normally capitalizes a button label. No \
trailing punctuation, no quotes, no emoji.
4. Output only via the provided tool call.
"""


def build_tag_translate_tool(codes) -> dict:
    return {
        "name": "submit_tag_translations",
        "description": "Submit the translated tag label for every requested language.",
        "input_schema": {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "object",
                    "properties": {c: {"type": "string", "description": f"{LANGUAGES[c]} label"} for c in codes},
                    "required": list(codes),
                }
            },
            "required": ["translations"],
        },
    }
