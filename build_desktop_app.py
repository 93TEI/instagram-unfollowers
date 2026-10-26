"""Build a Finder-launchable .app using this project's existing environment."""
from pathlib import Path
import plistlib
import platform
import shlex

root = Path(__file__).resolve().parent
app = Path.home() / "Desktop/인스타그램 맞팔.app"
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
        "LSUIElement": True,
        "LSArchitecturePriority": [platform.machine()],
    }, stream)
executable = macos / "InstagramMutual"
executable.write_text(
    "#!/bin/sh\nexec /usr/bin/arch -" + platform.machine() + " " + shlex.quote(str(root / "venv/bin/python"))
    + " " + shlex.quote(str(root / "desktop_launcher.py")) + "\n"
)
executable.chmod(0o755)
print(app)
