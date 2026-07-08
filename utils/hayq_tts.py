"""Armenian TTS via hayq.ican24.net — the same vendor already used in the
YDub and AI Caller projects, but two different endpoints/voice catalogs were
used across those two projects. Both are wired up here so they can be
compared side by side before picking one for the Armenian narration in
`worker/pipeline.py`'s `_synthesize_local_am`.

- `apittshy.php` — confirmed working in ai-caller/tts.py and the YDub pipeline.
  Voices: biverman, biverman1, biverwoman, abook2.
- `apitts.php` — used by the older zargtv wrapper class
  (video_generation_service/.../HayqIcan24TtsAPI.py). Voices: arthur.saribekyan,
  arax.poghosyan. Untested in this project — comparison will tell us if it's
  actually usable or was superseded for a reason.

Both endpoints return raw bytes with some non-audio bytes prepended before the
actual RIFF/WAV payload, so both need the same RIFF-marker extraction.
"""
import hashlib
import hmac
from pathlib import Path

import requests

PARTNER_ID = "goris"
SIGN_KEY = "19457199"

APITTSHY_VOICES = ["biverman", "biverman1", "biverwoman", "abook2"]
APITTS_VOICES = ["arthur.saribekyan", "arax.poghosyan"]


def _sign(text: str) -> str:
    return hmac.new(SIGN_KEY.encode(), text.encode("utf-8"), hashlib.md5).hexdigest()


def _extract_wav(content: bytes) -> bytes:
    riff = content.find(b"RIFF")
    if riff < 0:
        raise RuntimeError(f"TTS API did not return a WAV payload: {content[:200]!r}")
    return content[riff:]


def synthesize_apittshy(text: str, voice: str, dst_path: Path) -> Path:
    if voice not in APITTSHY_VOICES:
        raise ValueError(f"unknown apittshy voice {voice!r}, expected one of {APITTSHY_VOICES}")
    resp = requests.post(
        "https://hayq.ican24.net/apittshy.php",
        data={
            "partnerid": PARTNER_ID,
            "sign": _sign(text),
            "dataset": voice,
            "text": text,
            "train": "2",
            "vocoder": "1",
        },
        verify=False,
        timeout=30,
    )
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(_extract_wav(resp.content))
    return dst_path


def synthesize_apitts(text: str, voice: str, dst_path: Path) -> Path:
    if voice not in APITTS_VOICES:
        raise ValueError(f"unknown apitts voice {voice!r}, expected one of {APITTS_VOICES}")
    resp = requests.post(
        "https://hayq.ican24.net/apitts.php",
        data={
            "partnerid": PARTNER_ID,
            "sign": _sign(text),
            "dataset": voice,
            "text": text,
        },
        verify=False,
        timeout=30,
    )
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(_extract_wav(resp.content))
    return dst_path
