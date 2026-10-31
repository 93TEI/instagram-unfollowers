import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from instagram import fetch_non_followers as ig_fetch_non_followers
from instagram import login as ig_login
from instagram import logout as ig_logout
from instagram import refresh_json_export, get_export_status
from instagram import try_resume_session, verify_2fa as ig_verify_2fa
from state import app_state, try_start_job, finish_job
import instagram
from file_import import UploadLimitMiddleware, import_files

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Try to resume existing session on startup."""
    logger.info("Attempting session resume...")
    resumed = try_resume_session()
    if resumed:
        logger.info("Session resumed successfully for @%s", app_state["username"])
    else:
        logger.info("No valid session found, login required")
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(UploadLimitMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/api/health")
async def api_health():
    return {"app": "instagram-unfollowers", "status": "ok", "pid": os.getpid()}


class LoginRequest(BaseModel):
    username: str
    password: str


class VerifyRequest(BaseModel):
    code: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/login")
async def api_login(req: LoginRequest):
    return await run_auth(ig_login, req.username, req.password)


@app.post("/api/verify")
async def api_verify(req: VerifyRequest):
    return await run_auth(ig_verify_2fa, req.code)


async def run_auth(action, *args):
    def run():
        if not try_start_job():
            return {"status": "busy", "message": "다른 작업이 진행 중입니다."}
        try:
            return action(*args)
        finally:
            finish_job()
    return await asyncio.to_thread(run)


@app.get("/api/status")
async def api_status():
    return {
        "logged_in": app_state["logged_in"],
        "username": app_state["username"],
        "job_status": app_state["job_status"],
        "error": app_state["error"],
        "export": get_export_status(),
    }


@app.get("/api/progress")
async def api_progress():
    return {
        "job_status": app_state["job_status"],
        "export": get_export_status(),
    }


@app.post("/api/fetch")
async def api_fetch(dry_run: bool = False):
    """Compare saved JSON only. No requests to Instagram."""
    result = await asyncio.to_thread(ig_fetch_non_followers, dry_run)
    return result


@app.post("/api/export/refresh")
async def api_refresh_export():
    return await asyncio.to_thread(refresh_json_export)


@app.post("/api/logout")
async def api_logout():
    def run():
        ig_logout()
        return {"status": "ok"}
    return await run_auth(run)


@app.post("/api/import")
async def api_import(request: Request):
    if not app_state["logged_in"]:
        return {"status": "login_required", "message": "로그인이 필요합니다."}
    return await import_files(request, instagram._store(), app_state["username"])
