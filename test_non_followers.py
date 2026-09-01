"""select_non_followers() / 페이지네이션 가드 / 팔로워 이어받기 유닛 테스트.

select_non_followers는 차단/언팔 대상을 결정하는 유일한 지점, 페이지네이션 가드는
요청 폭주를 막는 지점, 팔로워 이어받기는 "부분 목록으로 비교해 실제 맞팔인 사람을
비팔로워로 오분류하는 사고"를 막는 지점이다. 전부 네트워크 없이 검증한다.
실행: ./venv/bin/pytest test_non_followers.py -v
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

import instagram
import state
from instagram import select_non_followers


def user(username):
    return SimpleNamespace(
        username=username,
        full_name=username.title(),
        profile_pic_url=f"https://example.com/{username}.jpg",
    )


def test_empty_input():
    assert select_non_followers({}, {}) == []


def test_all_mutual():
    following = {"1": user("alice"), "2": user("bob")}
    followed_by = {"1": True, "2": True}
    assert select_non_followers(following, followed_by) == []


def test_all_non_followers():
    following = {"1": user("alice"), "2": user("bob")}
    followed_by = {"1": False, "2": False}
    result = select_non_followers(following, followed_by)
    assert [u["username"] for u in result] == ["alice", "bob"]


def test_mixed_returns_only_non_followers():
    following = {"1": user("alice"), "2": user("bob"), "3": user("carol")}
    followed_by = {"1": True, "2": False, "3": True}
    result = select_non_followers(following, followed_by)
    assert [u["username"] for u in result] == ["bob"]
    assert result[0]["user_id"] == "2"


def test_result_is_sorted_by_username():
    following = {"1": user("zoe"), "2": user("adam"), "3": user("mia")}
    followed_by = {"1": False, "2": False, "3": False}
    result = select_non_followers(following, followed_by)
    assert [u["username"] for u in result] == ["adam", "mia", "zoe"]


def test_missing_status_is_excluded():
    """show_many가 값을 안 준 유저는 판정 불가 — 차단 후보에 올리지 않는다."""
    following = {"1": user("alice"), "2": user("bob")}
    followed_by = {"1": False}
    result = select_non_followers(following, followed_by)
    assert [u["username"] for u in result] == ["alice"]


def test_none_status_is_excluded():
    following = {"1": user("alice")}
    followed_by = {"1": None}
    assert select_non_followers(following, followed_by) == []


def test_int_keys_match_string_statuses():
    """following의 pk가 int로 들어와도 show_many의 문자열 키와 맞아야 한다."""
    following = {123: user("alice")}
    followed_by = {"123": False}
    result = select_non_followers(following, followed_by)
    assert [u["username"] for u in result] == ["alice"]
    assert result[0]["user_id"] == "123"


def test_output_shape():
    following = {"7": user("alice")}
    result = select_non_followers(following, {"7": False})
    assert result[0] == {
        "user_id": "7",
        "username": "alice",
        "full_name": "Alice",
        "profile_pic_url": "https://example.com/alice.jpg",
    }


# --- 페이지네이션 가드 ---------------------------------------------------------
# 구버전은 팔로워 1,000명 계정에서 followers/ 를 134페이지 긁었다. 커서가 끝나지 않으면
# 같은 구간을 계속 다시 받기 때문이다. 아래 테스트가 그 폭주를 막는 가드를 지킨다.

def raw_user(pk):
    return {
        "pk": pk,
        "username": f"user{pk}",
        "full_name": f"User {pk}",
        "profile_pic_url": f"https://example.com/{pk}.jpg",
        "is_private": False,
    }


@pytest.fixture
def no_network(monkeypatch):
    """
    private_request를 스텁으로 갈고 sleep을 없앤다. 호출 횟수를 세서 반환한다.

    반드시 monkeypatch로만 패치한다 — instagram.time 은 stdlib time 모듈 그 자체라
    직접 대입하면 프로세스 전역 time.sleep 이 망가지고 복원되지 않는다.
    """
    calls = []

    def install(pages):
        """pages: 응답 dict 리스트, 또는 호출 횟수를 받아 dict를 돌려주는 함수."""
        def fake_private_request(path, params=None, **kwargs):
            calls.append(path)
            if callable(pages):
                return pages(len(calls))
            return pages[min(len(calls) - 1, len(pages) - 1)]

        monkeypatch.setattr(instagram.cl, "private_request", fake_private_request)
        monkeypatch.setattr(instagram.time, "sleep", lambda *_: None)
        return calls

    return install


def test_stops_when_cursor_repeats_same_users(no_network):
    """새 유저가 안 늘어나는데 커서만 계속 오는 경우 — 폭주의 실제 원인."""
    same_page = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor"}
    calls = no_network([same_page])

    users, _, exhausted = instagram._fetch_relationship_list("me", "following", expected_total=1400)

    assert len(users) == 2
    assert len(calls) == 2, f"진행이 없는데 {len(calls)}번 요청했다"
    assert exhausted is False, (
        "no_progress는 안전 중단이지 완료 신호가 아니다 — 완료로 표시하면 이어받기 중 "
        "커서가 무효화돼 중복 페이지만 오는 경우를 '다 모았다'로 오판해 오분류가 재발한다"
    )


def test_stops_at_page_budget_when_cursor_never_ends(no_network):
    """매 페이지 새 유저가 오지만 커서가 끝나지 않는 경우 — 예산에서 끊어야 한다."""
    def endless_page(n):
        return {
            "users": [raw_user(n * 1000 + i) for i in range(3)],
            "next_max_id": "cursor",
        }

    calls = no_network(endless_page)
    users, next_cursor, exhausted = instagram._fetch_relationship_list("me", "following", expected_total=1400)

    budget = instagram._page_budget(1400)
    assert len(calls) == budget
    assert len(users) == budget * 3
    assert budget <= instagram.MAX_PAGES_HARD
    assert exhausted is False, "예산이 다 찼을 뿐 커서는 안 끝났으니 이어받을 수 있어야 한다"
    assert next_cursor == "cursor"


def test_stops_on_empty_page(no_network):
    """빈 페이지도 안전 중단일 뿐 완료 신호가 아니다 — 커서 무효화로도 빈 페이지가 올 수 있다."""
    calls = no_network([{"users": [], "next_max_id": "cursor"}])
    users, _, exhausted = instagram._fetch_relationship_list("me", "following", expected_total=1400)
    assert users == {}
    assert len(calls) == 1
    assert exhausted is False


def test_stops_when_cursor_absent(no_network):
    calls = no_network([{"users": [raw_user(1)], "next_max_id": None}])
    users, _, exhausted = instagram._fetch_relationship_list("me", "following", expected_total=1400)
    assert len(users) == 1
    assert len(calls) == 1
    assert exhausted is True


def test_guard_applies_to_followers_relation_too(no_network):
    """following과 followers가 같은 가드를 공유하는지 확인 — 한쪽만 고치는 회귀 방지."""
    same_page = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor"}
    calls = no_network([same_page])

    users, _, _ = instagram._fetch_relationship_list("me", "followers", expected_total=1400)

    assert len(users) == 2
    assert len(calls) == 2
    assert calls[0] == "friendships/me/followers/"




def test_page_budget_scales_with_expected_total():
    assert instagram._page_budget(1400) == 30  # (1400//100 + 1) * 2
    assert instagram._page_budget(0) == instagram.MAX_PAGES_HARD
    assert instagram._page_budget(100000) == instagram.MAX_PAGES_HARD


# --- 팔로워 이어받기 -----------------------------------------------------------
# followers/는 count=200을 요청해도 실제로는 페이지당 소량만 돌려주는 경우가 있어(관찰됨),
# 팔로워가 많은 계정은 한 번의 실행(세션당 페이지 예산 이내)으로 다 못 모을 수 있다.
# 이 상태에서 그냥 비교하면 아직 못 가져온 진짜 팔로워가 비팔로워로 오분류된다 —
# 이게 바로 이번에 고친 사고다. 아래 테스트는 "다 모으기 전에는 절대 완료 취급하지 않는다"와
# "이어받을 때 이전 진행상황 + 커서를 실제로 사용한다"를 검증한다.

@pytest.fixture
def followers_progress_file(tmp_path, monkeypatch):
    """FOLLOWERS_PROGRESS_FILE을 테스트별 임시 경로로 격리해 실제 파일을 건드리지 않는다."""
    path = str(tmp_path / "followers_progress.json")
    monkeypatch.setattr(instagram, "FOLLOWERS_PROGRESS_FILE", path)
    return path


@pytest.fixture
def stub_private_request(monkeypatch):
    """private_request를 스텁으로 갈고, 매 호출의 (path, params)를 기록한다."""
    def install(responder):
        """responder(path, params) -> dict 응답을 돌려주는 콜러블."""
        requests = []

        def fake_private_request(path, params=None, **kwargs):
            params = dict(params or {})
            requests.append((path, params))
            return responder(path, params)

        monkeypatch.setattr(instagram.cl, "private_request", fake_private_request)
        monkeypatch.setattr(instagram.time, "sleep", lambda *_: None)
        return requests

    return install


def test_fetch_all_followers_incomplete_run_persists_progress(followers_progress_file, stub_private_request):
    """예산 안에 안 끝나면 complete=False를 반환하고 진행상황을 저장해야 한다."""
    page = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor-A"}
    stub_private_request(lambda path, params: page)

    ids, complete = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)

    assert complete is False
    assert ids == {"1", "2"}
    assert os.path.exists(followers_progress_file)


def test_fetch_all_followers_resume_reports_cumulative_progress(followers_progress_file, stub_private_request):
    """
    회귀 테스트: 이어받는 중에 화면 진행률(app_state)이 이전 실행분을 포함한 누적 수치를
    보여줘야 한다. 안 그러면 실제로는 이어받고 있는데도 이번 실행분(0부터)만 보여서
    처음부터 다시 하는 것처럼 보인다 — 실사용에서 실제로 겪은 혼란.
    """
    page1 = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor-A"}
    stub_private_request(lambda path, params: page1)
    instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)
    assert instagram.app_state["fetch_progress"]["fetched"] == 2  # 1회차: offset 없음

    page2 = {"users": [raw_user(3)], "next_max_id": "cursor-B"}
    stub_private_request(lambda path, params: page2)
    instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)

    assert instagram.app_state["fetch_progress"]["fetched"] == 3, (
        "이전에 모은 2명 + 이번에 새로 받은 1명 = 3명으로 보여야 한다(0+1이 아니라)"
    )


def test_fetch_all_followers_resumes_from_saved_cursor(followers_progress_file, stub_private_request):
    """두 번째 실행이 저장된 커서를 실제로 요청 파라미터에 실어보내고, 이전 결과와 합쳐야 한다."""
    pages_by_cursor = {
        "": {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor-A"},
        "cursor-A": {"users": [raw_user(3)], "next_max_id": None},
    }
    requests = stub_private_request(lambda path, params: pages_by_cursor[params.get("max_id", "")])

    ids1, complete1 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)
    assert complete1 is False

    requests2 = stub_private_request(lambda path, params: pages_by_cursor[params.get("max_id", "")])
    ids2, complete2 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)

    assert complete2 is True
    assert ids2 == {"1", "2", "3"}, "이전에 모은 1,2 와 이번에 새로 받은 3 이 합쳐져야 한다"
    assert requests2[0][1]["max_id"] == "cursor-A", "저장된 커서로 이어받아야 한다"
    assert not os.path.exists(followers_progress_file), "완료되면 진행상황 파일을 지워야 한다"


def test_fetch_all_followers_stale_resume_cursor_does_not_falsely_complete(
    followers_progress_file, stub_private_request
):
    """
    회귀 테스트: 이어받은 커서가 무효화돼 빈 페이지(그런데 next_max_id는 여전히 옴)가 오면
    "다 모았다"로 착각하면 안 된다. 착각하면 아직 못 가져온 진짜 팔로워가 비팔로워로
    오분류되는 원래 사고가 재발한다. empty_page/no_progress는 안전 중단일 뿐 완료 신호가 아니다.
    """
    page1 = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor-A"}
    stub_private_request(lambda path, params: page1)
    ids1, complete1 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)
    assert complete1 is False

    # 이어받기 시도 — 커서가 무효화돼 빈 페이지만 돌아옴 (그런데도 next_max_id는 옴)
    stale_page = {"users": [], "next_max_id": "cursor-A-again"}
    stub_private_request(lambda path, params: stale_page)
    ids2, complete2 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)

    assert complete2 is False, "빈 페이지는 안전 중단이지 완료 신호가 아니다"
    assert ids2 == {"1", "2"}, "이전에 모은 결과는 유지되어야 한다"
    assert os.path.exists(followers_progress_file), "미완료 상태이므로 진행상황을 지우면 안 된다"


def test_fetch_all_followers_ignores_progress_for_different_user(followers_progress_file, stub_private_request):
    """다른 계정의 저장된 진행상황을 그대로 이어받으면 안 된다."""
    stub_private_request(lambda path, params: {"users": [raw_user(1)], "next_max_id": "cursor-A"})
    instagram._fetch_all_followers("user-a", expected_total=1000, max_pages=1)
    assert os.path.exists(followers_progress_file)

    requests = stub_private_request(lambda path, params: {"users": [raw_user(2)], "next_max_id": None})
    ids, complete = instagram._fetch_all_followers("user-b", expected_total=1000, max_pages=1)

    assert ids == {"2"}
    assert requests[0][1].get("max_id") is None, "user-a의 커서를 이어받으면 안 된다"


def test_fetch_all_followers_dry_run_does_not_persist(followers_progress_file, stub_private_request):
    """persist=False(dry_run)면 미완료여도 디스크에 아무것도 남기면 안 된다."""
    stub_private_request(lambda path, params: {"users": [raw_user(1)], "next_max_id": "cursor-A"})

    ids, complete = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1, persist=False)

    assert complete is False
    assert not os.path.exists(followers_progress_file)


def test_fetch_all_followers_discards_stale_progress(followers_progress_file, stub_private_request):
    """
    이어받기가 MAX_FOLLOWERS_PROGRESS_AGE_SEC보다 오래 끌리면 누적분을 버리고 새로 시작해야
    한다 — 너무 오래 걸린 수집은 그 사이 새로 생긴 팔로워를 놓쳤을 위험이 커서 무한정
    신뢰하면 안 된다(정렬 방식에 따라 이미 지나간 커서 앞쪽에 새 팔로워가 끼어들 수 있음).
    """
    page = {"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor-A"}
    stub_private_request(lambda path, params: page)
    ids1, complete1 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)
    assert complete1 is False
    assert ids1 == {"1", "2"}

    # 저장된 진행상황을 상한 시간보다 더 오래된 것처럼 조작한다.
    with open(followers_progress_file) as f:
        saved = json.load(f)
    saved["started_at"] = time.time() - instagram.MAX_FOLLOWERS_PROGRESS_AGE_SEC - 1
    with open(followers_progress_file, "w") as f:
        json.dump(saved, f)

    new_page = {"users": [raw_user(3)], "next_max_id": None}
    stub_private_request(lambda path, params: new_page)
    ids2, complete2 = instagram._fetch_all_followers("me", expected_total=1000, max_pages=1)

    assert ids2 == {"3"}, "오래된 진행상황(1,2)을 버리고 새로 시작해야 한다"
    assert complete2 is True


# --- fetch_non_followers 플로우 -------------------------------------------------

@pytest.fixture
def stub_fetch_flow(monkeypatch, followers_progress_file):
    """
    fetch_non_followers() 전체 흐름 테스트용 — following/followers 응답을 각각 지정한다.
    followers_pages는 리스트 또는 (누적 호출 횟수)->dict 콜러블(여러 웨이브 시뮬레이션용).
    """
    def install(following_pages, followers_pages, following_count, follower_count):
        # user_id/rank_token은 instagrapi Client의 읽기전용 프로퍼티라 클래스 레벨에서 덮어써야 한다.
        monkeypatch.setattr(type(instagram.cl), "user_id", property(lambda self: "999"))
        monkeypatch.setattr(
            instagram.cl, "user_info",
            lambda uid: SimpleNamespace(following_count=following_count, follower_count=follower_count),
        )
        cursor = {"following": 0, "followers": 0}
        requests = []

        def fake_private_request(path, params=None, **kwargs):
            requests.append(path)
            if path.startswith("accounts/current_user"):
                return {}
            if path.endswith("/following/"):
                idx = cursor["following"]
                cursor["following"] += 1
                return following_pages[min(idx, len(following_pages) - 1)]
            if path.endswith("/followers/"):
                cursor["followers"] += 1
                if callable(followers_pages):
                    return followers_pages(cursor["followers"])
                idx = cursor["followers"] - 1
                return followers_pages[min(idx, len(followers_pages) - 1)]
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(instagram.cl, "private_request", fake_private_request)
        monkeypatch.setattr(instagram.time, "sleep", lambda *_: None)
        # 실제 cooldown.json을 건드리면 안 된다 — dry_run=False 케이스를 테스트할 때만 호출된다.
        monkeypatch.setattr(instagram, "set_cooldown", lambda *a, **k: None)
        instagram.app_state["logged_in"] = True
        return requests

    return install


def test_fetch_non_followers_happy_path(stub_fetch_flow):
    following_pages = [{"users": [raw_user(1), raw_user(2)], "next_max_id": None}]
    followers_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    stub_fetch_flow(following_pages, followers_pages, following_count=2, follower_count=1)

    result = instagram.fetch_non_followers(dry_run=True)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert [u["username"] for u in result["users"]] == ["user2"]


def test_fetch_non_followers_requires_login(stub_fetch_flow):
    """로그인 게이트(_require_logged_in) 직접 검증 — 권한 체크는 자체 테스트가 있어야 한다."""
    following_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    followers_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    requests = stub_fetch_flow(following_pages, followers_pages, following_count=1, follower_count=1)
    instagram.app_state["logged_in"] = False  # stub_fetch_flow가 True로 켜둔 걸 되돌린다

    result = instagram.fetch_non_followers(dry_run=True)

    assert result["status"] == "login_required"
    assert requests == [], "로그인 안 된 상태에서는 네트워크를 전혀 건드리면 안 된다"


def test_run_write_action_requires_login():
    """unfollow/block이 공유하는 로그인 게이트도 마찬가지로 직접 검증한다."""
    instagram.app_state["logged_in"] = False

    result = instagram.bulk_unfollow(["1"])

    assert result["status"] == "login_required"


def test_fetch_non_followers_reports_partial_when_followers_incomplete(stub_fetch_flow):
    """
    followers 수집이 예산 안에 못 끝나면(dry_run=False, 실제 실행) status=partial이어야 하고,
    비교 결과(count/users)를 내면 안 된다 — 안 그러면 아직 못 가져온 진짜 팔로워(user1)가
    비팔로워로 오분류된다.
    """
    following_pages = [{"users": [raw_user(1), raw_user(2)], "next_max_id": None}]
    # 같은 페이지가 반복되면(=커서는 진행되는데 새 유저가 없음) no_progress 가드로 멈춘다 —
    # exhausted=False (안전 중단이지 완료 신호가 아니므로).
    followers_pages = [{"users": [raw_user(1)], "next_max_id": "cursor"}]
    requests = stub_fetch_flow(following_pages, followers_pages, following_count=2, follower_count=50)

    result = instagram.fetch_non_followers(dry_run=False)

    assert result["status"] == "partial"
    assert "users" not in result
    assert "count" not in result
    assert result["followers_fetched"] == 1
    assert result["followers_total"] == 50
    followers_calls = [p for p in requests if p.endswith("/followers/")]
    assert len(followers_calls) == instagram.MAX_FOLLOWERS_WAVES_PER_CALL * 2, (
        "웨이브가 계속 안 끝나도 MAX_FOLLOWERS_WAVES_PER_CALL에서 멈춰야 한다(무한 루프 방지)"
    )


def test_fetch_non_followers_auto_continues_across_waves_until_complete(stub_fetch_flow):
    """
    회귀 테스트: followers가 첫 웨이브(30페이지) 안에 못 끝나도 fetch_non_followers가 알아서
    다음 웨이브를 이어서 돌아 결국 완료해야 한다 — 사용자가 재클릭할 필요가 없어야 한다.
    """
    following_pages = [{"users": [raw_user(1)], "next_max_id": None}]

    def followers_page(call_count):
        # 1웨이브(30페이지)를 꽉 채운다: 매 페이지 새 유저 1명씩 — 커서는 안 끝남.
        if call_count <= instagram.FOLLOWERS_PAGE_BUDGET_PER_RUN:
            return {"users": [raw_user(1000 + call_count)], "next_max_id": "cursor"}
        # 2웨이브 첫 페이지에서 자연 종료.
        return {"users": [raw_user(1)], "next_max_id": None}

    requests = stub_fetch_flow(following_pages, followers_page, following_count=1, follower_count=31)

    result = instagram.fetch_non_followers(dry_run=False)

    assert result["status"] == "ok", "두 번째 웨이브에서 자동으로 완료됐어야 한다"
    followers_calls = [p for p in requests if p.endswith("/followers/")]
    assert len(followers_calls) == instagram.FOLLOWERS_PAGE_BUDGET_PER_RUN + 1, (
        "재클릭 없이 서버가 알아서 웨이브를 이어 돌았다는 증거 — 1웨이브(30) + 2웨이브(1)"
    )


def test_fetch_non_followers_reports_partial_when_following_incomplete(stub_fetch_flow):
    """
    following 자체가 예산 안에 못 끝나도(이어받기가 없는 경로, dry_run=False) 부분 목록으로
    비교하면 안 된다 — followers 호출까지 가지 않고 여기서 바로 partial을 반환해야 한다.
    """
    # 같은 페이지 반복 → no_progress 가드로 멈춤 → exhausted=False.
    following_pages = [{"users": [raw_user(1)], "next_max_id": "cursor"}]
    followers_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    stub_fetch_flow(following_pages, followers_pages, following_count=1, follower_count=1)

    result = instagram.fetch_non_followers(dry_run=False)

    assert result["status"] == "partial"
    assert "users" not in result
    assert result["following_fetched"] == 1
    assert result["following_total"] == 1


def test_fetch_non_followers_dry_run_ignores_incompleteness_gate(stub_fetch_flow):
    """
    회귀 테스트: dry_run은 max_pages=1로 일부러 following/followers를 1페이지만 받는다 —
    그때마다 '미완료'라며 partial로 조기 반환하면, dry_run 본래 목적(두 엔드포인트가 정상
    응답하는지 값싸게 확인하는 것)을 못 하게 된다. dry_run에서는 미완료여도 끝까지 진행해
    status=ok를 반환해야 한다(정확도 보장은 원래도 안 함 — docstring/HANDOFF.md 참고).
    """
    following_pages = [{"users": [raw_user(1), raw_user(2)], "next_max_id": "cursor"}]
    followers_pages = [{"users": [raw_user(1)], "next_max_id": "cursor"}]
    stub_fetch_flow(following_pages, followers_pages, following_count=999, follower_count=999)

    result = instagram.fetch_non_followers(dry_run=True)

    assert result["status"] == "ok", "dry_run은 미완료 게이트를 적용하지 않아야 followers/까지 검증한다"


# --- 동시 실행 방지 -------------------------------------------------------------
# fetch/unfollow/block은 asyncio.to_thread로 실행되는 진짜 OS 스레드다. 두 작업이 같은
# instagrapi Client를 동시에 두드리면 각자의 delay_range/페이지 간 지연이 서로를 모른 채
# 겹쳐서 실제 인스타그램에 나가는 요청 빈도가 그대로 배로 늘어난다 — 소프트 차단 위험 직결.

@pytest.fixture(autouse=True)
def _release_job_lock_after_test():
    """테스트가 assert 실패 등으로 락을 못 풀고 끝나도 다음 테스트에 영향 안 주게 정리한다."""
    yield
    if state._job_lock.locked():
        state._job_lock.release()


def test_try_start_job_blocks_concurrent_calls():
    assert state.try_start_job() is True
    assert state.try_start_job() is False, "이미 잡힌 락을 또 잡을 수 있으면 안 된다"
    state.finish_job()
    assert state.try_start_job() is True, "해제한 뒤에는 다시 잡을 수 있어야 한다"
    state.finish_job()


def test_fetch_non_followers_returns_busy_without_network_when_job_running(stub_fetch_flow):
    """
    다른 작업이 이미 진행 중이면 fetch_non_followers는 네트워크를 전혀 건드리지 않고
    즉시 busy를 반환해야 한다 — 그래야 두 작업이 겹쳐서 요청 빈도가 배로 늘어나는 걸 막는다.
    """
    following_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    followers_pages = [{"users": [raw_user(1)], "next_max_id": None}]
    requests = stub_fetch_flow(following_pages, followers_pages, following_count=1, follower_count=1)

    assert state.try_start_job() is True  # 다른 작업이 이미 실행 중인 것처럼 락을 선점
    result = instagram.fetch_non_followers(dry_run=True)

    assert result["status"] == "busy"
    assert requests == [], "락을 못 잡았으면 private_request를 한 번도 호출하면 안 된다"


def test_bulk_unfollow_returns_busy_without_network_when_job_running(monkeypatch):
    """
    unfollow/block(_run_write_action)도 fetch와 같은 락을 공유한다 — 다른 작업이 진행 중이면
    네트워크를 전혀 건드리지 않고 즉시 busy를 반환해야 한다.
    """
    requests = []
    monkeypatch.setattr(
        instagram.cl, "private_request",
        lambda path, *a, **k: requests.append(path) or {},
    )
    instagram.app_state["logged_in"] = True

    assert state.try_start_job() is True  # 다른 작업이 이미 실행 중인 것처럼 락을 선점
    result = instagram.bulk_unfollow(["1"])

    assert result["status"] == "busy"
    assert requests == [], "락을 못 잡았으면 private_request(세션 확인 포함)를 한 번도 호출하면 안 된다"
