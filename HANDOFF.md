# Instagram 맞팔 확인 — 오프라인 개선 핸드오프

## 작업 원칙

- 사용자 요청: 아래 개선을 순서대로 구현하고 **기능 하나마다 검증 → 설명 있는 커밋 → GitHub main 푸시**한다.
- 실제 계정 로그인·조회·내보내기를 테스트하지 않는다. 사용자 세션 파일도 검증에 사용하지 않는다.
- Python 외부 연결 차단과 모의 브라우저 화면을 사용한다. 실서비스 자동 내보내기는 나중에 사용자와 함께 검증한다.
- 기존 로그인은 유지하되 로컬 파일 이용은 독립시킨다. 사용자명 외 프로필 정보 수집과 언팔로우·차단은 추가하지 않는다.
- 화면 디자인은 유지하고 필요한 가져오기·복구·파일 관리·결과 저장 동작만 추가한다.
- 계정 세션·원본 내보내기·로그는 커밋하지 않는다. 서버를 실행하거나 기존 계정으로 확인하지 않는다.

## 순서와 완료 기준

| 번호 | 개선 | 완료 기준 | 상태 |
|---|---|---|---|
| 1 | 작업 복구 | 제출 전 중단은 새 요청 가능, 제출 이후 불명확한 작업은 재제출 금지; 재시작·타임아웃 검증 | 완료 |
| 2 | 화면에서 ZIP·JSON 가져오기 | 여러 파일 선택·드래그, 숫자 계정 ID 불필요, 크기·경로 제한, 실패 시 기존 데이터 보존 | 완료 |
| 3 | 로컬 비교와 로그인 분리 | 로그인 없이 가져오기·비교·재시작 후 재사용, 로그인·로그아웃과 데이터 소스 분리 | 완료 |
| 4 | 입력 검증·신뢰도 표시 | 파일 목록·인원수·검증 결과 표시, 누락·손상 차단, 완전성을 증명할 수 없음을 명시 | 대기 |
| 5 | 기준 시점 표시 | 가져온 시각과 내보내기 생성 시각 구분, 생성 시각 미확인 및 사용자 지정 표시 | 대기 |
| 6 | 오류별 복구 | 인증·화면 변경·다운로드·파일 손상 구분, 상황별 동작 안내, 안전한 작업 기록 복구 | 대기 |
| 7 | 다운로드·자료 관리 | 순서 변경 시 오인 재사용 방지, 정상 데이터 보존, 정리·삭제 전 범위 확인 | 대기 |
| 8 | 결과 활용 | 전체/검색 결과 복사, TXT·CSV 저장, 검색 건수; 인스타그램 요청 없음 | 대기 |
| 9 | 설치·배포 | 검증 버전 고정, 설치 상태 점검, 경로 독립 실행, 현재 설명과 검증 범위 문서화 | 대기 |

## 커밋 이름

1. `fix: recover exports interrupted before submission`
2. `feat: import Instagram ZIP and JSON files from the app`
3. `feat: compare local exports without Instagram login`
4. `feat: show export validation and completeness details`
5. `feat: distinguish export dates from local import times`
6. `feat: guide recovery from export and file errors`
7. `feat: manage saved data and resume downloads safely`
8. `feat: copy and download filtered comparison results`
9. `build: pin dependencies and add offline setup diagnostics`

각 커밋 본문에 문제·변경 동작·검증 결과를 쓴다. main과 현재 작업 브랜치에 함께 푸시한다.
커밋 자체에 자기 SHA를 넣지 않고 Git 이력에서 위 제목으로 확인한다.

## 검증 명령

```sh
venv/bin/python -m pytest -q test_non_followers.py
RUN_OFFLINE_BROWSER_TESTS=1 venv/bin/python -m pytest -q test_browser_offline.py
git diff --check
```

기준 버전: main `21d56e1`. 기존 검증은 순수/HTTP 51개, 모의 브라우저 7개다.
브라우저 검증은 모든 요청을 로컬로 대체하고 DNS를 차단하며 실제 계정 파일을 읽지 않는다.

## 다음 실계정 공동 검증에서만 확인할 항목

- 모바일 로그인 세션의 계정 센터 호환성 및 추가 인증 절차.
- 실제 내보내기 화면 구조와 작업·다운로드 식별 정보.
- 실제 요청 생성부터 준비·전체 파일 다운로드까지의 연동.

## 진행 기록

- 2026-10-31: 개선 목록과 기능별 검증·커밋·푸시 규칙을 기록했다. 기존 HANDOFF.md의 로컬 삭제 상태는 이 문서로 대체한다.

- 1번 완료: 제출 전 preparing 복구와 제출 마커 우선 처리를 구현했다. 순수/HTTP 검증 53개 통과.

- 2번 완료: 파일 선택·드래그 업로드와 ZIP/낱개 JSON 처리를 추가했다. 업로드 전체 100MB/100개 제한 및 실패 시 기존 자료 보존. 순수/HTTP 59개, 모의 브라우저 8개 통과.

- 3번 완료: 기본 화면을 로컬 비교로 열고 로컬 파일/저장된 계정 자료 선택을 로그인과 분리·저장했다. 계정 클라이언트 생성 없이 재시작 후 비교 가능. 순수/HTTP 61개, 모의 브라우저 9개 통과.
