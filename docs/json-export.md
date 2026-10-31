# JSON 내보내기 구현 및 검증 범위

## 확인한 자료

- [Meta: 계정 센터의 정보 다운로드 안내](https://about.fb.com/news/2023/10/manage-your-information-across-apps/):
  인스타그램 정보 다운로드가 계정 센터에서 제공된다는 근거입니다.
- [계정 센터 내보내기 화면 안내와 캡처](https://thesagaapp.featurebase.app/en/help/articles/3104330-import-instagram-contacts):
  Create export → 프로필 선택 → Export to device → Customize information → 기간·형식 → Start export 절차를 참고했습니다.
  이 앱의 광범위한 정보 선택 권장은 사용하지 않고 팔로워·팔로잉만 선택합니다.
- [공개 JSON 파서의 파일명·구조](https://github.com/git2samus/Social-vibes/blob/main/instagram/parser.py):
  `connections/followers_and_following/` 경로와 `relationships_following`, `string_list_data` 형식을 참고했습니다.
  비교 알고리즘은 별도로 구현하고 방향을 검증합니다.
- [Playwright 다운로드](https://playwright.dev/python/docs/downloads),
  [네트워크 제어](https://playwright.dev/python/docs/network): 다운로드 이벤트를 기다린 뒤 컨텍스트를 닫기 전에 파일을 저장하며,
  프로필 이미지·목록 수집 요청은 차단합니다. 테스트에서는 모든 외부 요청을 대체합니다.

공식 Help Center 본문은 이 환경에서 로그인/접근 제한으로 확인하지 못했습니다.
공개 안내 및 모의 화면과의 일치는 실제 계정에 대한 현재 서비스 호환성을 보장하지 않습니다.
오래된 `dyi/request_download_data/` 경로는 채택하지 않았으며, 추정 GraphQL ID나 비공개 API를 호출하지 않습니다.

## 동작과 중복 방지

`/api/fetch`와 `/api/status`, `/api/progress`는 로컬 데이터만 읽습니다.
`/api/export/refresh`만 명시적인 내보내기 작업을 시작하거나 준비 상태를 확인합니다.
기존 `/api/login`, `/api/verify`, `/api/logout`은 유지하며 `/api/bulk-unfollow`, `/api/bulk-block`은 제거했습니다.
조회 응답의 `users` 항목은 `username`만 포함합니다.

프로세스 작업 잠금으로 로그인·로그아웃·갱신 간 계정 변경 충돌을 막고, 계정별 파일 잠금으로
여러 서버 프로세스의 동시 제출을 막습니다. 제출 직전에 작업 기록을 원자적으로 저장합니다.
제출 이후 타임아웃이나 재시작이 발생해도 새 요청을 만들지 않습니다.
준비 확인은 사용자가 눌렀을 때만 수행하며 최소 30분 간격을 적용합니다.
완료된 다운로드 조각은 해시와 함께 기록해 동일 작업에서 재사용합니다.

새 ZIP은 전체 파싱과 비교에 성공해야 현재 JSON 스냅샷으로 등록됩니다.
실패하면 이전 정상 스냅샷을 유지합니다. 계정별 디렉터리와 스냅샷 해시로 다른 계정의 파일 혼합,
저장 후 파일 변경·누락을 감지합니다. 사용자명은 대소문자를 정규화하고 중복 제거 후 정렬합니다.

## 오프라인으로 확인하는 항목

- 팔로잉에만 있는 계정 반환, 맞팔 제외, 대소문자·중복·정렬·빈 목록.
- 과거/현재 JSON 구조, 여러 팔로워 파일, ZIP 분할, 파일 누락·손상·잘못된 경로·용량 제한.
- 연속 클릭, 동시 작업, 재시작, 제출 이후 응답 유실, 다운로드 이후 저장 재개.
- 조회·상태 API의 무통신 동작, 삭제한 계정 변경 API의 404 응답.
- 모의 내보내기 화면의 계정 선택·팔로워/팔로잉만 선택·전체 기간·JSON·한 번의 제출.
- 모의 다운로드 파일 저장과 재사용, 변경되거나 모호한 화면에서 제출/다운로드 중단.
- 사용자명 텍스트 표시·검색과 이미지 요청·주기적 조회 부재.

모의 화면은 공개 안내를 바탕으로 만든 지원 형태의 예시이며 실제 계정 화면을 복제한 기록이 아닙니다.
실제 계정의 내보내기 제출·완료·다운로드는 사용자의 지시에 따라 검증하지 않았습니다.
추가 인증이나 화면 차이로 자동화가 멈추면 기존 파일 비교는 계속 사용할 수 있고,
공식적으로 받은 파일을 `import_export.py`로 가져올 수 있습니다.
