"""Existing Instagram authentication plus local JSON comparison/export orchestration."""

import json
import logging
import os

from instagrapi import Client
from instagrapi.exceptions import BadPassword, ChallengeRequired, TwoFactorRequired

from export_browser import BrowserExporter
from export_data import ExportError, ExportStore, atomic_json
from export_jobs import refresh as refresh_export, status as export_status
from state import app_state, finish_job, try_start_job
import data_library

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")
DELAY_RANGE = [2, 4]

def _init_client() -> Client:
    """Create Client with persistent device fingerprint and global request delay."""
    client = Client(session_retry_total=0)
    # instagrapi가 모든 private request 뒤에 이 범위의 랜덤 지연을 자동 적용한다.
    client.delay_range = DELAY_RANGE

    if os.path.exists(DEVICE_FILE):
        with open(DEVICE_FILE, "r") as f:
            device = json.load(f)
        client.set_settings(device)
    else:
        # First run: save device settings immediately so they persist across restarts
        _save_device_from(client)

    return client


def _save_device_from(client: Client):
    """Save device fingerprint for reuse across restarts."""
    settings = client.get_settings()
    device_data = {
        "uuids": settings.get("uuids", {}),
        "device_settings": settings.get("device_settings", {}),
        "user_agent": settings.get("user_agent", ""),
    }
    with open(DEVICE_FILE, "w") as f:
        json.dump(device_data, f)


cl = None


def _client():
    global cl
    if cl is None:
        cl = _init_client()
    return cl


def _save_session(username):
    settings = cl.get_settings()
    settings["app_username"] = username
    atomic_json(SESSION_FILE, settings)

# Store 2FA context for verify_2fa flow
_two_factor_info: dict = {}


def try_resume_session() -> bool:
    """Load session.json without probing Instagram API. Validity is checked on first real call."""
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        _client()
        session = cl.load_settings(SESSION_FILE)
        if session:
            cl.set_settings(session)
            # set_settings가 delay_range를 건드리지는 않지만,
            # 세션 복원 뒤에도 지연이 반드시 살아있어야 하므로 명시적으로 재설정한다.
            cl.delay_range = DELAY_RANGE
            app_state["logged_in"] = True
            app_state["username"] = session.get("app_username") or cl.username or ""
            app_state["user_id"] = str(cl.user_id)
            logger.info("session_loaded username=%s verified=false", cl.username)
            return True
        return False
    except Exception as e:
        logger.warning("session_load_failed error_type=%s", type(e).__name__)
        return False


def login(username: str, password: str, verification_code: str = None) -> dict:
    """
    Attempt login. Reuses the module-level Client to keep device fingerprint consistent.
    """
    global _two_factor_info

    try:
        _client()
        cl.login(username, password, verification_code=verification_code or "")
        _save_session(username)
        app_state["logged_in"] = True
        app_state["username"] = username
        app_state["user_id"] = str(cl.user_id)
        app_state["error"] = None
        return {"status": "ok"}

    except TwoFactorRequired:
        two_factor_info = cl.last_json.get("two_factor_info", {})
        logger.info("two_factor_required username=%s", username)
        _two_factor_info["username"] = username
        _two_factor_info["password"] = password
        _two_factor_info["two_factor_identifier"] = two_factor_info.get("two_factor_identifier")
        return {"status": "2fa_required"}

    except ChallengeRequired:
        logger.warning("challenge_required username=%s", username)
        return {
            "status": "challenge_required",
            "message": "Instagram 보안 인증이 필요합니다. 앱에서 확인 후 다시 시도하세요.",
        }

    except BadPassword as e:
        return {"status": "error", "message": f"로그인 실패: {e.message}"}

    except Exception as e:
        logger.warning("login_failed type=%s", type(e).__name__)
        return {"status": "error", "message": "로그인하지 못했습니다. 입력 정보와 공식 앱의 인증 상태를 확인해주세요."}


def verify_2fa(code: str) -> dict:
    """Submit 2FA code directly to two_factor_login endpoint."""
    from uuid import uuid4

    two_factor_identifier = _two_factor_info.get("two_factor_identifier")
    username = _two_factor_info.get("username")

    if not two_factor_identifier or not username:
        return {"status": "error", "message": "2FA 세션이 만료되었습니다. 다시 로그인하세요."}

    try:
        data = {
            "verification_code": code,
            "phone_id": cl.phone_id,
            "_csrftoken": cl.token,
            "two_factor_identifier": two_factor_identifier,
            "username": username,
            "trust_this_device": "1",
            "guid": cl.uuid,
            "device_id": cl.android_device_id,
            "waterfall_id": str(uuid4()),
            "verification_method": "3",
        }
        cl.private_request("accounts/two_factor_login/", data, login=True)
        cl.authorization_data = cl.parse_authorization(
            cl.last_response.headers.get("ig-set-authorization")
        )
        cl.login_flow()
        _save_session(username)
        _save_device_from(cl)

        app_state["logged_in"] = True
        app_state["username"] = username
        app_state["user_id"] = str(cl.user_id)
        app_state["error"] = None
        _two_factor_info.clear()
        return {"status": "ok"}

    except Exception as e:
        logger.warning("two_factor_verify_failed error_type=%s", type(e).__name__)
        return {"status": "error", "message": "인증하지 못했습니다. 인증 코드와 공식 앱의 상태를 확인해주세요."}



def _store():
    return ExportStore(app_state["user_id"])


def fetch_non_followers(dry_run=False):
    """Read saved files only, including when dry_run is passed by older callers."""
    if not try_start_job():
        return {"status": "busy", "message": "다른 작업이 진행 중입니다."}
    try:
        app_state["job_status"] = "comparing"
        result = data_library.selected_store().compare()
        app_state["non_followers"] = result.get("users", [])
        return result
    except (ExportError, OSError) as exc:
        app_state["non_followers"] = []
        message = str(exc) if isinstance(exc, ExportError) else "저장된 JSON 파일을 읽을 수 없습니다."
        return {"status": "error", "message": message}
    finally:
        app_state["job_status"] = "idle"
        finish_job()


def refresh_json_export():
    if not app_state["logged_in"]:
        return {"status": "login_required", "message": "로그인이 필요합니다."}
    if not try_start_job():
        return {"status": "busy", "message": "다른 작업이 진행 중입니다."}
    try:
        app_state["job_status"] = "exporting"
        if not app_state["username"]:
            return {"status": "error", "message": "저장된 세션에 아이디가 없습니다. 앱에서 다시 로그인해주세요."}
        client = _client()
        provider = BrowserExporter(client.sessionid, app_state["username"], getattr(client, "password", None))
        result = refresh_export(_store(), app_state["username"], provider)
        if result["status"] == "ok":
            app_state["non_followers"] = result["users"]
        return result
    except (ExportError, OSError) as exc:
        message = str(exc) if isinstance(exc, ExportError) else "내보내기 기록을 읽거나 저장할 수 없습니다."
        return {"status": "error", "message": message}
    finally:
        app_state["job_status"] = "idle"
        finish_job()


def get_export_status():
    if not app_state["logged_in"]:
        return {"phase": "idle", "has_data": False}
    try:
        return export_status(_store())
    except (ExportError, OSError):
        return {"phase": "error", "has_data": False, "message": "내보내기 기록을 읽을 수 없습니다."}


def logout():
    """Clear the existing login. Account-scoped local exports remain available."""
    global cl
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    _two_factor_info.clear()
    cl = None
    app_state.update(logged_in=False, username=None, user_id=None,
                     error=None, job_status="idle", non_followers=[])
    logger.info("logged_out")
