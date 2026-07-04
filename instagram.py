import logging
import os
import random
import time

from instagrapi import Client
from instagrapi.extractors import extract_user_short
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

from state import app_state

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")


def _init_client() -> Client:
    """Create Client with persistent device fingerprint."""
    import json
    client = Client()

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
    import json
    settings = client.get_settings()
    device_data = {
        "uuids": settings.get("uuids", {}),
        "device_settings": settings.get("device_settings", {}),
        "user_agent": settings.get("user_agent", ""),
    }
    with open(DEVICE_FILE, "w") as f:
        json.dump(device_data, f)


cl = _init_client()

# Store 2FA context for verify_2fa flow
_two_factor_info: dict = {}


def try_resume_session() -> bool:
    """Load session.json without probing Instagram API. Validity is checked on first real call."""
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        session = cl.load_settings(SESSION_FILE)
        if session:
            cl.set_settings(session)
            app_state["logged_in"] = True
            app_state["username"] = cl.username
            app_state["user_id"] = str(cl.user_id)
            logger.info("Session loaded for @%s (not yet verified)", cl.username)
            return True
        return False
    except Exception as e:
        logger.warning("Session load failed: %s", e)
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        return False


def login(username: str, password: str, verification_code: str = None) -> dict:
    """
    Attempt login. Reuses the module-level Client to keep device fingerprint consistent.
    """
    global _two_factor_info

    try:
        cl.login(username, password, verification_code=verification_code or "")
        cl.dump_settings(SESSION_FILE)
        app_state["logged_in"] = True
        app_state["username"] = username
        app_state["user_id"] = str(cl.user_id)
        app_state["error"] = None
        return {"status": "ok"}

    except TwoFactorRequired:
        two_factor_info = cl.last_json.get("two_factor_info", {})
        logger.info("2FA required for @%s, info: %s", username, two_factor_info)
        _two_factor_info["username"] = username
        _two_factor_info["password"] = password
        _two_factor_info["two_factor_identifier"] = two_factor_info.get("two_factor_identifier")
        return {"status": "2fa_required"}

    except ChallengeRequired:
        logger.warning("Challenge required for @%s", username)
        return {
            "status": "challenge_required",
            "message": "Instagram 보안 인증이 필요합니다. 앱에서 확인 후 다시 시도하세요.",
        }

    except BadPassword as e:
        return {"status": "error", "message": f"로그인 실패: {e.message}"}

    except Exception as e:
        logger.exception("Login failed: %s", type(e).__name__)
        return {"status": "error", "message": f"[{type(e).__name__}] {e}"}


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
        logged = cl.private_request("accounts/two_factor_login/", data, login=True)
        cl.authorization_data = cl.parse_authorization(
            cl.last_response.headers.get("ig-set-authorization")
        )
        cl.login_flow()
        cl.dump_settings(SESSION_FILE)
        _save_device_from(cl)

        app_state["logged_in"] = True
        app_state["username"] = username
        app_state["user_id"] = str(cl.user_id)
        app_state["error"] = None
        _two_factor_info.clear()
        return {"status": "ok"}

    except Exception as e:
        logger.exception("2FA verification failed")
        return {"status": "error", "message": f"{e}"}


def _ensure_private_api():
    """Private API(v1) 호출이 가능한지 확인. 401이면 재로그인하여 세션 갱신."""
    try:
        cl.private_request("accounts/current_user/?edit=true")
        logger.info("Private API session valid")
    except Exception as e:
        logger.warning("Private API check failed (%s), attempting relogin", e)
        try:
            cl.relogin()
            cl.dump_settings(SESSION_FILE)
            logger.info("Relogin successful, private API restored")
        except Exception as re_err:
            logger.warning("Relogin failed (%s), will fall back to GQL", re_err)


def _fetch_all_following(user_id) -> dict:
    """v1 pagination으로 팔로잉 전체 수집, 페이지마다 진행률 업데이트."""
    users = {}
    max_id = ""
    while True:
        params = {
            "count": 200,
            "rank_token": cl.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        }
        if max_id:
            params["max_id"] = max_id
        result = cl.private_request(
            f"friendships/{user_id}/following/", params=params
        )
        for raw_user in result.get("users", []):
            user = extract_user_short(raw_user)
            users[user.pk] = user
        app_state["fetch_progress"]["fetched"] = len(users)
        max_id = result.get("next_max_id")
        if not max_id:
            break
        time.sleep(random.uniform(0.5, 1.5))
    return users


def _fetch_all_followers(user_id) -> dict:
    """v1 pagination으로 팔로워 전체 수집, 페이지마다 진행률 업데이트."""
    users = {}
    max_id = ""
    while True:
        params = {
            "count": 200,
            "rank_token": cl.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        }
        if max_id:
            params["max_id"] = max_id
        result = cl.private_request(
            f"friendships/{user_id}/followers/", params=params
        )
        for raw_user in result.get("users", []):
            user = extract_user_short(raw_user)
            users[user.pk] = user
        app_state["fetch_progress"]["fetched"] = len(users)
        max_id = result.get("next_max_id")
        if not max_id:
            break
        time.sleep(random.uniform(0.5, 1.5))
    return users


def fetch_non_followers() -> dict:
    """팔로잉 중 나를 팔로우하지 않는 유저 목록 반환."""
    if not app_state["logged_in"]:
        return {"status": "login_required", "message": "로그인이 필요합니다."}
    try:
        user_id = cl.user_id
        app_state["job_status"] = "fetching"

        # private API(v1) 사용 가능 여부 확인, 실패 시 재로그인
        _ensure_private_api()

        # user_info로 총 수 획득
        user_info = cl.user_info(user_id)
        following_count = user_info.following_count or 0
        follower_count = user_info.follower_count or 0

        # 팔로잉 목록 수집
        app_state["fetch_progress"] = {"phase": "following", "fetched": 0, "total": following_count}
        following = _fetch_all_following(user_id)
        time.sleep(random.uniform(1, 2))

        # 팔로워 목록 수집
        app_state["fetch_progress"] = {"phase": "followers", "fetched": 0, "total": follower_count}
        followers = _fetch_all_followers(user_id)

        # 차집합: 내가 팔로우하지만 나를 팔로우하지 않는 유저
        following_ids = set(following.keys())
        follower_ids = set(followers.keys())
        non_follower_ids = following_ids - follower_ids

        # 유저 정보 리스트 구성
        non_followers = []
        for uid in non_follower_ids:
            user = following[uid]
            non_followers.append({
                "user_id": str(uid),
                "username": user.username,
                "full_name": user.full_name,
                "profile_pic_url": str(user.profile_pic_url),
            })

        non_followers.sort(key=lambda x: x["username"])
        app_state["non_followers"] = non_followers
        app_state["job_status"] = "idle"
        app_state["fetch_progress"] = {"phase": None, "fetched": 0, "total": 0}
        return {"status": "ok", "count": len(non_followers), "users": non_followers}

    except (LoginRequired, PleaseWaitFewMinutes) as e:
        app_state["job_status"] = "idle"
        app_state["fetch_progress"] = {"phase": None, "fetched": 0, "total": 0}
        app_state["logged_in"] = False
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        logger.warning("Session invalid during fetch: %s", e)
        return {"status": "login_required", "message": "세션이 만료되었습니다. 다시 로그인해주세요."}

    except Exception as e:
        app_state["job_status"] = "idle"
        app_state["fetch_progress"] = {"phase": None, "fetched": 0, "total": 0}
        logger.exception("fetch_non_followers failed")
        return {"status": "error", "message": str(e)}


def bulk_unfollow(user_ids: list) -> dict:
    """주어진 user_id 목록을 일괄 언팔로우."""
    if not app_state["logged_in"]:
        return {"status": "login_required", "message": "로그인이 필요합니다."}
    try:
        app_state["job_status"] = "unfollowing"
        app_state["bulk_progress"] = {"done": 0, "total": len(user_ids)}
        success = 0
        failed = 0
        for uid in user_ids:
            try:
                cl.user_unfollow(str(uid))
                success += 1
            except Exception as e:
                logger.warning("Unfollow failed for %s: %s", uid, e)
                failed += 1
            app_state["bulk_progress"]["done"] = success + failed
            time.sleep(random.uniform(1, 3))
        app_state["job_status"] = "idle"
        app_state["bulk_progress"] = {"done": 0, "total": 0}
        return {"status": "ok", "success": success, "failed": failed}
    except (LoginRequired, PleaseWaitFewMinutes) as e:
        app_state["job_status"] = "idle"
        app_state["logged_in"] = False
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        return {"status": "login_required", "message": "세션이 만료되었습니다. 다시 로그인해주세요."}
    except Exception as e:
        app_state["job_status"] = "idle"
        logger.exception("bulk_unfollow failed")
        return {"status": "error", "message": str(e)}


def bulk_block(user_ids: list) -> dict:
    """주어진 user_id 목록을 일괄 차단."""
    if not app_state["logged_in"]:
        return {"status": "login_required", "message": "로그인이 필요합니다."}
    try:
        app_state["job_status"] = "blocking"
        app_state["bulk_progress"] = {"done": 0, "total": len(user_ids)}
        success = 0
        failed = 0
        for uid in user_ids:
            try:
                data = {
                    "surface": "profile",
                    "is_auto_block_enabled": "true",
                    "user_id": str(uid),
                    "_uid": cl.user_id,
                    "_uuid": cl.uuid,
                }
                result = cl.private_request(f"friendships/block/{uid}/", data)
                if result.get("friendship_status", {}).get("blocking"):
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning("Block failed for %s: %s", uid, e)
                failed += 1
            app_state["bulk_progress"]["done"] = success + failed
            time.sleep(random.uniform(1, 3))
        app_state["job_status"] = "idle"
        app_state["bulk_progress"] = {"done": 0, "total": 0}
        return {"status": "ok", "success": success, "failed": failed}
    except (LoginRequired, PleaseWaitFewMinutes) as e:
        app_state["job_status"] = "idle"
        app_state["logged_in"] = False
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        return {"status": "login_required", "message": "세션이 만료되었습니다. 다시 로그인해주세요."}
    except Exception as e:
        app_state["job_status"] = "idle"
        logger.exception("bulk_block failed")
        return {"status": "error", "message": str(e)}


def logout():
    """Remove session file and reset state."""
    global _two_factor_info
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    _two_factor_info.clear()
    app_state["logged_in"] = False
    app_state["username"] = None
    app_state["user_id"] = None
    app_state["error"] = None
    app_state["job_status"] = "idle"
    app_state["non_followers"] = []
    app_state["block_progress"] = {"total": 0, "blocked": 0, "current_username": None}
    app_state["fetch_progress"] = {"phase": None, "fetched": 0, "total": 0}
    logger.info("Logged out and session cleared")
