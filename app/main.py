import asyncio
import hmac
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import scan
from app.config import Settings
from app.petition import PetitionStore, ValidationError, validate
from app.ratelimit import RateLimiter
from app.sniff import sniff
from app.upload import UploadError, receive_file

log = logging.getLogger("banthefake")

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' blob: data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}


MAX_JSON_BODY = 16 * 1024  # petition / erase request bodies


class BodyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding the security headers to every response.

    It wraps the whole stack, outside Starlette's ServerErrorMiddleware, so even 500 responses produced
    by the catch-all exception handler carry the headers (an @app.middleware("http") would not see them).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                # Revalidate (via ETag) on every load so a deploy never leaves visitors on stale JS/CSS.
                headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class App(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersMiddleware(super().build_middleware_stack())


def error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class ScanSlots:
    """At most `n` scans at a time, process-wide; waiters give up after a timeout.

    The asyncio.Semaphore is created lazily for the running event loop (TestClient may run the app on
    more than one loop; uvicorn uses one).
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._loop is not loop:
            self._sem, self._loop = asyncio.Semaphore(self.n), loop
        return self._sem

    async def acquire(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._get().acquire(), timeout)
        except TimeoutError:
            return False
        return True

    def release(self) -> None:
        self._get().release()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    scan.configure_isolation(settings.scan_concurrency)
    store = PetitionStore(settings.db_path)
    # Petition rate limits: stored signatures count against BTF_SIGN_RATE, so a typo or a duplicate does
    # not use up a household's quota; every attempt (valid or not) counts against the higher
    # BTF_SIGN_ATTEMPT_RATE, which caps probing and junk submissions.
    sign_limiter = RateLimiter(settings.sign_rate, 3600)
    attempt_limiter = RateLimiter(settings.sign_attempt_rate, 3600)
    scan_limiter = RateLimiter(settings.scan_rate, 60)
    scan_slots = ScanSlots(settings.scan_concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scan.warmup()  # start loading the detector model in the background
        try:
            yield
        finally:
            scan.shutdown()

    app = App(title="BanTheFake", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error(exc.status_code, str(exc.detail), getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error(422, "Invalid request.")

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # Runs inside ServerErrorMiddleware, which logs the traceback; SecurityHeadersMiddleware adds headers.
        return error(500, "Internal server error.")

    def check_admin(request: Request) -> JSONResponse | None:
        if not settings.admin_token:
            return error(404, "Not Found")
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), settings.admin_token.encode()
        ):
            return error(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
        return None

    async def read_json(request: Request) -> object:
        """Parse a small JSON body. Raises BodyError (415 wrong type, 413 too large); None if not valid JSON.

        Requiring application/json also stops cross-site HTML forms (text/plain, urlencoded) from
        submitting, since browsers only send that type cross-origin after a CORS preflight.
        """
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise BodyError(415, "Content-Type must be application/json.")
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_JSON_BODY):
            raise BodyError(413, "Request body is too large.")
        body = bytearray()
        try:
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_JSON_BODY:
                    raise BodyError(413, "Request body is too large.")
        except ClientDisconnect as exc:
            raise BodyError(400, "The request was interrupted.") from exc
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None

    @app.get("/api/health")
    async def health() -> dict:
        caps = scan.capabilities()
        return {"ok": True, "model": caps["model"], "c2pa": caps["c2pa"]}

    @app.post("/api/petition", status_code=201)
    async def sign(request: Request):
        ip = client_ip(request)
        if not attempt_limiter.hit(ip) or not sign_limiter.hit(ip):
            return error(429, "Too many attempts. Please try again later.")
        try:
            payload = await read_json(request)
        except BodyError as exc:
            sign_limiter.refund(ip)
            return error(exc.status, exc.message)
        if not isinstance(payload, dict):
            sign_limiter.refund(ip)
            return error(400, "Invalid request body.")
        if payload.get("website"):
            return JSONResponse({"ok": True}, status_code=201)
        try:
            sig = validate(payload)
        except ValidationError as exc:
            sign_limiter.refund(ip)
            return error(422, str(exc))
        try:
            added = await run_in_threadpool(store.add, sig)
        except Exception:
            sign_limiter.refund(ip)
            raise
        if not added:
            sign_limiter.refund(ip)
            return error(409, "already_signed")
        return JSONResponse({"ok": True}, status_code=201)

    @app.get("/api/petition/count")
    async def count() -> dict:
        return {"count": await run_in_threadpool(store.count), "goal": settings.petition_goal}

    @app.get("/api/petition/export.csv")
    async def export(request: Request):
        if denied := check_admin(request):
            return denied
        body = await run_in_threadpool(store.export_csv)
        return Response(
            body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="signatures.csv"',
                "Cache-Control": "no-store",
            },
        )

    @app.delete("/api/petition")
    async def erase(request: Request):
        if denied := check_admin(request):
            return denied
        try:
            payload = await read_json(request)
        except BodyError as exc:
            return error(exc.status, exc.message)
        email = payload.get("email") if isinstance(payload, dict) else None
        if not isinstance(email, str) or not email.strip():
            return error(422, "email is required.")
        if not await run_in_threadpool(store.delete, email):
            return error(404, "not_found")
        return {"deleted": True}

    @app.post("/api/scan")
    async def scan_upload(request: Request):
        if not scan_limiter.hit(client_ip(request)):
            return error(429, "Too many scans. Please wait a minute and try again.")
        fd, tmp_name = tempfile.mkstemp(prefix="btf-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as dest:
                info = await receive_file(request, dest, "file", settings.max_upload_bytes)
            if info.size == 0:
                return error(400, "The file is empty.")
            with tmp_path.open("rb") as f:
                sniffed = sniff(f.read(64))
            if sniffed is None:
                return error(415, "Unsupported file type. Upload a JPG, PNG, WEBP, GIF, MP4, MOV or WEBM.")
            _, mime = sniffed
            filename = os.path.basename(info.filename.replace("\\", "/"))[:255]
            # Decoding media and running the model is memory hungry: bound how many run at once.
            if not await scan_slots.acquire(settings.scan_wait_s):
                return error(503, "busy", {"Retry-After": "30"})
            try:
                report = await run_in_threadpool(scan.scan_file, tmp_path, filename, mime)
            finally:
                scan_slots.release()
            return report
        except UploadError as exc:
            return error(exc.status, exc.message)
        except ClientDisconnect:
            # The client went away mid-upload: routine, not a server error, so no traceback.
            log.info("scan upload interrupted by client disconnect")
            return error(400, "The upload was interrupted.")
        except scan.UnsupportedMedia as exc:
            return error(415, str(exc))
        except scan.ScanTimeout:
            return error(504, "scan_timeout")
        except scan.ScanFailed:
            log.exception("isolated scan worker failed")
            return error(500, "scan_failed")
        except Exception:
            log.exception("scan failed")
            return error(500, "Scan failed.")
        finally:
            tmp_path.unlink(missing_ok=True)

    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
    return app


app = create_app()
