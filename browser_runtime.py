"""Discover a browser without launching one or reading account/session data."""
import importlib.metadata
import json
import os
import sys
from pathlib import Path


def find_browser(chrome_paths=None, cache_root=None, browser_manifest=None):
    if chrome_paths is None:
        if sys.platform == "darwin":
            chrome_paths = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        elif sys.platform == "win32":
            chrome_paths = [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"]
        else:
            chrome_paths = [Path("/opt/google/chrome/chrome"), Path("/usr/bin/google-chrome")]
    for path in chrome_paths:
        path = Path(path)
        if path.is_file() and os.access(path, os.X_OK):
            return {"kind": "chrome", "label": "Google Chrome", "channel": "chrome", "executable": str(path)}
    try:
        if browser_manifest is None:
            browser_manifest = importlib.metadata.distribution("playwright").locate_file("playwright/driver/package/browsers.json")
        browser_manifest = Path(browser_manifest)
        data = json.loads(browser_manifest.read_text())
        revision = next(item["revision"] for item in data["browsers"] if item["name"] == "chromium")
        if cache_root is None:
            custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            if custom == "0":
                cache_root = browser_manifest.parent / ".local-browsers"
            elif custom:
                cache_root = Path(custom)
            elif sys.platform == "darwin":
                cache_root = Path.home() / "Library/Caches/ms-playwright"
            elif sys.platform == "win32":
                cache_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ms-playwright"
            else:
                cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
        folder = Path(cache_root) / ("chromium-" + str(revision))
        names = ("Google Chrome for Testing", "Chromium", "chrome", "chrome.exe")
        if folder.is_dir():
            for name in names:
                for path in sorted(folder.rglob(name)):
                    if path.is_file() and os.access(path, os.X_OK):
                        return {"kind": "chromium", "label": "Playwright Chromium", "channel": "chromium", "executable": str(path)}
    except (OSError, ValueError, KeyError, StopIteration, importlib.metadata.PackageNotFoundError):
        pass
    return None
