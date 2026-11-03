"""Build a Finder-launchable app; imports never modify Applications or Desktop."""
from pathlib import Path
import plistlib
import platform
import shlex
import shutil
import tempfile

IDENTIFIER = 'local.tei.instagram-mutual'
NAME = '인스타그램 맞팔.app'


def _check_bundle(path):
    if path.exists() and not path.is_symlink():
        try:
            with (path / 'Contents/Info.plist').open('rb') as stream:
                if plistlib.load(stream).get('CFBundleIdentifier') == IDENTIFIER:
                    return
        except (OSError, ValueError):
            pass
        raise RuntimeError('같은 이름의 다른 앱이 있습니다: ' + str(path))


def build_app(root=None, applications=None, desktop=None):
    root = Path(root or Path(__file__).resolve().parent).resolve()
    if not (root / 'desktop_launcher.py').is_file():
        raise RuntimeError('프로젝트 실행 파일이 없습니다. 현재 프로젝트 경로를 확인해주세요.')
    applications = Path(applications or Path.home() / 'Applications')
    desktop = Path(desktop or Path.home() / 'Desktop')
    app = applications / NAME
    shortcut = desktop / NAME
    _check_bundle(app)
    _check_bundle(shortcut)
    if app.is_symlink():
        raise RuntimeError('설치 위치가 다른 폴더로 연결되어 있습니다. 설치 경로를 확인해주세요.')
    if shortcut.is_symlink() and shortcut.resolve() != app.resolve():
        raise RuntimeError('바탕화면에 같은 이름의 다른 바로가기가 있습니다.')
    macos = app / 'Contents/MacOS'
    macos.mkdir(parents=True, exist_ok=True)
    (app / 'Contents/Resources').mkdir(exist_ok=True)
    with (app / 'Contents/Info.plist').open('wb') as stream:
        plistlib.dump({
            'CFBundleName':'인스타그램 맞팔', 'CFBundleDisplayName':'인스타그램 맞팔',
            'CFBundleIdentifier':IDENTIFIER, 'CFBundleExecutable':'InstagramMutual',
            'CFBundlePackageType':'APPL', 'CFBundleVersion':'2', 'CFBundleShortVersionString':'1.1',
            'LSUIElement':False, 'LSArchitecturePriority':[platform.machine()],
        }, stream)
    python = shlex.quote(str(root / 'venv/bin/python'))
    launcher = shlex.quote(str(root / 'desktop_launcher.py'))
    message = shlex.quote('프로젝트 폴더 또는 Python 환경을 찾지 못했습니다. 폴더를 이동했다면 새 위치에서 python3 build_desktop_app.py를 다시 실행하고, setup_check.py로 설치 상태를 확인해주세요.\n기존 프로젝트 위치: ' + str(root))
    alert = shlex.quote('on run argv\ndisplay alert "인스타그램 맞팔 · 실행 경로 확인" message (item 1 of argv) as critical\nend run')
    executable = macos / 'InstagramMutual'
    executable.write_text('#!/bin/sh\n'
        + 'if [ ! -x ' + python + ' ] || [ ! -f ' + launcher + ' ]; then\n'
        + '  /usr/bin/osascript -e ' + alert + ' ' + message + '\n  exit 1\nfi\n'
        + 'exec /usr/bin/arch -' + platform.machine() + ' ' + python + ' ' + launcher + '\n')
    executable.chmod(0o755)
    desktop.mkdir(parents=True, exist_ok=True)
    if shortcut.exists() and not shortcut.is_symlink():
        backup = Path(tempfile.mkdtemp(prefix='instagram-app-backup-'))
        shutil.move(str(shortcut), str(backup / shortcut.name))
    if not shortcut.is_symlink():
        shortcut.symlink_to(app, target_is_directory=True)
    return app


if __name__ == '__main__':
    print(build_app())
