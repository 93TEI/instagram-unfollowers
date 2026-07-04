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
- [ ] 코드 구현 시작 안 됨

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

## 레이트 리미팅 규칙

| 구간 | 딜레이 |
|------|--------|
| 차단 건별 | random 2~5초 |
| 10건마다 | 추가 30~60초 |
| 50건마다 | 추가 3~5분 |
| 팔로워/팔로잉 수집 사이 | random 1~2초 |

## 프로젝트 구조

```
instagram-unfollowers/
├── main.py           # FastAPI 앱
├── instagram.py      # instagrapi 로직
├── state.py          # 앱 상태
├── requirements.txt
├── session.json      # (gitignored) 저장된 세션
├── .gitignore
├── HANDOFF.md        # 이 파일
└── static/
    └── index.html    # 프론트엔드
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
