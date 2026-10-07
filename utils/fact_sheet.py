"""Shared fact-sheet parsing: plain text, or an attached .txt/.docx file's
bytes. Used by both the Telegram admin FSM (bot/admin.py) and the web admin
upload form (webapp/app.py) so the two stay in sync.
"""
import io


def extract_fact_sheet_text(
    plain_text: str | None, filename: str | None = None, file_bytes: bytes | None = None
) -> str | None:
    if plain_text:
        return plain_text.strip()
    if file_bytes is not None and filename:
        name = filename.lower()
        if name.endswith(".docx"):
            from docx import Document as DocxDocument
            doc = DocxDocument(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if name.endswith(".txt"):
            return file_bytes.decode("utf-8").strip()
    return None
