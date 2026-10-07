"""QR code generation for exhibit deep links."""
import io
from pathlib import Path

import qrcode


def build_deep_link(bot_username: str, exhibit_id: str) -> str:
    return f"https://t.me/{bot_username}?start=exh_{exhibit_id}"


def generate_qr_png(deep_link: str, dst_path: Path) -> Path:
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(deep_link)
    img.save(dst_path)
    return dst_path


def generate_qr_png_bytes(deep_link: str) -> bytes:
    """Same QR, generated in memory — used by the web admin's media_qr route
    so it never depends on a PNG file surviving a redeploy/container
    recreation (the deep_link -> QR mapping is deterministic, no reason to
    persist the image at all)."""
    buf = io.BytesIO()
    qrcode.make(deep_link).save(buf)
    return buf.getvalue()
