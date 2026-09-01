# Instagram 비팔로워 차단기 - Handoff

## Goal

인스타그램 계정에 로그인하여 내가 팔로우하지만 나를 팔로우하지 않는 유저를 찾아 차단하는 로컬 웹앱 구현.

- 웹 UI (브라우저)로 결과 확인 후 선택적 차단
- instagrapi (Python 비공식 Instagram API) 사용
- FastAPI 백엔드 + 단일 HTML 프론트엔드

## Current Progress

- [x] 기술 스택 결정: Python + FastAPI + instagrapi + vanilla HTML/JS
- [x] 프로젝트 구조 설계 완료
- [x] API 엔드포인트 설계 완료
- [x] 프론트엔드 상태 머신 설계 완료
- [x] 레이트 리미팅 전략 확정
- [x] 프로젝트 디렉토리 생성: `/Users/tei/instagram-unfollowers/`
- [x] 전체 구현 완료 (로그인/2FA, 조회, 언팔, 차단, 웹 UI)
- [x] 소프트 차단 사고 대응 — 아래 "소프트 차단 사고" 섹션 참고
- [ ] 새 수집 로직 실전 검증 (드라이런 → 전체 실행 → 24~48시간 뒤 카운트 확인)

## What Worked

- instagrapi가 팔로워 조회, 차단 등 필요한 모든 기능을 지원
- SSE (Server-Sent Events)가 진행상황 스트리밍에 적합 (WebSocket 불필요)
- 단일 HTML 파일로 빌드 도구 없이 프론트엔드 구현 가능

## What Didn't Work

- Official Instagram Graph API는 차단 기능 미지원, 비즈니스 계정만 사용 가능 → 불가
- settings.json에 `effort` 필드 직접 추가 불가 → env 블록에 환경변수로 해결

## Next Steps (단계별 구현 순서)

### Step 1: 프로젝트 세팅
- `requirements.txt` 작성
- `.gitignore` 작성
- venv 생성 및 의존성 설치
- `static/` 디렉토리 생성

### Step 2: `state.py` 작성
- 앱 전역 상태 딕셔너리 정의
- 로그인 상태, 작업 상태, 진행상황, 비팔로워 목록 관리

### Step 3: `instagram.py` 작성
- instagrapi Client 초기화
- `try_resume_session()`: 기존 session.json으로 자동 로그인
- `login()`: 로그인 + TwoFactorRequired/ChallengeRequired 처리
- `verify_2fa()`: 2FA 코드 제출
- `fetch_non_followers()`: 팔로잉/팔로워 수집 → 집합 차 계산
- `block_users()`: 레이트 리미팅 적용하며 순차 차단
- `logout()`: 세션 삭제 및 상태 초기화

### Step 4: `main.py` 작성
- FastAPI 앱 + lifespan (세션 복원)
- `POST /api/login`, `POST /api/verify`
- `GET /api/status`
- `GET /api/fetch-stream` (SSE, 백그라운드 스레드)
- `POST /api/block-start` + `GET /api/block-stream` (SSE)
- `POST /api/logout`
- `GET /` → static/index.html 서빙

### Step 5: `static/index.html` 작성
- 상태 머신: login → 2fa → fetching → confirm → blocking → done
- 로그인 폼, 2FA 입력, 프로그레스 바, 유저 목록 (체크박스), 차단 진행 표시
- EventSource로 SSE 스트림 소비
- 전체선택/해제, 확인 다이얼로그

### Step 6: 테스트 및 검증
- `uvicorn main:app --reload --port 8000` 실행
- 로그인 → 2FA → 수집 → 목록 확인 → 소수 유저 차단 테스트
- session.json 재활용 확인

## 소프트 차단 사고 (2026-08-01)

팔로워 목록 전체 페이징 방식으로 fetch를 실행한 **다음 날** 프로필의 팔로워/팔로잉 숫자가
사라지는 소프트 차단이 발생했다. 두 번 재현됨.

- 1회 fetch = `following/` 10 페이지 + `followers/` **134 페이지** = 약 144 요청 / 3~5분
- 요청은 **전부 200 OK**. 예외가 하나도 발생하지 않았고 제한은 하루 뒤에 나타났다.
- 따라서 **런타임 에러 처리로는 이 문제를 감지할 수 없다.** 방어는 사전 요청 예산뿐이다.
- 참고 기준: `friendships/followers/` 는 세션당 시간당 약 30 페이지가 한계로 알려져 있다.

### 페이지네이션 폭주 (진짜 원인)

팔로워가 **1,000명인 계정에서 `followers/` 를 134페이지** 긁었다. 페이지당 7.5명꼴 —
정상이면 5~20페이지면 끝난다. `next_max_id` 가 끝나지 않으면 같은 구간을 계속 다시 받는데,
결과를 딕셔너리에 담으니 중복이 사라져 **폭주가 눈에 띄지 않았다.**

instagrapi 의 `user_following_v1_chunk` 도 동일하게 가드가 없다. 라이브러리를 써도 못 피한다.
`_fetch_relationship_list()` 의 세 가지 중단 조건이 이 문제를 막는다:

| 조건 | 의미 |
|------|------|
| `new_count == 0` | 커서는 오는데 새 유저가 없음 = 같은 구간 반복 |
| `not raw_users` | 빈 페이지 |
| `pages >= budget` | 예상 인원 대비 2배 초과 (`_page_budget()`), 최대 60 |

페이지마다 `following_page ... returned=N new=N total=N` 을 로그로 남기므로,
다음에 이상하면 로그만 보면 바로 안다.

### 대응 (2026-08-01, 1차)

1. 팔로워 목록 전체 수집을 제거하고 `friendships/show_many/` 배치 조회로 대체.
   추가로 팔로잉 응답에 `friendship_status.followed_by` 가 같이 오면 show_many 를 건너뛴다.
   팔로잉 1,400 기준 시뮬레이션 결과 **144회 → 8회(inline 있음) / 22회(없음)**.
2. `cl.delay_range = [3, 7]` 전역 적용 + 페이지/청크 간 추가 지연.
3. `state.py` 에 권장 대기 시간 도입. `cooldown.json` 에 저장되어 서버 재시작에도 유지된다.
   **실행을 막지는 않는다** — 사용자가 위험을 감수하고 언제든 실행할 수 있어야 한다는 결정.
   UI에 경고만 표시한다.
4. 자동 `relogin()` 제거 — 제한 상태에서 재로그인하면 악순환이 된다.
5. 스로틀 계열 예외에서 `session.json` 을 지우지 않는다 (기존에는 세션 만료로 오인).

### show_many 무력화 및 followers/ 페이징 복귀 (2026-08-13, 2차)

인스타그램이 `friendships/show_many/` 응답에서 `followed_by` 필드 자체를 제거했다 (요청은
200 OK로 오지만 각 유저 status에 그 필드가 없음 — `friendships/show/<id>/` 단건 조회에는
정상적으로 옴, 배치 엔드포인트에서만 빠짐). 위 1차 대응의 핵심 최적화가 완전히 무력화됨.

대안 검토: (A) `friendships/show/` 단건 조회 988회 — 1차 사고와 동일 규모의 요청량이라 기각.
(B) `followers/` 전체 페이징 복귀 — 1차 사고의 진짜 원인은 페이징 자체가 아니라 **커서 반복
버그**(같은 구간을 무한히 다시 받음)였고, 그 버그는 이미 `_fetch_relationship_list()`의 3중
중단 조건으로 고쳐져 있었다. 실제로 following/ 988명이 5페이지에 정상 종료되는 것을 로그로
확인했고, followers/도 같은 함수/가드를 공유하므로 동일하게 동작할 것으로 판단해 (B)로 결정.

`_fetch_all_following()` → `_fetch_relationship_list(user_id, relation, ...)`로 일반화해
following/followers 양쪽이 같은 가드를 쓰도록 통합. `_fetch_followed_by()` (show_many 기반)
삭제. `followed_by`는 이제 API 호출 없이 `팔로워 id 집합 in following` 로컬 연산으로 계산.

이어받기 정확성 관련 후속 수정(같은 날, 실전 실행에서 드러남):
- `exhausted`는 자연 종료(`next_max_id` 없음)일 때만 `True`. `empty_page`/`no_progress`도
  한때 `True`로 잘못 처리됐었다 — 이어받기 중 커서 무효화로 빈/중복 페이지가 오는 걸 "완료"로
  착각해 오분류가 재발할 뻔했다.
- `followers_progress.json`에 `started_at` 저장, `MAX_FOLLOWERS_PROGRESS_AGE_SEC`(24시간) 넘으면
  폐기하고 새로 시작 — 이어받기가 너무 오래 끌리면 그 사이 새로 생긴 팔로워를 인스타그램의
  페이지 정렬상 영영 놓칠 수 있어서(정렬 방식은 라이브 검증 불가, 확신은 못 함) 무한정
  누적되지 않게 상한을 걸었다.
- `state.py`에 `threading.Lock` 기반 동시 실행 방지(`try_start_job`/`finish_job`) 추가 —
  `app_state["job_status"]`는 원자적이지 않아 두 요청이 동시에 통과할 수 있었다(TOCTOU).
  fetch와 unfollow/block이 같은 `cl` 인스턴스를 동시에 두드리면 delay_range가 무력화돼
  실질 요청 빈도가 배로 는다.

### 팔로워 웨이브 자동 이어달리기 (2026-08-13, 3차)

사용자가 30페이지(1웨이브) 끝날 때마다 수동으로 재클릭해야 하는 게 번거롭고, 화면 진행률이
이번 웨이브분만 보여줘서(이전 웨이브 누적을 안 더함) 실제로는 이어받는 중인데 처음부터
다시 하는 것처럼 보이는 문제가 겹쳐 사용자가 크게 혼란스러워했다. 진행률 표시는 offset을
더하는 걸로 고쳤고(그래프상 버그였음), 재클릭 자체를 없애달라는 요청은 별도로 확인받았다:
페이지 간 지연(2~5초)은 그대로 두고, `fetch_non_followers()`가 웨이브 사이
`FOLLOWERS_INTER_WAVE_DELAY`(10~20초)만 쉬면서 최대 `MAX_FOLLOWERS_WAVES_PER_CALL`(10)
웨이브까지 자동으로 이어서 돈다. **이건 명시적으로 트레이드오프다** — 웨이브 사이에 사람이
수동으로 재시도하며 자연히 생기던 긴 텀이 없어지므로, 팔로워가 아주 많은 계정에서는 한 세션에
훨씬 많은 페이지가 짧은 시간에 몰릴 수 있다. 그래도 못 끝나면(10웨이브=최대 300페이지) 여전히
`partial`을 반환하고 절대 비교 결과를 계산하지 않는다.

### 하지 말 것

- `_fetch_relationship_list()` 의 중단 조건 3개(`no_progress`/`empty_page`/`page_budget`) 중
  하나라도 빼기 — following/followers 둘 다 재발 위험. 페이징 폭주의 실질적 방어는 이것뿐이다.
- 요청 간 지연 줄이기 (`delay_range`, 페이지 간 sleep, following↔followers 사이 10~20초,
  `FOLLOWERS_INTER_WAVE_DELAY`)
- `MAX_FOLLOWERS_WAVES_PER_CALL` 없애거나 크게 늘리기 — 이미 사용자 확인 하에 재클릭을
  없앤 대가로 세션당 요청이 늘어난 상태라, 상한까지 풀면 안전장치가 사실상 없어진다
- 스로틀 응답을 받고 재로그인하기
- 실행을 막는 게이트 다시 넣기 (의도적으로 제거함)
- `friendships/show_many/` 로 되돌리기 (`followed_by` 필드가 사라진 채로 남아있으면 조용히
  0명만 나온다 — 예외 없이 실패하므로 알아채기 어렵다)

## 레이트 리미팅 규칙 (구현된 실제 값)

| 구간 | 딜레이 |
|------|--------|
| 모든 private request 뒤 | `delay_range` random 2~4초 (instagrapi 자동) |
| following/followers 페이지 간 | 추가 random 2~5초 |
| 팔로잉 수집 → 팔로워 수집 사이 | random 10~20초 |

지연을 더 줄이지 말 것 — 이 값이 하한선이다.
| 언팔/차단 건별 | random 2~5초 |
| 언팔/차단 10건마다 | 추가 random 30~60초 |
| 언팔/차단 1회 실행 상한 | **50명** (초과분은 `skipped` 로 반환) |

### 권장 대기 시간 (경고 표시용, 실행은 막지 않음)

| 트리거 | 기간 |
|--------|------|
| fetch 성공 | 6시간 |
| 언팔/차단 10명 이상 | 1시간 |
| `PleaseWaitFewMinutes` / 429 | 24시간 |
| `feedback_required` | 72시간 |

경고를 지우려면 `cooldown.json` 삭제.

## 프로젝트 구조

```
instagram-unfollowers/
├── main.py                 # FastAPI 앱
├── instagram.py            # instagrapi 로직
├── state.py                # 앱 상태 + 쿨다운
├── test_non_followers.py   # select_non_followers() 유닛 테스트
├── requirements.txt
├── session.json            # (gitignored) 저장된 세션
├── device.json             # (gitignored) 디바이스 지문 — 절대 바꾸지 말 것
├── cooldown.json           # (gitignored) 쿨다운 상태
├── .gitignore
├── HANDOFF.md              # 이 파일
└── static/
    └── index.html          # 프론트엔드
```

## 명령어

```bash
./venv/bin/uvicorn main:app --port 8000    # 실행
./venv/bin/pytest test_non_followers.py -q # 테스트
curl -X POST "localhost:8000/api/fetch?dry_run=true"  # 드라이런 (세션확인/user_info 포함 요청 약 4회)
```

## 핵심 의존성

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
instagrapi>=2.1.0
Pillow>=10.0.0
httpx>=0.27.0
```

## 주의사항

- instagrapi는 비공식 API — Instagram ToS 위반이나 개인 계정 관리 목적으로는 법적 리스크 없음
- 계정 일시 제한 가능성 있음 → 레이트 리미팅 필수
- 차단 중 앱/브라우저 닫으면 중단됨
- session.json에 인증 토큰 포함되므로 절대 git에 커밋하지 말 것
