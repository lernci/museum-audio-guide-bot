"""QR code generation for exhibit deep links."""
from pathlib import Path

import qrcode


def build_deep_link(bot_username: str, exhibit_id: str) -> str:
    return f"https://t.me/{bot_username}?start=exh_{exhibit_id}"


def generate_qr_png(deep_link: str, dst_path: Path) -> Path:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(deep_link)
    img.save(dst_path)
    return dst_path
