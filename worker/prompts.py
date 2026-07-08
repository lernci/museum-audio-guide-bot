"""Claude prompt + structured-output schema for the docent-script pipeline.

One Claude call per exhibit produces narration scripts for all 10 languages
at once (Armenian included — the staff's raw fact sheet gets turned into a
proper narrative too, it just gets routed to the local TTS engine afterward
instead of OpenAI/ElevenLabs).
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

OUTPUT you produce: one narration script per requested target language.

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

7. Output nothing except the narration text for each language via the \
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
        "Submit the finished narration script for every requested language."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scripts": {
                "type": "object",
                "description": "Map of language code -> narration script text.",
                "properties": {
                    code: {"type": "string", "description": f"{name} narration script"}
                    for code, name in LANGUAGES.items()
                },
                "required": list(LANGUAGES.keys()),
            }
        },
        "required": ["scripts"],
    },
}


def call_claude_for_scripts(client, exhibit_id: str, title_am: str, fact_sheet_am: str) -> dict:
    """Returns {"am": "...", "en": "...", ...}. `client` is an anthropic.Anthropic instance."""
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[SUBMIT_SCRIPTS_TOOL],
        tool_choice={"type": "tool", "name": "submit_docent_scripts"},
        messages=[
            {
                "role": "user",
                "content": build_user_message(exhibit_id, title_am, fact_sheet_am),
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_docent_scripts":
            return block.input["scripts"]
    raise RuntimeError("Claude did not return the expected tool call")
