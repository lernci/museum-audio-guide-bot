"""Session-cookie login for the web admin panel. Completely separate from
goris.am's own accounts — a single admin identity, credentials in this
project's own .env, nothing shared with the Payload CMS project.
"""
import os

import bcrypt
from starlette.requests import Request
from starlette.responses import RedirectResponse

SESSION_KEY = "webapp_user"


def verify_login(username: str, password: str) -> bool:
    expected_user = os.environ["WEBAPP_ADMIN_USER"]
    expected_hash = os.environ["WEBAPP_ADMIN_PASSWORD_HASH"]
    if username != expected_user:
        return False
    return bcrypt.checkpw(password.encode(), expected_hash.encode())


def is_logged_in(request: Request) -> bool:
    return request.session.get(SESSION_KEY) == os.environ["WEBAPP_ADMIN_USER"]


def log_in(request: Request, username: str) -> None:
    request.session[SESSION_KEY] = username


def log_out(request: Request) -> None:
    request.session.clear()


def require_login(request: Request) -> RedirectResponse | None:
    """Call at the top of every protected route. Returns a redirect if the
    caller should stop, or None if the request may proceed."""
    if not is_logged_in(request):
        return RedirectResponse(url=request.url_for("login"), status_code=303)
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
