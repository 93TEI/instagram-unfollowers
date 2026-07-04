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
from state import app_state

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

app.mount("/static", StaticFiles(directory="static"), name="static")


class LoginRequest(BaseModel):
    username: str
    password: str


class VerifyRequest(BaseModel):
    code: str


class BulkActionRequest(BaseModel):
    user_ids: list[str]


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
    }


@app.get("/api/progress")
async def api_progress():
    return {
        "job_status": app_state["job_status"],
        "phase": app_state["fetch_progress"]["phase"],
        "fetched": app_state["fetch_progress"]["fetched"],
        "total": app_state["fetch_progress"]["total"],
    }


@app.get("/api/bulk-progress")
async def api_bulk_progress():
    return app_state["bulk_progress"]


@app.post("/api/fetch")
async def api_fetch():
    result = await asyncio.to_thread(ig_fetch_non_followers)
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
