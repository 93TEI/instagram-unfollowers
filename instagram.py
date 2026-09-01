import json
import logging
import os
import random
import time

from instagrapi import Client
from instagrapi.extractors import extract_user_short
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    TwoFactorRequired,
)

from state import (
    FEEDBACK_COOLDOWN_SEC,
    FETCH_COOLDOWN_SEC,
    FOLLOWERS_PARTIAL_COOLDOWN_SEC,
    THROTTLE_COOLDOWN_SEC,
    app_state,
    cooldown_remaining,
    finish_job,
    set_cooldown,
    try_start_job,
)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
DEVICE_FILE = os.path.join(BASE_DIR, "device.json")
FOLLOWERS_PROGRESS_FILE = os.path.join(BASE_DIR, "followers_progress.json")

# 요청 예산 관련 상수.
# 예전에는 팔로잉 목록 + friendships/show_many/ 배치 조회로 요청 수를 줄였으나,
# 2026-08-12 인스타그램이 show_many 응답에서 followed_by 필드 자체를 없애버려 무력화됐다
# (요청은 200 OK로 오지만 맞팔 여부를 판정할 수 없음). 대안으로 followers/ 전체 페이징을
# 다시 쓴다 — 과거 소프트 차단 사고의 진짜 원인은 페이징 자체가 아니라 커서 반복 버그였고,
# 그 버그는 아래 3중 중단 조건으로 이미 고쳐져 있다.
#
# 2026-08-13 실전 실행에서 드러난 문제: following/은 count=200 요청 시 실제로 200명씩
# 돌아오지만, followers/는 같은 count=200을 요청해도 서버가 페이지당 9~23명만 돌려준다
# (계정마다 다를 수 있음, 관찰됨). 그래서 following용 _page_budget() 공식(약 100~200명/페이지
# 가정)을 followers에 그대로 쓰면 팔로워가 많은 계정에서 실제로 다 모으기 전에 예산을 소진해
# "누락된 팔로워"가 "비팔로워"로 오분류된다 — 예산을 단순히 늘리면 HANDOFF.md에 기록된
# "followers/는 세션당 시간당 약 30페이지 한계" 참고치를 넘어 소프트 차단 위험이 다시 커진다.
# 그래서 followers만 "이어받기"로 처리한다: 한 번의 실행은 FOLLOWERS_PAGE_BUDGET_PER_RUN
# 페이지만 가져오고, 커서가 안 끝났으면 진행상황을 FOLLOWERS_PROGRESS_FILE에 저장한 뒤
# fetch_non_followers()가 "완료되지 않음"을 명시적으로 반환한다. 완전히 다 모으기 전까지는
# 절대 비교 결과(non_followers)를 계산하지 않는다 — 부분 목록으로 비교하면 실제 맞팔인
# 사람이 비팔로워로 잘못 나온다.
DELAY_RANGE = [2, 4]                  # 모든 private request 뒤 자동 지연 (instagrapi가 적용)
PAGE_SIZE = 200                       # following/에서는 실제로 페이지당 최대치에 가깝게 돌아온다.
MAX_PAGES_HARD = 60                   # 커서가 끝나지 않아도 여기서 멈춘다 (폭주 방지 최후 방어선)
FOLLOWERS_PAGE_BUDGET_PER_RUN = 30    # followers/ 세션당 시간당 한계로 알려진 값. 웨이브당 상한.
MAX_FOLLOWERS_PROGRESS_AGE_SEC = 24 * 3600  # 이어받기 누적 상한 — 아래 docstring 참고
FOLLOWERS_INTER_WAVE_DELAY = (10, 20)  # 웨이브(30페이지) 사이 대기 — 사용자가 재클릭 안 해도
                                        # fetch_non_followers()가 이 간격으로 자동으로 다음
                                        # 웨이브를 이어서 돈다. 페이지 간 지연(2~5초)은 안 바꿨다 —
                                        # 사용자가 명시적으로 "실제 요청 속도는 그대로, 수동
                                        # 재클릭만 없애달라"고 확인한 뒤 넣은 값이다. 완전히
                                        # 끝날 때까지 도는 것 자체가 예전의 "회당 30페이지"
                                        # 안전장치보다 한 세션에 더 많은 요청을 보낸다는
                                        # 뜻이므로, MAX_FOLLOWERS_WAVES_PER_CALL로 최후 상한을 둔다.
MAX_FOLLOWERS_WAVES_PER_CALL = 10      # 한 번의 fetch 호출에서 자동으로 도는 최대 웨이브 수
                                        # (30 * 10 = 최대 300페이지). 이것도 못 끝내면 partial 반환.
MAX_WRITE_PER_RUN = 50      # 1회 실행에서 언팔/차단할 수 있는 최대 인원
WRITE_COOLDOWN_TRIGGER = 10 # 이 인원 이상 쓰기 작업을 하면 쿨다운을 건다
WRITE_COOLDOWN_SEC = 3600   # 읽기/쓰기를 같은 세션에서 연달아 하지 않기 위한 간격


def _init_client() -> Client:
    """Create Client with persistent device fingerprint and global request delay."""
    client = Client()
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
            # set_settings가 delay_range를 건드리지는 않지만,
            # 세션 복원 뒤에도 지연이 반드시 살아있어야 하므로 명시적으로 재설정한다.
            cl.delay_range = DELAY_RANGE
            app_state["logged_in"] = True
            app_state["username"] = cl.username
            app_state["user_id"] = str(cl.user_id)
            logger.info("session_loaded username=%s verified=false", cl.username)
            return True
        return False
    except Exception as e:
        logger.warning("session_load_failed error=%s", e)
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
        logger.exception("login_failed type=%s", type(e).__name__)
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
        cl.private_request("accounts/two_factor_login/", data, login=True)
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
        logger.exception("two_factor_verify_failed")
        return {"status": "error", "message": f"{e}"}


def _classify_error(e: Exception, operation: str) -> dict:
    """
    예외를 사용자 응답 + 쿨다운 정책으로 변환한다. fetch/unfollow/block이 공유하는 단일 지점.

    핵심: 스로틀링(PleaseWaitFewMinutes, 429, feedback_required)은 세션 만료가 아니다.
    여기서 session.json을 지우면 사용자가 재로그인을 반복하게 되고, 반복 로그인이
    제한을 더 키운다. 세션은 유지하고 쿨다운만 건다.
    """
    if isinstance(e, LoginRequired):
        app_state["logged_in"] = False
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        logger.warning("session_expired operation=%s", operation)
        return {"status": "login_required", "message": "세션이 만료되었습니다. 다시 로그인해주세요."}

    if isinstance(e, FeedbackRequired) or "feedback_required" in str(e):
        set_cooldown(FEEDBACK_COOLDOWN_SEC, "feedback_required")
        logger.warning("feedback_required operation=%s cooldown_hours=72", operation)
        return {
            "status": "cooldown",
            "message": "Instagram이 이 동작을 제한했습니다. 72시간 정도 쉬는 것을 권장합니다.",
            "cooldown_remaining": cooldown_remaining(),
        }

    if isinstance(e, (PleaseWaitFewMinutes, ClientThrottledError, RateLimitError)):
        set_cooldown(THROTTLE_COOLDOWN_SEC, "throttled")
        logger.warning("throttled operation=%s type=%s cooldown_hours=24", operation, type(e).__name__)
        return {
            "status": "cooldown",
            "message": "Instagram이 요청 속도를 제한했습니다. 24시간 정도 쉬는 것을 권장합니다.",
            "cooldown_remaining": cooldown_remaining(),
        }

    if isinstance(e, ChallengeRequired):
        logger.warning("challenge_required operation=%s", operation)
        return {
            "status": "error",
            "message": "Instagram 보안 인증이 필요합니다. 공식 앱에서 확인 후 다시 시도하세요.",
        }

    logger.exception("operation_failed operation=%s type=%s", operation, type(e).__name__)
    return {"status": "error", "message": f"[{type(e).__name__}] {e}"}


def _require_logged_in():
    """로그인 게이트. fetch/write 경로가 공유하는 단일 지점. 통과하면 None."""
    if app_state["logged_in"]:
        return None
    return {"status": "login_required", "message": "로그인이 필요합니다."}


def _check_private_api():
    """
    Private API(v1) 세션이 살아있는지 확인. 실패하면 예외를 그대로 올린다.

    자동 relogin을 하지 않는다: 제한이 걸린 상태에서 재로그인하면
    (제한 → 실패 → 재로그인 → 더 강한 제한) 악순환이 된다.
    """
    cl.private_request("accounts/current_user/?edit=true")
    logger.info("private_api_check ok=true")


def _page_budget(expected_total: int) -> int:
    """예상 인원 대비 허용 페이지 수. 커서가 같은 구간을 반복해도 무한히 돌지 않게 한다."""
    if not expected_total:
        return MAX_PAGES_HARD
    return min(MAX_PAGES_HARD, max(10, (expected_total // 100 + 1) * 2))


def _fetch_relationship_list(
    user_id, relation: str, expected_total: int = 0, max_pages: int = 0, start_max_id: str = "",
    progress_offset: int = 0,
) -> tuple:
    """
    v1 pagination으로 following/followers 수집. relation: "following" 또는 "followers".
    start_max_id를 주면 그 커서부터 이어서 수집한다(followers의 이어받기용).
    progress_offset은 화면 진행률 표시에만 쓴다 — 이전 실행에서 이미 모아둔 인원 수를
    더해줘야, 이어받는 중인데도 진행률이 0부터 다시 시작하는 것처럼 보이지 않는다
    (실제 이어받기 자체는 start_max_id가 담당하고 이 값과 무관하다).
    반환: (users: {user_id(str): UserShort}, next_max_id: str, exhausted: bool)

    exhausted=True: 커서가 자연 종료됐다(더 가져올 게 없다는 인스타그램의 명시적 신호) — 이때만
    "완료"로 신뢰할 수 있다.
    exhausted=False: empty_page/no_progress/page_budget 중 하나로 멈췄다 — 셋 다 "더는 못 믿겠으니
    안전하게 멈춘다"는 방어용 중단이지 완료 신호가 아니다. empty_page/no_progress를 완료로
    취급하면, 이어받기 중 커서가 무효화돼 빈 페이지나 중복 페이지가 오는 경우를 "다 모았다"고
    착각해 실제로는 못 가져온 사람들을 비팔로워로 오분류하는 사고가 재발한다.
    next_max_id로 이어받을 수 있다(다음 실행에서 start_max_id로 넘긴다).

    무한 루프 방어가 이 함수의 핵심이다. 과거 followers/ 전체 페이징에서 팔로워 1,000명짜리
    계정을 134페이지 긁은 사고가 있었다 — 커서가 끝나지 않으면 같은 구간을 계속 다시 받는다.
    라이브러리의 user_following_v1_chunk 도 같은 구조라 라이브러리를 써도 피할 수 없다.
    following과 followers 양쪽이 이 가드를 공유해야 한다 — 한쪽만 고치면 다른 쪽에서 재발한다.
    """
    users = {}
    max_id = start_max_id
    pages = 0
    budget = max_pages or _page_budget(expected_total)
    exhausted = False

    while True:
        params = {
            "count": PAGE_SIZE,
            "rank_token": cl.rank_token,
            "search_surface": "follow_list_page",
            "query": "",
            "enable_groups": "true",
        }
        if max_id:
            params["max_id"] = max_id
        result = cl.private_request(f"friendships/{user_id}/{relation}/", params=params)

        raw_users = result.get("users", [])
        before = len(users)
        for raw_user in raw_users:
            user = extract_user_short(raw_user)
            users[str(user.pk)] = user
        new_count = len(users) - before

        pages += 1
        app_state["fetch_progress"]["fetched"] = progress_offset + len(users)
        max_id = result.get("next_max_id") or ""
        logger.info(
            "%s_page page=%d returned=%d new=%d total=%d has_cursor=%s",
            relation, pages, len(raw_users), new_count, len(users), bool(max_id),
        )

        if not max_id:
            exhausted = True
            break
        if not raw_users:
            # 빈 페이지 — 커서 무효화일 수 있다. "다 모았다"로 믿으면 안 되므로 exhausted=False 유지.
            logger.warning("%s_stop reason=empty_page page=%d", relation, pages)
            break
        if new_count == 0:
            # 커서는 계속 오는데 새 유저가 없다 = 같은 구간 반복. exhausted=False 유지 — 완료 신호가 아니다.
            logger.warning("%s_stop reason=no_progress page=%d total=%d", relation, pages, len(users))
            break
        if pages >= budget:
            logger.warning("%s_stop reason=page_budget pages=%d budget=%d", relation, pages, budget)
            break
        time.sleep(random.uniform(2, 5))

    logger.info("%s_fetched pages=%d users=%d exhausted=%s", relation, pages, len(users), exhausted)
    return users, max_id, exhausted


def _load_followers_progress(user_id: str) -> dict:
    """
    저장된 팔로워 이어받기 진행상황을 불러온다. 다른 계정 것이거나 너무 오래됐으면 버리고
    새로 시작한다.

    이어받기는 완료(exhausted=True)될 때까지 여러 번의 실행에 걸쳐 누적된다 — 계정 규모에
    따라 이게 몇 시간에서 며칠 걸릴 수 있다. 그 사이 실제로 새로 나를 팔로우하기 시작한
    사람이 인스타그램의 페이지 정렬상 이미 지나간 커서보다 앞쪽에 끼어들면, 완료될 때까지도
    영영 수집되지 않을 수 있다 — 그 사람이 following에도 있으면(맞팔) 비팔로워로 오분류된다.
    이걸 완전히 막을 수는 없지만(인스타그램의 정렬 방식은 확인 불가), 누적 기간에
    MAX_FOLLOWERS_PROGRESS_AGE_SEC 상한을 걸어 위험을 무한정 키우지 않는다 — 너무 오래
    끌린 진행상황은 신뢰하지 말고 처음부터 다시 모은다.
    """
    empty = {"follower_ids": [], "cursor": "", "started_at": time.time()}
    if not os.path.exists(FOLLOWERS_PROGRESS_FILE):
        return empty
    try:
        with open(FOLLOWERS_PROGRESS_FILE, "r") as f:
            data = json.load(f)
        if data.get("user_id") != user_id:
            return empty
        started_at = data.get("started_at", 0)
        age = time.time() - started_at
        if age > MAX_FOLLOWERS_PROGRESS_AGE_SEC:
            logger.warning("followers_progress_stale age_sec=%d — 처음부터 다시 모은다", age)
            return empty
        return {
            "follower_ids": data.get("follower_ids", []),
            "cursor": data.get("cursor", ""),
            "started_at": started_at,
        }
    except Exception as e:
        logger.warning("followers_progress_load_failed error=%s", e)
        return empty


def _save_followers_progress(user_id: str, follower_ids: set, cursor: str, started_at: float):
    with open(FOLLOWERS_PROGRESS_FILE, "w") as f:
        json.dump(
            {
                "user_id": user_id,
                "follower_ids": sorted(follower_ids),
                "cursor": cursor,
                "started_at": started_at,
            },
            f,
        )
    logger.info("followers_progress_saved collected=%d", len(follower_ids))


def _clear_followers_progress():
    if os.path.exists(FOLLOWERS_PROGRESS_FILE):
        os.remove(FOLLOWERS_PROGRESS_FILE)


def _fetch_all_followers(user_id: str, expected_total: int = 0, max_pages: int = 0, persist: bool = True) -> tuple:
    """
    팔로워 전체를 이어받기(resume) 방식으로 수집한다.
    반환: (follower_ids: set[str], complete: bool)

    followers/ 는 count=200을 요청해도 실제로는 페이지당 9~23명만 돌려주는 경우가 관찰됐다.
    계정 규모가 크면 한 번의 실행(세션당 페이지 예산 이내)으로 끝나지 않을 수 있다 —
    끝나지 않으면 진행 상황을 FOLLOWERS_PROGRESS_FILE에 저장하고 complete=False를 반환한다.
    호출자는 complete=False면 비교 결과를 계산하면 안 된다: 부분 목록으로 비교하면
    아직 못 가져온 진짜 팔로워가 비팔로워로 오분류된다.
    """
    progress = (
        _load_followers_progress(user_id) if persist
        else {"follower_ids": [], "cursor": "", "started_at": time.time()}
    )
    known_ids = set(progress["follower_ids"])
    budget = max_pages or FOLLOWERS_PAGE_BUDGET_PER_RUN

    new_users, next_cursor, exhausted = _fetch_relationship_list(
        user_id, "followers", expected_total=expected_total,
        max_pages=budget, start_max_id=progress["cursor"],
        progress_offset=len(known_ids),
    )
    all_ids = known_ids | set(new_users)

    if not persist:
        return all_ids, exhausted

    if exhausted:
        _clear_followers_progress()
    else:
        _save_followers_progress(user_id, all_ids, next_cursor, progress["started_at"])

    return all_ids, exhausted


def select_non_followers(following: dict, followed_by: dict) -> list:
    """
    팔로잉 중 나를 팔로우하지 않는 유저만 반환한다. I/O 없는 순수 함수.

    followed_by에 없거나 판정할 수 없는 uid는 제외한다 — 확실하지 않은 사람을
    차단 후보로 올리는 것보다 목록에서 빠지는 쪽이 안전하다.
    """
    non_followers = []
    for uid, user in following.items():
        if followed_by.get(str(uid)) is not False:
            continue
        non_followers.append({
            "user_id": str(uid),
            "username": user.username,
            "full_name": user.full_name,
            "profile_pic_url": str(user.profile_pic_url),
        })
    non_followers.sort(key=lambda x: x["username"])
    return non_followers


def fetch_non_followers(dry_run: bool = False) -> dict:
    """
    팔로잉 중 나를 팔로우하지 않는 유저 목록 반환.
    dry_run=True면 팔로잉/팔로워 각 1페이지만 호출한다(세션확인/user_info 포함 요청 약 4회).
    """
    denied = _require_logged_in()
    if denied:
        return denied

    if not try_start_job():
        # 이미 다른 fetch/unfollow/block이 돌고 있다 — 여기서 그대로 진행하면 같은 cl
        # 인스턴스를 두 스레드가 동시에 두드려서 delay_range/페이지 간 지연이 무력화된다.
        return {"status": "busy", "message": "다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요."}

    try:
        user_id = cl.user_id
        app_state["job_status"] = "fetching"
        _check_private_api()

        user_info = cl.user_info(user_id)
        following_count = user_info.following_count or 0
        follower_count = user_info.follower_count or 0
        max_pages = 1 if dry_run else 0

        app_state["fetch_progress"] = {
            "phase": "following", "fetched": 0, "total": following_count,
        }
        following, _, following_exhausted = _fetch_relationship_list(
            user_id, "following", expected_total=following_count, max_pages=max_pages
        )

        if not following_exhausted and not dry_run:
            # following은 followers와 달리 이어받기가 없다 — following/은 실측상 페이지당
            # 가득 차게 돌아와 예산 내에 끝나는 게 정상이므로, 못 끝났으면 일시적 문제로 보고
            # 재시도를 유도한다. 이 부분 목록으로 비교를 진행하면 실제 비팔로워 일부가
            # 누락될 수 있다(팔로워 오분류만큼 위험하진 않지만 결과가 불완전해진다).
            #
            # dry_run은 이 게이트를 적용하지 않는다 — dry_run은 max_pages=1로 일부러 1페이지만
            # 받아서 following/followers 두 엔드포인트가 정상 응답하는지만 확인하는 용도라,
            # "완료 전 결과 차단"을 dry_run에도 적용하면 followers/ 호출 자체를 못 해보고
            # 여기서 조기 반환돼 검증 커버리지가 줄어든다. dry_run 결과는 원래도 정확도를
            # 보장하지 않는다(HANDOFF.md, docstring).
            logger.warning(
                "fetch_incomplete following_collected=%d following_expected=%d",
                len(following), following_count,
            )
            return {
                "status": "partial",
                "message": (
                    f"팔로잉 목록을 아직 다 모으지 못했습니다 ({len(following)}/{following_count}명). "
                    "잠시 후 다시 조회해주세요."
                ),
                "following_fetched": len(following),
                "following_total": following_count,
            }

        # 팔로잉→팔로워 사이 10~20초 대기 — 이 구간에서 fetch_progress를 미리 "followers"로
        # 바꿔둔다. 대기 뒤에 바꾸면 UI가 대기 시간 내내 이미 끝난 팔로잉 단계 문구("988/988명")
        # 를 그대로 보여줘서, 실제로는 정상 대기 중인데 멈춘 것처럼 보이는 문제가 있었다.
        # fetched 초기값도 0이 아니라 이전에 이어받은 인원 수로 맞춘다 — 안 그러면 실제로는
        # 이어받는 중인데 대기 내내 "0/1466명"이 떠서 처음부터 다시 하는 것처럼 보인다.
        already_collected = len(_load_followers_progress(user_id)["follower_ids"]) if not dry_run else 0
        app_state["fetch_progress"] = {
            "phase": "followers", "fetched": already_collected, "total": follower_count,
        }
        time.sleep(random.uniform(10, 20))

        # 웨이브(회당 최대 30페이지)를 자동으로 이어서 돈다 — 사용자가 매번 재클릭할 필요
        # 없이 한 번의 호출 안에서 완료까지 밀어붙인다. 웨이브 사이 지연은 유지한다
        # (FOLLOWERS_INTER_WAVE_DELAY 주석 참고 — 명시적으로 확인받은 트레이드오프).
        follower_ids, followers_complete = set(), False
        for wave in range(1, MAX_FOLLOWERS_WAVES_PER_CALL + 1):
            follower_ids, followers_complete = _fetch_all_followers(
                user_id, expected_total=follower_count, max_pages=max_pages, persist=not dry_run
            )
            if followers_complete or dry_run or wave == MAX_FOLLOWERS_WAVES_PER_CALL:
                break
            logger.info(
                "followers_wave_incomplete wave=%d/%d collected=%d expected=%d",
                wave, MAX_FOLLOWERS_WAVES_PER_CALL, len(follower_ids), follower_count,
            )
            time.sleep(random.uniform(*FOLLOWERS_INTER_WAVE_DELAY))

        if not followers_complete and not dry_run:
            logger.warning(
                "fetch_incomplete followers_collected=%d followers_expected=%d",
                len(follower_ids), follower_count,
            )
            set_cooldown(FOLLOWERS_PARTIAL_COOLDOWN_SEC, "followers_partial")
            return {
                "status": "partial",
                "message": (
                    f"팔로워 목록을 아직 다 모으지 못했습니다 ({len(follower_ids)}/{follower_count}명). "
                    f"{MAX_FOLLOWERS_WAVES_PER_CALL}회 자동 재시도 안에 다 못 모았습니다. "
                    "진행 상황은 저장했으니 잠시 후 다시 조회하면 이어서 모읍니다."
                ),
                "followers_fetched": len(follower_ids),
                "followers_total": follower_count,
            }

        followed_by = {uid: uid in follower_ids for uid in following}

        non_followers = select_non_followers(following, followed_by)
        app_state["non_followers"] = non_followers

        if not dry_run:
            set_cooldown(FETCH_COOLDOWN_SEC, "fetch_completed")

        logger.info(
            "fetch_completed dry_run=%s following=%d followers=%d non_followers=%d",
            dry_run, len(following), len(follower_ids), len(non_followers),
        )
        return {"status": "ok", "count": len(non_followers), "users": non_followers}

    except Exception as e:
        return _classify_error(e, "fetch")

    finally:
        app_state["job_status"] = "idle"
        app_state["fetch_progress"] = {"phase": None, "fetched": 0, "total": 0}
        finish_job()


def _run_write_action(user_ids: list, action, operation: str) -> dict:
    """
    언팔/차단 공통 실행부. 쓰기 작업은 읽기보다 위험하므로 인원 상한과 지연을 강제한다.

    건별 2~5초, 10건마다 추가 30~60초. 1회 실행 상한 50명(초과분은 다음 실행으로).
    """
    denied = _require_logged_in()
    if denied:
        return denied

    if not try_start_job():
        return {"status": "busy", "message": "다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요."}

    targets = user_ids[:MAX_WRITE_PER_RUN]
    skipped = len(user_ids) - len(targets)

    try:
        _check_private_api()
        app_state["job_status"] = operation
        app_state["block_progress"] = {
            "total": len(targets), "blocked": 0, "current_username": None,
        }

        success = 0
        failed = 0
        for idx, uid in enumerate(targets, start=1):
            try:
                action(int(uid))
                success += 1
            except (
                PleaseWaitFewMinutes, ClientThrottledError, RateLimitError,
                FeedbackRequired, LoginRequired, ChallengeRequired,
            ) as e:
                # 스로틀/세션 신호가 오면 남은 인원을 계속 밀어붙이지 않고 즉시 중단한다.
                # LoginRequired가 아래 일반 except로 새면 남은 49명을 헛돌며 전부 실패로 센다.
                logger.warning(
                    "write_aborted operation=%s type=%s done=%d",
                    operation, type(e).__name__, success,
                )
                result = _classify_error(e, operation)
                result["success"] = success
                result["failed"] = failed
                return result
            except Exception as e:
                logger.warning("write_item_failed operation=%s user_id=%s error=%s", operation, uid, e)
                failed += 1

            app_state["block_progress"]["blocked"] = success
            if idx == len(targets):
                break
            time.sleep(random.uniform(2, 5))
            if idx % 10 == 0:
                time.sleep(random.uniform(30, 60))

        if success >= WRITE_COOLDOWN_TRIGGER:
            set_cooldown(WRITE_COOLDOWN_SEC, "bulk_write_completed")

        logger.info(
            "write_completed operation=%s success=%d failed=%d skipped=%d",
            operation, success, failed, skipped,
        )
        return {"status": "ok", "success": success, "failed": failed, "skipped": skipped}

    except Exception as e:
        return _classify_error(e, operation)

    finally:
        app_state["job_status"] = "idle"
        finish_job()


def bulk_unfollow(user_ids: list) -> dict:
    """주어진 user_id 목록을 일괄 언팔로우 (1회 최대 50명)."""
    return _run_write_action(user_ids, cl.user_unfollow, "unfollowing")


def bulk_block(user_ids: list) -> dict:
    """주어진 user_id 목록을 일괄 차단 (1회 최대 50명)."""
    return _run_write_action(user_ids, cl.user_block, "blocking")


def logout():
    """Remove session file and reset state. 쿨다운은 계정에 걸린 것이므로 유지한다."""
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
    logger.info("logged_out")
