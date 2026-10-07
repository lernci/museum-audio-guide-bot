"""Web admin panel: add/edit exhibits, listen to generated voice notes,
publish/unpublish. Runs inside the same asyncio event loop as the Telegram
bot (see main.py) so it shares one `bot` instance, one `db` module, and the
same in-process generation queue (worker/pipeline.py) — no second process,
no IPC.
"""
import os
from pathlib import Path

from aiogram.types import BufferedInputFile
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.visitor import LANGUAGE_LABELS
from db import db
from utils.fact_sheet import extract_fact_sheet_text
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

    # ── new exhibit ──────────────────────────────────────────────────────
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
        existing = await db.get_exhibit(exhibit_id)
        if existing is not None:
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
        await pipeline.enqueue_exhibit(exhibit_id)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── exhibit detail / edit ────────────────────────────────────────────
    @app.get("/exhibits/{exhibit_id}", name="exhibit_detail", response_class=HTMLResponse)
    async def exhibit_detail(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is None:
            return HTMLResponse("Exhibit not found.", status_code=404)
        photos = await db.get_exhibit_photos(exhibit_id)
        audio_rows = await db.get_all_audio_rows(exhibit_id)
        return templates.TemplateResponse(
            request, "exhibit_detail.html",
            {"exhibit": exhibit, "photos": photos, "audio_rows": audio_rows},
        )

    @app.post("/exhibits/{exhibit_id}/edit", name="exhibit_edit")
    async def exhibit_edit(
        request: Request,
        exhibit_id: str,
        title_am: str = Form(""),
        fact_sheet_text: str = Form(""),
        fact_sheet_file: UploadFile | None = None,
        photos: list[UploadFile] = None,
    ):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is None:
            return HTMLResponse("Exhibit not found.", status_code=404)

        fact_sheet_am = await _resolve_fact_sheet(fact_sheet_text, fact_sheet_file)
        await db.update_exhibit_content(
            exhibit_id,
            title_am=title_am.strip() or None,
            fact_sheet_am=fact_sheet_am,
        )
        photo_files = [p for p in (photos or []) if p.filename]
        if photo_files:
            file_ids = await _upload_photos(request.app.state.bot, photo_files[:MAX_PHOTOS])
            await db.replace_exhibit_photos(exhibit_id, file_ids)

        await db.clear_exhibit_generation_state(exhibit_id)
        await db.set_exhibit_status(exhibit_id, "draft")
        await pipeline.enqueue_exhibit(exhibit_id)
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── publish / unpublish / retry ──────────────────────────────────────
    @app.post("/exhibits/{exhibit_id}/publish", name="exhibit_publish")
    async def exhibit_publish(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is not None and exhibit["status"] in ("review", "unpublished"):
            await db.set_exhibit_status(exhibit_id, "live")
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    @app.post("/exhibits/{exhibit_id}/unpublish", name="exhibit_unpublish")
    async def exhibit_unpublish(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is not None and exhibit["status"] == "live":
            await db.set_exhibit_status(exhibit_id, "unpublished")
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    @app.post("/exhibits/{exhibit_id}/retry/{lang}", name="exhibit_retry")
    async def exhibit_retry(request: Request, exhibit_id: str, lang: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        try:
            await pipeline.retry_language(request.app.state.bot, exhibit_id, lang)
        except Exception:  # noqa: BLE001 — surfaced on the detail page, never crash the request
            pass
        return RedirectResponse(request.url_for("exhibit_detail", exhibit_id=exhibit_id), status_code=303)

    # ── media proxies — stream straight from Telegram, nothing stored twice ─
    @app.get("/media/photo/{exhibit_id}/{index}", name="media_photo")
    async def media_photo(request: Request, exhibit_id: str, index: int):
        if (redirect := require_login(request)) is not None:
            return redirect
        photos = await db.get_exhibit_photos(exhibit_id)
        if index < 0 or index >= len(photos):
            return HTMLResponse("Not found.", status_code=404)
        data = await _download_telegram_file(request.app.state.bot, photos[index]["telegram_file_id"])
        return Response(content=data, media_type="image/jpeg")

    @app.get("/media/qr/{exhibit_id}", name="media_qr")
    async def media_qr(request: Request, exhibit_id: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        exhibit = await db.get_exhibit(exhibit_id)
        if exhibit is None or not exhibit["qr_code_path"]:
            return HTMLResponse("Not found.", status_code=404)
        qr_path = Path(exhibit["qr_code_path"])
        if not qr_path.is_file():
            return HTMLResponse("Not found.", status_code=404)
        return Response(content=qr_path.read_bytes(), media_type="image/png")

    @app.get("/media/audio/{exhibit_id}/{lang}", name="media_audio")
    async def media_audio(request: Request, exhibit_id: str, lang: str):
        if (redirect := require_login(request)) is not None:
            return redirect
        row = await db.get_audio_row(exhibit_id, lang)
        if row is None or not row["telegram_file_id"]:
            return HTMLResponse("Not found.", status_code=404)
        data = await _download_telegram_file(request.app.state.bot, row["telegram_file_id"])
        return Response(content=data, media_type="audio/ogg")

    return app


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
