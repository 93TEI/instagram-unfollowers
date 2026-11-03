"""Preview and confirm cleanup of selected local data; preserve export job markers."""
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import data_library
import export_data
from export_data import ExportError, ExportStore
from export_jobs import ACTIVE_PHASES, account_lock


def safe_store(source):
    store = ExportStore(source)
    if store.directory.is_symlink() or store.directory.resolve().parent != export_data.DATA_ROOT.resolve():
        raise ExportError("자료 경로가 변경되어 정리하지 않았습니다.", code="unsafe_path")
    return store


def tree_signature(path, relative):
    """Include every file's size/mtime/inode so stale previews cannot delete new data."""
    if path.is_symlink():
        raise ExportError("연결된 파일이 있어 정리하지 않았습니다.", code="unsafe_path")
    result = []
    candidates = [path] + (sorted(path.rglob("*")) if path.is_dir() else [])
    for item in candidates:
        if item.is_symlink():
            raise ExportError("자료 안에 연결된 파일이 있어 정리하지 않았습니다.", code="unsafe_path")
        stat = item.stat()
        result.append([relative + ("/" + item.relative_to(path).as_posix() if item != path else ""),
                       stat.st_size, stat.st_mtime_ns, stat.st_ino, item.is_dir()])
    return result


def _plan(store, mode):
    if mode not in ("cleanup", "delete_data"):
        raise ExportError("알 수 없는 자료 관리 작업입니다.")
    try:
        current = store.metadata()
    except ExportError:
        if mode == "cleanup":
            raise ExportError("현재 자료를 확인할 수 없어 정리하지 않았습니다. 파일을 다시 가져오거나 자료 삭제 범위를 확인해주세요.", code="data_record_invalid")
        current = None
    try:
        job = store.read_state()
        active = job.get("phase") in ACTIVE_PHASES or "submitted_at" in job
        protected_generation = job.get("generation") if active else None
        if not isinstance(protected_generation, str) or not re.fullmatch(r"[0-9a-f]{32}", protected_generation):
            protected_generation = None
    except ExportError:
        job, active, protected_generation = {"unreadable": True}, True, None
    targets = []
    for parent_name in ("snapshots", "downloads"):
        parent = store.directory / parent_name
        if parent.is_symlink():
            raise ExportError("자료 경로가 연결된 폴더로 변경되었습니다.", code="unsafe_path")
        if not parent.exists():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or child.is_symlink():
                if child.is_symlink():
                    raise ExportError("자료 안에 연결된 폴더가 있습니다.", code="unsafe_path")
                continue
            if parent_name == "snapshots":
                if not re.fullmatch(r"[0-9a-f]{64}", child.name):
                    continue
                if mode == "cleanup" and current and child.name == current["snapshot"]:
                    continue
            else:
                if not re.fullmatch(r"[0-9a-f]{32}", child.name):
                    continue
                if active and (not protected_generation or child.name == protected_generation):
                    continue
            targets.append(child)
    if mode == "delete_data" and (store.directory / "current.json").exists():
        targets.append(store.directory / "current.json")
    if store.directory.exists():
        targets.extend(path for path in sorted(store.directory.iterdir()) if re.fullmatch(r"\.trash-[0-9a-f]{32}", path.name))
    signatures, files, total = [], [], 0
    for path in targets:
        relative = path.relative_to(store.directory).as_posix()
        signature = tree_signature(path, relative)
        size = sum(item[1] for item in signature if not item[4])
        signatures.extend(signature)
        files.append({"path": relative, "bytes": size})
        total += size
    payload = {"source": store.directory.name, "mode": mode, "entries": signatures,
               "current": current, "job": job}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"status": "ok", "source": store.directory.name, "mode": mode, "token": token,
            "files": files, "total_bytes": total,
            "preserved": ["로그인 세션", "내보내기 작업 기록", "복구용 보관 기록"] + (["진행 중인 내보내기 다운로드"] if active else []),
            "message": "정리는 현재 정상 자료를 유지합니다." if mode == "cleanup" else "선택한 자료의 스냅샷과 사용하지 않는 다운로드를 삭제합니다."}


def preview(source, mode):
    store = safe_store(source)
    with account_lock(store) as locked:
        if not locked:
            return {"status": "busy", "message": "자료 작업이 진행 중입니다."}
        return _plan(store, mode)


def execute(source, mode, token, confirmed):
    if not confirmed:
        raise ExportError("삭제 범위를 확인하고 확인란을 선택해주세요.", code="confirmation_required")
    store = safe_store(source)
    with account_lock(store) as locked:
        if not locked:
            return {"status": "busy", "message": "자료 작업이 진행 중입니다."}
        if data_library.selected_store().directory.name != source:
            raise ExportError("선택한 자료가 바뀌었습니다. 삭제 범위를 다시 확인해주세요.", code="stale_preview")
        plan = _plan(store, mode)
        if token != plan["token"]:
            raise ExportError("미리보기 이후 자료가 변경되었습니다. 새 범위를 확인해주세요.", code="stale_preview")
        if not plan["files"]:
            return {"status": "ok", "message": "정리할 자료가 없습니다.", "removed": 0}
        trash = store.directory / (".trash-" + uuid.uuid4().hex)
        trash.mkdir(mode=0o700)
        moved = []
        try:
            for index, item in enumerate(plan["files"]):
                path = store.directory / item["path"]
                target = trash / str(index)
                os.replace(path, target)
                moved.append((path, target))
            if mode == "delete_data":
                data_library.select_source("local")
        except OSError:
            for path, target in reversed(moved):
                os.replace(target, path)
            raise
        try:
            shutil.rmtree(trash)
            message = "자료 정리 완료" if mode == "cleanup" else "선택한 자료를 삭제했습니다. 내보내기 작업 기록은 유지했습니다."
        except OSError:
            message = "자료를 목록에서 제거했지만 임시 보관 파일을 지우지 못했습니다. 저장 폴더의 .trash 기록을 확인해주세요."
        return {"status": "ok", "message": message, "removed": len(moved)}
