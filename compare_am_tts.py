"""Generate the same Armenian sample narration through every known
hayq.ican24.net voice/endpoint combo, so they can be listened to side by
side and one picked for `_synthesize_local_am`.

Output: tts_samples/<endpoint>_<voice>.wav
"""
import asyncio
import warnings
from pathlib import Path

import requests
import urllib3

from utils.hayq_tts import synthesize_apittshy, synthesize_apitts, APITTSHY_VOICES, APITTS_VOICES
from utils.audio import probe_duration_seconds

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

SAMPLE_TEXT_AM = (
    "Բարի գալուստ Գորիսի երկրաբանական թանգարան։ Ձեր առջև ներկայացված է հանքային "
    "նմուշ, որը հայտնաբերվել է Սյունիքի մարզում։ Այն ձևավորվել է միլիոնավոր "
    "տարիների ընթացքում՝ հրաբխային ապարների սառեցման արդյունքում։"
)

OUT_DIR = Path("tts_samples")


async def main():
    results = []

    for voice in APITTSHY_VOICES:
        dst = OUT_DIR / f"apittshy_{voice}.wav"
        try:
            synthesize_apittshy(SAMPLE_TEXT_AM, voice, dst)
            duration = await probe_duration_seconds(dst)
            results.append(("apittshy.php", voice, "OK", dst.stat().st_size, duration))
        except Exception as exc:
            results.append(("apittshy.php", voice, f"FAILED: {exc}", None, None))

    for voice in APITTS_VOICES:
        dst = OUT_DIR / f"apitts_{voice}.wav"
        try:
            synthesize_apitts(SAMPLE_TEXT_AM, voice, dst)
            duration = await probe_duration_seconds(dst)
            results.append(("apitts.php", voice, "OK", dst.stat().st_size, duration))
        except Exception as exc:
            results.append(("apitts.php", voice, f"FAILED: {exc}", None, None))

    print(f"\n{'endpoint':<14} {'voice':<20} {'status':<40} {'bytes':>8} {'sec':>6}")
    for endpoint, voice, status, size, duration in results:
        size_s = str(size) if size is not None else "-"
        dur_s = f"{duration:.1f}" if duration is not None else "-"
        print(f"{endpoint:<14} {voice:<20} {status:<40} {size_s:>8} {dur_s:>6}")

    print(f"\nFiles saved under {OUT_DIR.resolve()}/ — play them locally to compare.")


if __name__ == "__main__":
    asyncio.run(main())
