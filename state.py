import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOLDOWN_FILE = os.path.join(BASE_DIR, "cooldown.json")

# fetch/unfollow/block은 asyncio.to_thread로 실행되는 진짜 OS 스레드다. 같은 instagrapi
# Client(instagram.py의 cl)를 두 작업이 동시에 두드리면, 각자 독립적으로 도는 delay_range/
# 페이지 간 sleep이 서로를 모르는 채 겹쳐서 실제 인스타그램에 나가는 요청 빈도가 그대로
# 배로 늘어난다 — 이 프로젝트 전체가 막으려는 바로 그 소프트 차단 위험이다.
# app_state["job_status"]는 UI 표시용일 뿐 원자적이지 않아 동시성 가드로 못 쓴다(두 스레드가
# "idle"을 동시에 읽고 동시에 통과하는 TOCTOU 레이스가 가능) — 그래서 진짜 락을 쓴다.
_job_lock = threading.Lock()

# 권장 대기 시간. 인스타그램의 소프트 차단 신호는 실행 직후가 아니라 하루쯤 뒤에 나타나므로
# (요청 자체는 전부 200 OK로 통과함) 런타임 에러로는 감지할 수 없다.
#
# 이 값은 **경고 표시용이며 실행을 막지 않는다.** 사용자가 위험을 감수하고 언제든
# 실행할 수 있어야 한다는 결정에 따른 것이다. 실질적인 방어는 following/followers 페이징의
# 3중 중단 조건(instagram.py의 _fetch_relationship_list)과 요청 간 지연이 담당한다.
FETCH_COOLDOWN_SEC = 6 * 3600       # fetch 성공(완전 수집) 후 다음 fetch까지 권장 간격
FOLLOWERS_PARTIAL_COOLDOWN_SEC = 3600  # 팔로워 이어받기 미완료 시 — followers/ 시간당 한계 참고
THROTTLE_COOLDOWN_SEC = 24 * 3600   # PleaseWaitFewMinutes / 429
FEEDBACK_COOLDOWN_SEC = 72 * 3600   # feedback_required

app_state = {
    "logged_in": False,
    "username": None,
    "user_id": None,
    "job_status": "idle",
    "non_followers": [],
    "block_progress": {"total": 0, "blocked": 0, "current_username": None},
    "fetch_progress": {"phase": None, "fetched": 0, "total": 0},
    "error": None,
    "cooldown_until": 0.0,
    "cooldown_reason": None,
}


def _persist_cooldown():
    """쿨다운을 디스크에 저장. 서버 재시작으로 예산 제한이 무효화되면 안 된다."""
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(
            {
                "cooldown_until": app_state["cooldown_until"],
                "cooldown_reason": app_state["cooldown_reason"],
            },
            f,
        )


def load_cooldown():
    """저장된 쿨다운 복원. 앱 시작 시 1회 호출."""
    if not os.path.exists(COOLDOWN_FILE):
        return
    try:
        with open(COOLDOWN_FILE, "r") as f:
            data = json.load(f)
        app_state["cooldown_until"] = float(data.get("cooldown_until", 0))
        app_state["cooldown_reason"] = data.get("cooldown_reason")
        logger.info(
            "cooldown_loaded remaining_sec=%d reason=%s",
            cooldown_remaining(),
            app_state["cooldown_reason"],
        )
    except Exception as e:
        logger.warning("cooldown_load_failed error=%s", e)


def set_cooldown(seconds: int, reason: str):
    """권장 대기 시간을 기록한다(경고 표시용, 실행을 막지 않음). 기존 값이 더 길면 유지."""
    until = time.time() + seconds
    if until <= app_state["cooldown_until"]:
        return
    app_state["cooldown_until"] = until
    app_state["cooldown_reason"] = reason
    _persist_cooldown()
    logger.info("cooldown_set seconds=%d reason=%s", seconds, reason)


def cooldown_remaining() -> int:
    """남은 권장 대기 시간(초). 0이면 위험 없음."""
    return max(0, int(app_state["cooldown_until"] - time.time()))


def try_start_job() -> bool:
    """
    동시 실행 방지 락 획득을 시도한다. 성공(True)해야 fetch/unfollow/block을 시작할 수 있다.
    실패(False)면 이미 다른 작업이 진행 중이므로 새 작업을 시작하면 안 된다(락을 못 잡았으니
    finish_job()도 호출하면 안 된다).
    """
    return _job_lock.acquire(blocking=False)


def finish_job():
    """try_start_job()이 True를 반환했을 때만 짝을 맞춰 호출한다. 반드시 finally에서 호출."""
    _job_lock.release()
