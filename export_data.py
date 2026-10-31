"""Read Instagram relationship exports locally; never contacts Instagram."""

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from datetime import datetime
from urllib.parse import unquote, urlparse

DATA_ROOT = Path(__file__).resolve().parent / "exports"
MAX_JSON_BYTES = 100 * 1024 * 1024
RELATION_FILE = re.compile(r"^(followers|following)(?:_([1-9][0-9]*))?\.json$")
USERNAME = re.compile(r"^[a-zA-Z0-9_.]{1,30}$")
COMPLETENESS_NOTE = "마지막 분할 파일 누락, 전체 기간 및 동일 계정 여부는 JSON만으로 확인할 수 없습니다. 한 번의 내보내기에 속한 모든 파일인지 확인해주세요."


class ExportError(ValueError):
    pass


def parse_export_date(value):
    if value in (None, ""):
        return None
    try:
        if not isinstance(value, str):
            raise ValueError()
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
        timestamp = date.timestamp()
        if timestamp < 0 or timestamp > time.time() + 300:
            raise ValueError()
        return timestamp
    except (ValueError, OverflowError) as exc:
        raise ExportError("내보내기 생성 시각을 확인해주세요. 시간대가 포함된 날짜여야 하며 미래 시각은 사용할 수 없습니다.") from exc


def snapshot_times(metadata):
    return {"saved_at": metadata.get("saved_at"),
            "imported_at": metadata.get("imported_at"),
            "exported_at": metadata.get("exported_at"),
            "exported_at_source": metadata.get("exported_at_source", "unknown")}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_usernames(data, relation):
    """Support list exports and relationships_following/followers wrappers."""
    if isinstance(data, dict):
        data = data.get("relationships_" + relation)
    if not isinstance(data, list):
        raise ExportError(f"{relation} JSON의 목록 형식을 확인할 수 없습니다.")
    result = set()
    for entry in data:
        if not isinstance(entry, dict):
            raise ExportError("JSON에 올바르지 않은 계정 항목이 있습니다.")
        items = entry.get("string_list_data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ExportError("JSON의 string_list_data 형식을 확인할 수 없습니다.")
        item = items[0]
        candidate = item.get("value") or entry.get("title")
        if not candidate:
            href = item.get("href", "")
            if not isinstance(href, str):
                raise ExportError("JSON의 프로필 주소 형식이 올바르지 않습니다.")
            try:
                parsed = urlparse(href)
            except ValueError as exc:
                raise ExportError("JSON의 프로필 주소 형식이 올바르지 않습니다.") from exc
            if parsed.scheme != "https" or parsed.hostname not in ("instagram.com", "www.instagram.com"):
                raise ExportError("JSON에서 아이디를 찾을 수 없습니다.")
            parts = unquote(parsed.path).strip("/").split("/")
            if len(parts) == 2 and parts[0] == "_u":
                parts = parts[1:]
            candidate = parts[0] if len(parts) == 1 else None
        if not isinstance(candidate, str):
            raise ExportError("JSON에서 아이디를 찾을 수 없습니다.")
        candidate = candidate.strip().lstrip("@").lower()
        if not USERNAME.fullmatch(candidate):
            raise ExportError("JSON에 올바르지 않은 인스타그램 아이디가 있습니다.")
        result.add(candidate)
    return result


def _validate_files(files):
    for relation in ("followers", "following"):
        names = [name for name in files if RELATION_FILE.fullmatch(name).group(1) == relation]
        if not names:
            raise ExportError(f"{relation} JSON 파일이 없습니다. 팔로워와 팔로잉을 함께 내보내세요.")
        numbers = [RELATION_FILE.fullmatch(name).group(2) for name in names]
        if None in numbers:
            if len(numbers) != 1:
                raise ExportError(f"{relation} 단일 파일과 분할 파일이 섞여 있습니다.")
        else:
            numbers = sorted(int(n) for n in numbers)
            if numbers != list(range(1, len(numbers) + 1)):
                raise ExportError(f"{relation} 분할 파일이 누락되었습니다. 모든 파일을 함께 가져오세요.")


def compare_files(files):
    _validate_files(files)
    groups = {"following": set(), "followers": set()}
    inspected = []
    for name, content in files.items():
        relation = RELATION_FILE.fullmatch(name).group(1)
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ExportError(f"{name} 파일을 JSON으로 읽을 수 없습니다.") from exc
        names = parse_usernames(data, relation)
        groups[relation].update(names)
        rows = data.get("relationships_" + relation) if isinstance(data, dict) else data
        inspected.append({"name": name, "relation": relation, "records": len(rows), "unique_accounts": len(names)})
    users = [{"username": name} for name in sorted(groups["following"] - groups["followers"])]
    return {"status": "ok", "count": len(users), "users": users,
            "following_count": len(groups["following"]), "followers_count": len(groups["followers"]),
            "validation": {"format": "passed", "numbering": "passed", "completeness": "unverified",
                           "files": sorted(inspected, key=lambda item: item["name"]), "note": COMPLETENESS_NOTE}}


def fingerprint(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def read_sources(paths):
    """One export, possibly multiple ZIP parts. Never extract arbitrary ZIP paths."""
    files, parents, size = {}, set(), 0

    def add(name, length, read):
        nonlocal size
        path = PurePosixPath(name)
        if not RELATION_FILE.fullmatch(path.name):
            return
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ExportError("내보내기 파일 경로가 올바르지 않습니다.")
        size += length
        if size > MAX_JSON_BYTES:
            raise ExportError("JSON 파일 용량이 허용 범위를 초과했습니다.")
        if path.name in files:
            raise ExportError("중복된 목록 파일이 있습니다. 한 번의 내보내기만 선택하세요.")
        parents.add(str(path.parent))
        files[path.name] = read()

    for source in paths:
        source = Path(source)
        if source.is_dir():
            for path in sorted(source.rglob("*.json")):
                if path.is_symlink():
                    raise ExportError("심볼릭 링크는 가져올 수 없습니다.")
                add(path.relative_to(source).as_posix(), path.stat().st_size, path.read_bytes)
        elif RELATION_FILE.fullmatch(source.name):
            if source.is_symlink():
                raise ExportError("심볼릭 링크는 가져올 수 없습니다.")
            add(source.name, source.stat().st_size, source.read_bytes)
        elif zipfile.is_zipfile(source):
            try:
                with zipfile.ZipFile(source) as archive:
                    for entry in archive.infolist():
                        if not entry.is_dir():
                            add(entry.filename, entry.file_size, lambda e=entry: archive.read(e))
            except (zipfile.BadZipFile, RuntimeError) as exc:
                raise ExportError("ZIP 파일을 읽을 수 없습니다. 다운로드 완료 여부를 확인하세요.") from exc
        else:
            raise ExportError("내보낸 ZIP 파일 또는 JSON이 들어 있는 폴더를 지정하세요.")
    if len(parents) > 1:
        raise ExportError("여러 계정 또는 다른 내보내기의 목록이 섞여 있습니다.")
    _validate_files(files)
    return files


class ExportStore:
    def __init__(self, account_id, root=None):
        if account_id != "local" and not re.fullmatch(r"[0-9]+", str(account_id or "")):
            raise ExportError("로그인 계정 정보를 확인할 수 없습니다.")
        self.directory = Path(root if root is not None else DATA_ROOT) / str(account_id)

    def read_state(self):
        try:
            data = json.loads((self.directory / "job.json").read_text(encoding="utf-8"))
            phases = {"idle", "preparing", "submitted", "pending", "uncertain", "downloading", "downloaded", "ready", "error"}
            if not isinstance(data, dict) or not isinstance(data.get("phase"), str) or data["phase"] not in phases:
                raise ValueError()
            for key in ("next_check_at", "last_attempt_at", "submitted_at"):
                if key in data and (not isinstance(data[key], (int, float)) or not math.isfinite(data[key]) or data[key] < 0):
                    raise ValueError()
            if "existing_exports" in data and (not isinstance(data["existing_exports"], list) or
                                                any(not isinstance(k, str) for k in data["existing_exports"])):
                raise ValueError()
            return data
        except FileNotFoundError:
            return {"phase": "idle"}
        except (ValueError, UnicodeError) as exc:
            # Never silently reset a corrupt request marker and submit twice.
            raise ExportError("내보내기 작업 기록을 읽을 수 없습니다. 중복 요청을 막기 위해 중단했습니다.") from exc

    def write_state(self, data):
        atomic_json(self.directory / "job.json", data)

    def import_sources(self, paths, username, exported_at=None):
        export_date = parse_export_date(exported_at)
        files = read_sources(paths)
        result = compare_files(files)  # Validate everything before replacing a good snapshot.
        snapshot = fingerprint(files)
        snapshots = self.directory / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = snapshots / snapshot
        if target.exists():
            if fingerprint(read_sources([target])) != snapshot:
                raise ExportError("이전에 저장한 동일 JSON이 변경되었습니다. 손상된 파일을 재사용하지 않습니다.")
        else:
            temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=snapshots))
            try:
                for name, content in files.items():
                    path = temporary / name
                    path.write_bytes(content)
                    path.chmod(0o600)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        imported_at = time.time()
        metadata = {"snapshot": snapshot, "saved_at": imported_at, "imported_at": imported_at,
                    "exported_at": export_date, "exported_at_source": "user" if export_date is not None else "unknown", "username": username,
                    "files": sorted(files), "following_count": result["following_count"],
                    "followers_count": result["followers_count"], "validation": result["validation"]}
        previous = self.metadata()
        if previous and previous.get("snapshot") == snapshot:
            metadata["saved_at"] = previous["saved_at"]
        atomic_json(self.directory / "current.json", metadata)
        return {**result, **snapshot_times(metadata)}

    def metadata(self):
        try:
            data = json.loads((self.directory / "current.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("snapshot", ""))):
                raise ValueError()
            if not isinstance(data.get("saved_at"), (int, float)) or not math.isfinite(data["saved_at"]):
                raise ValueError()
            for key in ("imported_at", "exported_at"):
                if data.get(key) is not None and (not isinstance(data[key], (int, float)) or not math.isfinite(data[key]) or data[key] < 0):
                    raise ValueError()
            if not isinstance(data.get("files"), list) or not data["files"] or any(
                not isinstance(name, str) or not RELATION_FILE.fullmatch(name) for name in data["files"]
            ):
                raise ValueError()
            return data
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as exc:
            raise ExportError("저장된 JSON의 정보를 읽을 수 없습니다.") from exc

    def compare(self):
        metadata = self.metadata()
        if metadata is None:
            return {"status": "no_data", "message": "저장된 JSON이 없습니다. ZIP·JSON 가져오기로 파일을 선택하세요."}
        directory = self.directory / "snapshots" / metadata["snapshot"]
        try:
            files = read_sources([directory])
        except OSError as exc:
            raise ExportError("저장된 JSON 파일을 읽을 수 없습니다.") from exc
        if sorted(files) != metadata["files"] or fingerprint(files) != metadata["snapshot"]:
            raise ExportError("저장된 JSON 파일이 누락되거나 변경되었습니다.")
        result = compare_files(files)
        return {**result, **snapshot_times(metadata)}
