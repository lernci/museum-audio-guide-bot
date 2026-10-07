"""Web admin panel: a staged pipeline (Source -> Scripts -> Audio -> Publish)
for adding/editing exhibits and listening to generated voice notes. Runs
inside the same asyncio event loop as the Telegram bot (see main.py) so it
shares one `bot` instance, one `db` module, and the same in-process
generation queue (worker/pipeline.py) — no second process, no IPC.

Generation (Stage 2/3) always writes to audio_cache's draft_* columns,
never the published columns bot/visitor.py reads — only Publish
(pipeline.publish_draft) copies draft -> published. That's what makes an
already-live exhibit keep serving its old content untouched while staff
edit and regenerate it.
"""
import os
from pathlib import Path
from urllib.parse import quote

from aiogram.types import BufferedInputFile
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.visitor import LANGUAGE_LABELS
from db import db
from utils.fact_sheet import extract_fact_sheet_text
from utils.qr import build_deep_link, generate_qr_png_bytes
from worker import pipeline
from webapp.auth import is_logged_in, log_in, log_out, require_login, verify_login

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

MAX_PHOTOS = 3


def create_app(bot) -> FastAPI:
    app = FastAPI(root_path=os.environ.get("WEBAPP_ROOT_PATH", ""))
    app.state.bot = bot
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ["WEBAPP_SESSION_SECRET"],
        session_cookie="museumbot_session",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.globals["LANGUAGE_LABELS"] = LANGUAGE_LABELS

    # ── auth ─────────────────────────────────────────────────────────────
    @app.get("/login", name="login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if is_logged_in(request):
            return RedirectResponse(request.url_for("dashboard"), status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", name="login_submit")
    async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        if not verify_login(username, password):
            return templates.TemplateResponse(
                request, "login.html", {"error": "Wrong username or password."}, status_code=401
            )
        log_in(request, username)
        return RedirectResponse(request.url_for("dashboard"), status_code=303)

    @app.post("/logout", name="logout")
    async def logout(request: Request):
        log_out(request)
        return RedirectResponse(request.url_for("login"), status_code=303)

    # ── dashboard ────────────────────────────────────────────────────────
    @app.get("/", name="dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibits = await db.list_exhibits()
        return templates.TemplateResponse(request, "dashboard.html", {"exhibits": exhibits})

    # ── new exhibit (Stage 1 only — Scripts/Audio/Publish happen on the
    #    detail page once the exhibit exists) ───────────────────────────
    @app.get("/exhibits/new", name="exhibit_new_form", response_class=HTMLResponse)
    async def exhibit_new_form(request: Request):
        if (redirect := require_login(request)) is not None:
            return redirect
        return templates.TemplateResponse(request, "exhibit_new.html", {"error": None})

    @app.post("/exhibits/new", name="exhibit_new_submit")
    async def exhibit_new_submit(
        request: Request,
        exhibit_id: str = Form(...),
        title_am: str = Form(...),
        fact_sheet_text: str = Form(""),
        fact_sheet_file: UploadFile | None = None,
        photos: list[UploadFile] = None,
    ):
        if (redirect := require_login(request)) is not None:
            return redirect

        exhibit_id = exhibit_id.strip()
        if await db.get_exhibit(exhibit_id) is not None:
            return templates.TemplateResponse(
                request, "exhibit_new.html", {"error": f"Exhibit {exhibit_id} already exists."}, status_code=400
            )

        fact_sheet_am = await _resolve_fact_sheet(fact_sheet_text, fact_sheet_file)
        if not fact_sheet_am:
            return templates.TemplateResponse(
                request, "exhibit_new.html",
                {"error": "Provide the fact sheet as text or attach a .txt/.docx file."}, status_code=400,
            )
        photo_files = [p for p in (photos or []) if p.filename]
        if not photo_files:
            return templates.TemplateResponse(
                request, "exhibit_new.html", {"error": "At least one photo is required."}, status_code=400
            )

        file_ids = await _upload_photos(request.app.state.bot, photo_files[:MAX_PHOTOS])
        await db.create_exhibit(exhibit_id, title_am.strip(), fact_sheet_am, created_by=None)
        await db.replace_exhibit_photos(exhibit_id, file_ids)
        await db.set_deep_link(exhibit_id, build_deep_link(pipeline.BOT_USERNAME, exhibit_id))
        await db.mark_source_edited(exhibit_id)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── exhibit detail (the staged pipeline page) ────────────────────────
    @app.get("/exhibits/{exhibit_id}", name="exhibit_detail", response_class=HTMLResponse)
    async def exhibit_detail(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is None:
            return HTMLResponse("Exhibit not found.", status_code=404)
        photos = await db.get_exhibit_photos(exhibit_id)
        audio_rows = await db.get_all_audio_rows(exhibit_id)
        publish_problems = pipeline.publish_readiness(audio_rows)
        return templates.TemplateResponse(
            request, "exhibit_detail.html",
            {
                "exhibit": exhibit, "photos": photos, "audio_rows": audio_rows,
                "publish_problems": publish_problems,
                "generate_error": request.query_params.get("generate_error"),
            },
        )

    # ── Stage 1 — Source ─────────────────────────────────────────────────
    @app.post("/exhibits/{exhibit_id}/source", name="exhibit_save_source")
    async def exhibit_save_source(
        request: Request,
        exhibit_id: str,
        title_am: str = Form(...),
        fact_sheet_text: str = Form(""),
        fact_sheet_file: UploadFile | None = None,
        photos: list[UploadFile] = None,
    ):
        if (redirect := require_login(request)) is not None:
            return redirect
        if await db.get_exhibit(exhibit_id) is None:
            return HTMLResponse("Exhibit not found.", status_code=404)

        fact_sheet_am = await _resolve_fact_sheet(fact_sheet_text, fact_sheet_file)
        await db.update_exhibit_content(exhibit_id, title_am=title_am.strip() or None, fact_sheet_am=fact_sheet_am)

        photo_files = [p for p in (photos or []) if p.filename]
        if photo_files:
            file_ids = await _upload_photos(request.app.state.bot, photo_files[:MAX_PHOTOS])
            await db.replace_exhibit_photos(exhibit_id, file_ids)

        # Any Stage 1 change invalidates every language's draft script+audio —
        # the core staleness rule (see db.mark_source_edited).
        await db.mark_source_edited(exhibit_id)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── Stage 2 — Scripts ────────────────────────────────────────────────
    @app.post("/exhibits/{exhibit_id}/generate-scripts", name="exhibit_generate_scripts")
    async def exhibit_generate_scripts(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        if await db.get_exhibit(exhibit_id) is None:
            return HTMLResponse("Exhibit not found.", status_code=404)
        try:
            await pipeline.generate_scripts_draft(exhibit_id)
        except Exception as exc:  # noqa: BLE001 — surfaced on the detail page
            return RedirectResponse(
                str(request.url_for("exhibit_detail", exhibit_id=exhibit_id)) + f"?generate_error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    @app.post("/exhibits/{exhibit_id}/scripts/{lang}", name="exhibit_save_script")
    async def exhibit_save_script(
        request: Request, exhibit_id: str, lang: str,
        title_translated: str = Form(""), script_text: str = Form(...),
    ):
        if (redirect := require_login(request)) is not None:
            return redirect
        await db.save_draft_script_manual(exhibit_id, lang, title_translated.strip() or None, script_text.strip())
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── Stage 3 — Audio ──────────────────────────────────────────────────
    @app.post("/exhibits/{exhibit_id}/generate-audio", name="exhibit_generate_audio")
    async def exhibit_generate_audio(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        rows = await db.get_all_audio_rows(exhibit_id)
        # Only languages with a current (non-stale) script are eligible —
        # TTS-ing a script that's about to be regenerated anyway would just
        # be wasted work the next Stage 2 run immediately invalidates.
        langs = [
            code for code, row in rows.items()
            if row["draft_script_text"] and not row["script_stale"]
            and (row["audio_stale"] or row["draft_status"] != "ready")
        ]
        if langs:
            await pipeline.generate_audio_draft(request.app.state.bot, exhibit_id, langs)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── Stage 4 — Publish / Unpublish ────────────────────────────────────
    @app.post("/exhibits/{exhibit_id}/publish", name="exhibit_publish")
    async def exhibit_publish(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        if await db.get_exhibit(exhibit_id) is None:
            return HTMLResponse("Exhibit not found.", status_code=404)
        await pipeline.publish_draft(exhibit_id)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    @app.post("/exhibits/{exhibit_id}/unpublish", name="exhibit_unpublish")
    async def exhibit_unpublish(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is not None and exhibit["status"] == "live":
            await db.set_exhibit_status(exhibit_id, "unpublished")
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── media proxies — stream straight from Telegram/in-memory, nothing
    #    duplicated on our own disk; all support Range so <audio>/<img>
    #    elements on the same page can't desync (see commit notes). ─────
    @app.get("/media/photo/{exhibit_id}/{index}", name="media_photo")
    async def media_photo(request: Request, exhibit_id: str, index: int):
        if (redirect := require_login(request)) is not None:
            return redirect
        photos = await db.get_exhibit_photos(exhibit_id)
        if index < 0 or index >= len(photos):
            return HTMLResponse("Not found.", status_code=404)
        data = await _download_telegram_file(request.app.state.bot, photos[index]["telegram_file_id"])
        return _ranged_response(request, data, "image/jpeg")

    @app.get("/media/qr/{exhibit_id}", name="media_qr")
    async def media_qr(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is None or not exhibit["deep_link"]:
            return HTMLResponse("Not found.", status_code=404)
        # Generated on demand from the deep link — deterministic, so there's
        # nothing to persist and nothing that can go missing after a redeploy.
        data = generate_qr_png_bytes(exhibit["deep_link"])
        return _ranged_response(request, data, "image/png")

    @app.get("/media/audio/{exhibit_id}/{lang}", name="media_audio")
    async def media_audio(request: Request, exhibit_id: str, lang: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        row = await db.get_audio_row(exhibit_id, lang)
        # Always the draft — that's the copy staff are actively reviewing
        # before Publish; it's byte-identical to the published copy right
        # after a Publish anyway.
        if row is None or not row["draft_telegram_file_id"]:
            return HTMLResponse("Not found.", status_code=404)
        data = await _download_telegram_file(request.app.state.bot, row["draft_telegram_file_id"])
        return _ranged_response(request, data, "audio/ogg")

    return app


def _ranged_response(request: Request, data: bytes, media_type: str) -> Response:
    """Honors a Range header if present (206 + Content-Range), and always
    advertises Accept-Ranges. Browsers issue Range probes to determine an
    <audio> element's duration/seekability; a server that silently ignores
    Range while still answering 200 is exactly what caused several
    same-page <audio> elements to show 0:00 and occasionally play each
    other's content — some browsers' media stack gets confused about which
    response belongs to which request once Range framing doesn't match
    what they asked for. Serving a real 206 removes the ambiguity."""
    total = len(data)
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            start_s, end_s = range_header.removeprefix("bytes=").split("-", 1)
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
        except ValueError:
            start, end = 0, total - 1
        if 0 <= start <= end < total:
            chunk = data[start:end + 1]
            return Response(
                content=chunk, status_code=206, media_type=media_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(chunk)),
                },
            )
    return Response(content=data, media_type=media_type, headers={"Accept-Ranges": "bytes"})


async def _resolve_fact_sheet(plain_text: str, upload: UploadFile | None) -> str | None:
    if upload is not None and upload.filename:
        content = await upload.read()
        return extract_fact_sheet_text(None, upload.filename, content)
    return extract_fact_sheet_text(plain_text)


async def _upload_photos(bot, photo_files: list[UploadFile]) -> list[str]:
    """Uploads each photo to the private log channel to get a telegram_file_id
    — the same storage mechanism the Telegram admin FSM already uses, so
    photos added from either side look identical in the DB."""
    file_ids = []
    for photo in photo_files:
        content = await photo.read()
        input_file = BufferedInputFile(content, filename=photo.filename or "photo.jpg")
        sent = await bot.send_photo(pipeline.LOG_CHANNEL_ID, photo=input_file)
        file_ids.append(sent.photo[-1].file_id)
    return file_ids


async def _download_telegram_file(bot, file_id: str) -> bytes:
    import io

    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    return buf.getvalue()
