"""Build a Finder-launchable .app using this project's existing environment."""
from pathlib import Path
import plistlib
import platform
import shlex
import shutil
import tempfile

root = Path(__file__).resolve().parent
app = Path.home() / "Applications/인스타그램 맞팔.app"
macos = app / "Contents/MacOS"
macos.mkdir(parents=True, exist_ok=True)
(app / "Contents/Resources").mkdir(exist_ok=True)
with (app / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump({
        "CFBundleName": "인스타그램 맞팔",
        "CFBundleDisplayName": "인스타그램 맞팔",
        "CFBundleIdentifier": "local.tei.instagram-mutual",
        "CFBundleExecutable": "InstagramMutual",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
        "LSUIElement": False,
        "LSArchitecturePriority": [platform.machine()],
    }, stream)
executable = macos / "InstagramMutual"
executable.write_text(
    "#!/bin/sh\nexec /usr/bin/arch -" + platform.machine() + " " + shlex.quote(str(root / "venv/bin/python"))
    + " " + shlex.quote(str(root / "desktop_launcher.py")) + "\n"
)
executable.chmod(0o755)
desktop = Path.home() / "Desktop/인스타그램 맞팔.app"
if desktop.exists() and not desktop.is_symlink():
    with (desktop / "Contents/Info.plist").open("rb") as stream:
        existing = plistlib.load(stream)
    if existing.get("CFBundleIdentifier") != "local.tei.instagram-mutual":
        raise RuntimeError("바탕화면에 같은 이름의 다른 앱이 있습니다.")
    backup = Path(tempfile.mkdtemp(prefix="instagram-app-backup-"))
    shutil.move(str(desktop), str(backup / desktop.name))
if not desktop.is_symlink():
    desktop.symlink_to(app, target_is_directory=True)
print(app)
