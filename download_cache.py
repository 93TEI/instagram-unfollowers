"""Bind completed downloads to file identities, never their current screen order."""
import hashlib
import json
import re
import zipfile
from pathlib import Path

from export_data import ExportError, atomic_json

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mismatch(message):
    return ExportError(message, code="download_identity_unknown", actions=("import", "compare"))


def completed_downloads(directory):
    """Recover a fully saved batch without reopening Accounts Center."""
    directory = Path(directory)
    path = directory / "transfer.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        keys, parts = data["keys"], data["parts"]
        if data.get("version") != 2 or not isinstance(keys, list) or not 1 <= len(keys) <= 100 or not all(isinstance(key, str) for key in keys) or len(keys) != len(set(keys)) or not isinstance(parts, dict):
            raise ValueError()
        if not set(parts).issubset(keys):
            raise ValueError()
        if set(parts) != set(keys):
            return None
        paths = []
        for key in keys:
            part = parts[key]
            if not isinstance(part, dict) or not re.fullmatch(r"part-[1-9][0-9]*\.zip", str(part.get("filename", ""))):
                raise ValueError()
            item = directory / part["filename"]
            if part.get("state") != "complete":
                return None
            if item.is_symlink() or not item.is_file() or item.stat().st_size > MAX_DOWNLOAD_BYTES or not zipfile.is_zipfile(item) or file_hash(item) != part.get("sha256"):
                raise ValueError()
            paths.append(item)
        if len(paths) != len(set(paths)):
            raise ValueError()
        return sorted(paths, key=lambda item: int(item.stem.split("-")[1]))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise mismatch("저장된 다운로드 묶음이 변경되었거나 손상되었습니다. 온라인 요청 없이 중단했습니다.") from None


class DownloadCache:
    def __init__(self, directory, export_key, identities):
        self.directory = Path(directory)
        if self.directory.is_symlink() or self.directory.parent.is_symlink():
            raise mismatch("다운로드 폴더 경로가 변경되었습니다.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.manifest_path = self.directory / "transfer.json"
        count = len(identities)
        if not 1 <= count <= 100:
            raise mismatch("다운로드 파일 개수를 확인할 수 없습니다.")
        if count == 1:
            self.mode, self.keys = "single", ["single"]
        elif all(identities) and len(set(identities)) == count:
            self.mode = "stable"
            self.keys = [hashlib.sha256(value.encode()).hexdigest() for value in identities]
        else:
            self.mode, self.keys = "unidentified", [f"unknown-{i}" for i in range(count)]
        self.data = {"version": 2, "export": export_key, "mode": self.mode,
                     "keys": sorted(self.keys), "parts": {}}
        if self.manifest_path.exists():
            try:
                existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if (not isinstance(existing, dict) or any(existing.get(k) != self.data[k] for k in ("version", "export", "mode", "keys"))
                        or not isinstance(existing.get("parts"), dict) or not set(existing["parts"]).issubset(self.keys)):
                    raise ValueError()
                self.data = existing
                filenames = []
                for part in existing["parts"].values():
                    if (not isinstance(part, dict) or not re.fullmatch(r"part-[1-9][0-9]*\.zip", str(part.get("filename", "")))
                            or part.get("state") not in ("started", "complete")):
                        raise ValueError()
                    filenames.append(part["filename"])
                if len(filenames) != len(set(filenames)):
                    raise ValueError()
            except (ValueError, UnicodeError, TypeError):
                raise mismatch("이전 다운로드 기록과 현재 파일을 안전하게 연결할 수 없습니다. 파일을 보존하고 자동 이어받기를 중단했습니다.") from None
            if self.mode == "unidentified" and existing["parts"] and (
                len(existing["parts"]) != count or any(not self._path(part).is_file() for part in existing["parts"].values())
            ):
                raise mismatch("분할 파일을 식별할 수 없어 화면 순서로 이어받지 않았습니다. 받은 파일을 보존했습니다. 공식 ZIP을 모두 직접 가져오세요.")

    def _path(self, part):
        path = self.directory / part["filename"]
        if path.is_symlink():
            raise mismatch("다운로드 파일 경로가 변경되었습니다.")
        return path

    def existing(self, key):
        part = self.data["parts"].get(key)
        if part is None:
            return None
        path = self._path(part)
        if part["state"] == "started" and not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_DOWNLOAD_BYTES or not zipfile.is_zipfile(path):
            raise mismatch("이전에 받은 ZIP이 없어졌거나 손상되었습니다. 자동 재사용하지 않습니다.")
        digest = file_hash(path)
        if part["state"] == "complete" and digest != part.get("sha256"):
            raise mismatch("이전에 받은 ZIP 내용이 변경되었습니다. 파일을 섞지 않고 중단했습니다.")
        if part["state"] == "started":
            # Final names are installed only after save_as finishes. Recover a
            # crash after rename but before writing the completion marker.
            part.update(state="complete", sha256=digest)
            atomic_json(self.manifest_path, self.data)
        return path

    def begin(self, key):
        if key not in self.keys:
            raise mismatch("알 수 없는 다운로드 파일입니다.")
        if key not in self.data["parts"]:
            self.data["parts"][key] = {"filename": f"part-{len(self.data['parts']) + 1}.zip", "state": "started"}
        atomic_json(self.manifest_path, self.data)
        path = self._path(self.data["parts"][key])
        if path.with_suffix(".zip.partial").is_symlink():
            raise mismatch("다운로드 임시 파일 경로가 변경되었습니다.")
        return path

    def complete(self, key):
        part = self.data["parts"][key]
        path = self._path(part)
        if path.stat().st_size > MAX_DOWNLOAD_BYTES or not zipfile.is_zipfile(path):
            raise mismatch("다운로드한 파일이 정상 ZIP이 아니거나 너무 큽니다.")
        part.update(state="complete", sha256=file_hash(path))
        atomic_json(self.manifest_path, self.data)

    def paths(self):
        paths = [self.existing(key) for key in self.keys]
        if any(path is None for path in paths):
            raise mismatch("다운로드하지 않은 분할 파일이 있습니다.")
        return sorted(paths, key=lambda path: int(path.stem.split("-")[1]))
