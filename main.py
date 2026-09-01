import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from instagram import fetch_non_followers as ig_fetch_non_followers
from instagram import login as ig_login
from instagram import logout as ig_logout
from instagram import bulk_unfollow as ig_bulk_unfollow
from instagram import bulk_block as ig_bulk_block
from instagram import try_resume_session, verify_2fa as ig_verify_2fa
from state import app_state, cooldown_remaining, load_cooldown

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Try to resume existing session on startup."""
    # 쿨다운을 먼저 복원한다. 서버 재시작으로 요청 예산 제한이 초기화되면 안 된다.
    load_cooldown()
    logger.info("Attempting session resume...")
    resumed = try_resume_session()
    if resumed:
        logger.info("Session resumed successfully for @%s", app_state["username"])
    else:
        logger.info("No valid session found, login required")
    yield


app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


class LoginRequest(BaseModel):
    username: str
    password: str


class VerifyRequest(BaseModel):
    code: str


class BulkActionRequest(BaseModel):
    user_ids: list


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/login")
async def api_login(req: LoginRequest):
    result = ig_login(req.username, req.password)
    return result


@app.post("/api/verify")
async def api_verify(req: VerifyRequest):
    result = ig_verify_2fa(req.code)
    return result


@app.get("/api/status")
async def api_status():
    return {
        "logged_in": app_state["logged_in"],
        "username": app_state["username"],
        "job_status": app_state["job_status"],
        "error": app_state["error"],
        "cooldown_remaining": cooldown_remaining(),
        "cooldown_reason": app_state["cooldown_reason"],
    }


@app.get("/api/progress")
async def api_progress():
    return {
        "job_status": app_state["job_status"],
        "phase": app_state["fetch_progress"]["phase"],
        "fetched": app_state["fetch_progress"]["fetched"],
        "total": app_state["fetch_progress"]["total"],
    }


@app.post("/api/fetch")
async def api_fetch(dry_run: bool = False):
    """dry_run=true면 팔로잉/팔로워 각 1페이지만 호출한다 (세션확인/user_info 포함 요청 약 4회)."""
    result = await asyncio.to_thread(ig_fetch_non_followers, dry_run)
    return result


@app.post("/api/bulk-unfollow")
async def api_bulk_unfollow(req: BulkActionRequest):
    result = await asyncio.to_thread(ig_bulk_unfollow, req.user_ids)
    return result


@app.post("/api/bulk-block")
async def api_bulk_block(req: BulkActionRequest):
    result = await asyncio.to_thread(ig_bulk_block, req.user_ids)
    return result


@app.post("/api/logout")
async def api_logout():
    ig_logout()
    return {"status": "ok"}
