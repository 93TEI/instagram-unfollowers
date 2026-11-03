# Mac 바탕화면 앱

프로젝트 폴더에서 가상환경과 고정 의존성을 준비한 후 실행기를 만듭니다.

```sh
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python setup_check.py
venv/bin/python build_desktop_app.py
```

`~/Applications/인스타그램 맞팔.app`에 설치하고 바탕화면에 바로가기를 만듭니다.
다른 앱을 같은 이름으로 덮어쓰지 않습니다. 기존에 이 프로젝트가 만든 바탕화면 앱은 보관 후 바로가기로 교체합니다.

- 서버가 꺼져 있으면 설치 상태를 확인하고 서버·브라우저를 엽니다.
- 실행 중이면 **화면 열기 / 서버 끄기**를 선택합니다. 브라우저를 닫아도 서버는 계속 실행됩니다.
- 서버 종료는 진행 중인 작업도 중단합니다. 제출 전 준비와 제출 여부 미확인 상태는 재시작 후 구분해서 처리합니다.
- 프로젝트를 이동하거나 Python 환경을 변경한 뒤에는 새 위치에서 앱 생성 명령을 다시 실행하세요.
  새 실행기는 기록한 경로가 없으면 오류와 재생성 방법을 안내합니다.

서버는 `http://127.0.0.1:18765`에서만 연결을 받습니다. 로그·PID는 `~/Library/Logs/InstagramMutual/`에 저장합니다.
종료 시 저장된 PID와 서버 응답을 비교해 이 앱에서 실행한 프로세스인지 확인합니다.

Launchpad 등록을 갱신할 때는 다음 명령을 사용할 수 있습니다.

```sh
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$HOME/Applications/인스타그램 맞팔.app"
```

로그인·자동 내보내기는 선택 기능입니다. 로컬 파일을 가져와 비교하는 데 계정 접속은 필요하지 않습니다.
세션·기기 정보·자료·로그는 Git에 올리지 않습니다. 자세한 환경 점검은 [docs/setup.md](docs/setup.md)를 참고하세요.
