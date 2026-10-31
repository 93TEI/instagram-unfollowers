# Instagram Unfollowers

> **경고: 이 프로그램을 사용하면 봇 사용으로 판정되어 인스타그램 계정이 확정적으로 정지됩니다. 사용하지 마세요.**

## JSON 기반 맞팔 비교

기존 로그인·인증 화면을 사용합니다. **맞팔 안한 목록**은 로컬에 저장된 JSON을 읽고
`팔로잉 아이디 − 팔로워 아이디`를 계산합니다. 이 버튼은 인스타그램 요청을 보내지 않습니다.
결과에는 아이디 텍스트만 표시하며, 프로필 사진·이름 조회와 언팔로우·차단 기능은 제거했습니다.

- **JSON 갱신**: 기존 로그인 세션을 이용해 계정 센터의 내보내기 화면을 처리합니다.
  팔로워·팔로잉, 전체 기간, JSON을 선택하고 확인한 뒤 한 번 제출합니다.
- **JSON 준비 확인**: 진행 중인 내보내기가 있으면 새로 요청하지 않고 준비 상태를 확인합니다.
  준비되면 ZIP을 다운로드하고 관련 JSON을 저장합니다. 확인 사이에는 최소 30분 간격을 둡니다.
- 앱은 내보내기 준비 상태를 자동 반복 확인하지 않습니다. 목록 재조회·화면 새로고침·앱 재시작도
  새 내보내기를 만들지 않습니다. 제출 후 응답을 잃으면 제출 여부 미확인 상태를 유지합니다.
- JSON과 작업 기록은 `exports/<숫자 계정 ID>/`에 계정별로 저장하며 Git에서 제외합니다.
  화면의 저장 시각은 이 컴퓨터에 파일을 저장한 시각입니다. 실시간 팔로우 상태를 뜻하지 않습니다.

자동 내보내기는 공개된 화면 절차를 따르는 브라우저 자동화입니다. **현재 실서비스의 화면과
기존 모바일 로그인 세션의 웹 호환성은 실제 계정으로 검증하지 않았습니다.** 추가 인증,
여러 내보내기, 화면 변경 등으로 대상을 확인하지 못하면 중단하며 직접 목록 수집으로 전환하지 않습니다.
로컬 비교의 무통신 동작과 로그인·내보내기의 온라인 동작은 구분해야 합니다.

## 실행

```sh
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 18765
```

macOS에 설치된 Google Chrome을 사용합니다. Chrome이 없는 환경에서는
`venv/bin/python -m playwright install chromium`으로 브라우저를 설치합니다.
기존 바탕화면 앱의 실행 방법은 [DESKTOP.md](DESKTOP.md)를 참고하세요.

이미 내보낸 ZIP 또는 압축 해제 폴더도 오프라인으로 가져올 수 있습니다.
한 번의 내보내기에 속한 **모든 파일**을 함께 지정하세요.

```sh
venv/bin/python import_export.py --account-id 123456 --username my_account /path/to/export.zip
```

`following.json`, `followers.json` 또는 `followers_1.json`, `followers_2.json` 등의 분할 파일을 지원합니다.
기간은 전체 기간이어야 합니다. JSON 자체만으로 마지막 분할 파일의 누락이나 내보내기 기간을
완전히 판별할 수는 없습니다. 번호 중간 누락·손상·잘못된 형식은 결과를 표시하지 않고 오류로 처리합니다.

## 계정을 사용하지 않는 검증

```sh
venv/bin/python -m pytest -q test_non_followers.py
RUN_OFFLINE_BROWSER_TESTS=1 venv/bin/python -m pytest -q test_browser_offline.py
```

합성 JSON과 모의 화면만 사용합니다. Python의 외부 연결을 차단하고, 브라우저 요청도 모두
로컬 응답으로 대체하거나 차단합니다. 사용자 세션 파일을 읽지 않고 실제 내보내기를 제출하지 않습니다.
검증 범위와 참고 자료는 [docs/json-export.md](docs/json-export.md)에 기록했습니다.
