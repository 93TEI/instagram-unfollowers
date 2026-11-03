"""Offline setup diagnostics using metadata/files only. Never reads session.json."""
import argparse
import importlib.metadata
import json
import os
import ssl
import sys
from pathlib import Path

from browser_runtime import find_browser

ROOT = Path(__file__).resolve().parent


def locked_versions(root):
    result = {}
    for line in (Path(root) / "requirements.lock.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        result[name] = version
    return result


def check_setup(root=ROOT, version_lookup=None, browser_lookup=None, python_version=None, tls=None):
    root = Path(root)
    version_lookup = version_lookup or importlib.metadata.version
    browser_lookup = browser_lookup or find_browser
    python_version = tuple(python_version or sys.version_info[:3])
    tls = tls or (ssl.OPENSSL_VERSION, ssl.OPENSSL_VERSION_INFO[:3])
    messages, packages = [], []
    try:
        for name, expected in locked_versions(root).items():
            try:
                installed = version_lookup(name)
            except importlib.metadata.PackageNotFoundError:
                installed = None
            packages.append({"name": name, "expected": expected, "installed": installed, "ok": installed == expected})
    except (OSError, ValueError):
        messages.append({"level": "error", "text": "고정 버전 목록을 읽을 수 없습니다. 프로젝트 파일 경로를 확인해주세요."})
    if any(not package["ok"] for package in packages):
        messages.append({"level": "error", "text": "설치되지 않았거나 검증 버전과 다른 패키지가 있습니다. 가상환경에서 requirements.txt를 다시 설치해주세요."})
    if python_version < (3, 9):
        messages.append({"level": "error", "text": "Python 3.9 이상이 필요합니다."})
    assets = all((root / name).is_file() for name in ("main.py", "static/index.html", "desktop_launcher.py"))
    if not assets:
        messages.append({"level": "error", "text": "실행에 필요한 프로젝트 파일이 없습니다. 폴더 이동 여부를 확인해주세요."})
    writable = os.access(root, os.W_OK) and (not (root / "exports").exists() or os.access(root / "exports", os.W_OK))
    if not writable:
        messages.append({"level": "error", "text": "프로젝트 또는 자료 저장 폴더에 쓰기 권한이 없습니다."})
    browser = browser_lookup()
    if not browser:
        messages.append({"level": "warning", "text": "자동 내보내기용 브라우저가 없습니다. Google Chrome 또는 Playwright Chromium을 설치해주세요. 로컬 파일 비교에는 필요하지 않습니다."})
    ssl_ok = tls[0].startswith("OpenSSL ") and tuple(tls[1]) >= (1, 1, 1)
    if not ssl_ok:
        messages.append({"level": "warning", "text": "온라인 로그인 라이브러리에 필요한 OpenSSL 환경을 확인해주세요. 현재 SSL은 " + tls[0] + "입니다. 로컬 파일 비교는 이용할 수 있습니다."})
    local_ready = assets and writable and python_version >= (3, 9) and bool(packages) and not any(message["level"] == "error" for message in messages)
    return {"status": "ok" if local_ready else "needs_setup", "local_ready": local_ready,
            "auto_export_environment_ready": local_ready and browser is not None and ssl_ok,
            "account_integration_verified": False, "python": ".".join(map(str, python_version)),
            "ssl": tls[0], "browser": browser, "packages": packages, "messages": messages}


def main():
    parser = argparse.ArgumentParser(description="계정 접속 없는 설치 상태 점검")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check_setup()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("로컬 파일 비교: " + ("준비됨" if result["local_ready"] else "설치 확인 필요"))
        print("자동 내보내기 실행 환경: " + ("준비됨 · 실제 계정 연동 미검증" if result["auto_export_environment_ready"] else "추가 확인 필요 · 실제 계정 연동 미검증"))
        for message in result["messages"]:
            print(message["level"] + ": " + message["text"])
        for package in result["packages"]:
            if not package["ok"]:
                print(package["name"] + ": " + str(package["installed"]) + " / 필요 " + package["expected"])
    return 0 if result["local_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
