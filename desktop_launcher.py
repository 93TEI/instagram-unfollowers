"""Start the local app once, then open its browser interface from Finder."""
import fcntl
import json
import os
import platform
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:18765"
LOG_DIR = Path.home() / "Library/Logs/InstagramMutual"


def server_info():
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as response:
            info = json.load(response)
            if info.get("app") == "instagram-unfollowers" and info.get("status") == "ok":
                return info
    except (OSError, ValueError):
        pass
    return None


def healthy():
    return server_info() is not None


def running_action():
    result = subprocess.run([
        "/usr/bin/osascript", "-e",
        'button returned of (display dialog "서버가 실행 중입니다.\\n서버를 끄면 진행 중인 작업도 중단됩니다. 브라우저 창만 닫으면 서버는 계속 실행됩니다." '
        'with title "인스타그램 맞팔 · 서버 관리" '
        'buttons {"취소", "서버 끄기", "화면 열기"} default button "화면 열기")',
    ], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "취소"


def stop_server():
    info = server_info()
    if info is None:
        return
    pid_path = LOG_DIR / "server.pid"
    if not pid_path.exists() or int(pid_path.read_text()) != info.get("pid"):
        raise RuntimeError("이 앱에서 실행한 서버인지 확인할 수 없어 종료하지 않았습니다.")
    pid = info["pid"]
    if not isinstance(pid, int) or pid <= 1:
        raise RuntimeError("서버 실행 정보가 올바르지 않습니다.")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("서버가 작업을 정리하고 있습니다. 잠시 후 다시 확인해 주세요.")
        time.sleep(0.2)
    pid_path.unlink(missing_ok=True)
    subprocess.run(["/usr/bin/osascript", "-e",
                    'display dialog "서버를 껐습니다. 다시 사용하려면 바탕화면 앱을 열어주세요." '
                    'with title "인스타그램 맞팔" buttons {"확인"} default button "확인"'])


def launch():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    with (LOG_DIR / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy():
            action = running_action()
            if action == "서버 끄기":
                stop_server()
                return
            if action != "화면 열기":
                return
        else:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", 18765))
                except OSError:
                    raise RuntimeError("실행 포트 18765를 다른 프로그램이 사용 중입니다.")
            log_path = LOG_DIR / "server.log"
            with log_path.open("ab") as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(
                    ["/usr/bin/arch", "-" + platform.machine(), str(ROOT / "venv/bin/python"), "-m", "uvicorn", "main:app",
                     "--host", "127.0.0.1", "--port", "18765"],
                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,
                )
            pid_path = LOG_DIR / "server.pid"
            pid_path.write_text(str(process.pid))
            os.chmod(pid_path, 0o600)
            deadline = time.monotonic() + 120
            while not healthy():
                if process.poll() is not None:
                    raise RuntimeError("서버를 시작하지 못했습니다. 로그: " + str(log_path))
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise RuntimeError("서버 시작 시간이 초과되었습니다. 잠시 후 다시 실행해 주세요.")
                time.sleep(0.4)
        subprocess.run(["/usr/bin/open", URL], check=True)


if __name__ == "__main__":
    try:
        launch()
    except Exception as error:
        message = str(error)
        # Pass error text as argv, never interpolate it into AppleScript code.
        subprocess.run([
            "/usr/bin/osascript", "-e",
            'on run argv\ndisplay alert "인스타그램 맞팔" message (item 1 of argv) as critical\nend run',
            message,
        ])
        sys.exit(1)
