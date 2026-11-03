# 설치·실행 환경 점검

`python3 setup_check.py`는 표준 라이브러리만으로 실행됩니다. `--json`을 붙이면 기계가 읽을 수 있는 결과를 출력합니다.
패키지를 import해서 계정 클라이언트를 만들지 않고 설치 메타데이터·실행 파일 존재 여부만 검사합니다.
화면의 설치 점검도 같은 결과를 제공합니다. 설치 점검 자체는 설치·업데이트·네트워크 요청을 수행하지 않습니다.

## 환경 고정

`requirements.lock.txt`는 오프라인 검증에 사용한 macOS arm64 / Python 3.9.6 환경의 의존성 전체를 정확한 버전으로 기록합니다.
`requirements.txt`는 해당 파일을 포함합니다. 설치 후 `venv/bin/python -m pip check`와 오프라인 검증을 실행하세요.
버전 변경 시 lock 파일과 검증 결과를 함께 갱신해야 합니다. 다른 플랫폼의 바이너리 패키지 호환성은 별도 검증 대상입니다.

## 브라우저

설치된 Google Chrome을 우선 사용합니다. 없으면 설치된 Playwright 패키지의 `browsers.json`에 지정된 Chromium 리비전과
실제 캐시의 실행 파일이 일치하는지 확인합니다. 다른 리비전의 브라우저를 임의로 사용하지 않습니다.
`PLAYWRIGHT_BROWSERS_PATH` 환경변수도 지원합니다. Chromium은 전체 브라우저의 headless 채널을 사용합니다.

```sh
venv/bin/python -m playwright install chromium
```

이는 자동 내보내기와 모의 브라우저 검증용입니다. 로컬 파일 처리는 자동화 브라우저 설치와 독립적입니다.
[Playwright 공식 안내](https://playwright.dev/python/docs/browsers)는 버전별 브라우저 설치와 `chromium` 채널의 headless 동작을 설명합니다.

## SSL

검증 환경의 Python 3.9.6은 LibreSSL 2.8.3을 사용합니다. 오프라인 기능 검증은 통과했지만 온라인 로그인 라이브러리에서
지원되지 않는 SSL 환경 경고가 발생합니다. 설치 점검은 이 문제를 온라인 환경 경고로 분리해 표시하며 로컬 기능을 막지 않습니다.
실제 계정 공동 검증 전에 OpenSSL 1.1.1 이상을 사용하는 Python으로 별도 가상환경을 준비하고 고정 의존성을 설치·검증해야 합니다.
기존 Python을 이 작업에서 교체하거나 계정 연결로 시험하지는 않았습니다.
[urllib3 공식 안내](https://urllib3.readthedocs.io/en/stable/v2-migration-guide.html)는 LibreSSL과 오래된 OpenSSL의 지원 제외를 설명합니다.

## 폴더 이동과 바탕화면 앱

웹의 정적 파일 경로는 현재 작업 폴더가 아닌 프로젝트 파일 위치를 기준으로 합니다.
바탕화면 앱은 프로젝트와 가상환경의 위치를 기록하므로 폴더 이동 후 새 위치에서 `venv/bin/python build_desktop_app.py`로 다시 만드세요.
새 실행기는 경로가 없으면 재생성·설치 점검 방법을 안내합니다. 서버를 시작하기 전에도 설치 상태를 확인합니다.
이 빌드 도구를 import하는 것만으로 앱을 설치하지 않습니다. 빌드 검증은 임시 Applications/Desktop에서 수행합니다.
