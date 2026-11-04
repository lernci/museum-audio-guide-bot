"""Tag helpers: Armenian-only entry -> one API call translates the label into the other 9
languages (tag_translations). A translation failure NEVER blocks creating/renaming a tag:
the Armenian name is stored, the tag simply shows as "untranslated" (visitors see the
Armenian name) until a retry succeeds. Same OpenAI client/model as script generation; this
module only borrows the client getter and doesn't touch pipeline internals."""
import asyncio
import json
import logging
import os

from db import db
from worker import pipeline
from worker.prompts import LANGUAGES, TAG_TRANSLATE_SYSTEM_PROMPT, build_tag_translate_tool

log = logging.getLogger(__name__)

TRANSLATION_TIMEOUT_SECONDS = 30
DEFAULT_TAGS_SOFT_LIMIT = 12

TAGS_ADVISORY = ("Խորհուրդ ենք տալիս հատկանիշները պահել քիչ ու ընդհանրական (մինչև ~12), որ այցելուի "
                 "նավիգացիան պարզ մնա։ Ստուգեք՝ արդյոք գոյություն ունեցողներից մեկը չի՞ համապատասխանում։")


def tags_soft_limit() -> int:
    try:
        return int(os.environ.get("TAGS_SOFT_LIMIT", DEFAULT_TAGS_SOFT_LIMIT))
    except ValueError:
        return DEFAULT_TAGS_SOFT_LIMIT


async def over_soft_limit() -> bool:
    return await db.count_tags() > tags_soft_limit()


async def _translate(name_hy: str, codes: list) -> dict:
    tool = build_tag_translate_tool(codes)
    client = pipeline._get_openai_client()
    response = await client.chat.completions.create(
        model=pipeline.OPENAI_SCRIPTS_MODEL,
        messages=[
            {"role": "system", "content": TAG_TRANSLATE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Armenian label: {name_hy}\n"
                                        f"Target languages (code: name): "
                                        + ", ".join(f"{c}: {LANGUAGES[c]}" for c in codes)},
        ],
        tools=[{"type": "function", "function": {
            "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}],
        tool_choice={"type": "function", "function": {"name": tool["name"]}},
    )
    calls = response.choices[0].message.tool_calls
    if not calls:
        raise RuntimeError("missing tool call")
    return json.loads(calls[0].function.arguments)["translations"]


async def translate_tag(tag_id) -> bool:
    """Translate one tag into the other 9 languages and store them. Never raises: returns
    True on success, False (tag left untranslated, to be retried later) on any failure."""
    try:
        tag = await db.get_tag(tag_id)
        if tag is None:
            return False
        result = await asyncio.wait_for(_translate(tag["name_hy"], db.TRANSLATION_LANGS),
                                        timeout=TRANSLATION_TIMEOUT_SECONDS)
        names = {c: (result.get(c) or "").strip() for c in db.TRANSLATION_LANGS}
        if not all(names.values()):
            raise RuntimeError(f"incomplete translation: missing {[c for c, v in names.items() if not v]}")
        await db.save_tag_translations(tag_id, names)
        return True
    except Exception as exc:  # noqa: BLE001 — by design never blocks the caller
        log.warning("tag %s translation failed: %s", tag_id, exc)
        return False


async def translate_tags(tag_ids) -> dict:
    """Translate several tags concurrently -> {tag_id: ok}."""
    tag_ids = list(tag_ids)
    results = await asyncio.gather(*(translate_tag(t) for t in tag_ids))
    return dict(zip(tag_ids, results))


async def translate_missing() -> dict:
    return await translate_tags(await db.list_untranslated_tag_ids())
