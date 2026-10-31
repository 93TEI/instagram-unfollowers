"""Durable, account-scoped export jobs. Status and comparison are local-only."""

import fcntl
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from export_data import ExportError

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 30 * 60
ACTIVE_PHASES = {"preparing", "submitted", "pending", "uncertain", "downloading", "downloaded"}


@contextmanager
def account_lock(store):
    store.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store.directory / ".lock").open("a") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def status(store):
    job = store.read_state()
    current = store.metadata()
    return {"phase": job.get("phase", "idle"), "message": job.get("message", ""),
            "has_data": current is not None, "saved_at": current.get("saved_at") if current else None,
            "retry_after": max(0, int(job.get("next_check_at", 0) - time.time()) + 1)}


def refresh(store, username, provider, now=None):
    """Only this explicit action touches the provider. Never automatically polls."""
    now = time.time() if now is None else now
    with account_lock(store) as locked:
        if not locked:
            return {"status": "busy", "message": "내보내기 작업이 진행 중입니다."}
        job = store.read_state()
        if job.get("phase") == "downloaded":
            return _import_download(store, username, job)
        remaining = job.get("next_check_at", 0) - now
        if remaining > 0:
            return {"status": "pending", "message": job.get("message") or "같은 요청의 반복을 막기 위해 잠시 기다립니다.",
                    "retry_after": int(remaining) + 1}
        continuing = job.get("phase") in ACTIVE_PHASES
        if not continuing:
            job = {"generation": uuid.uuid4().hex}
        job.update({"phase": "pending" if continuing else "preparing", "last_attempt_at": now,
                    "next_check_at": now + CHECK_INTERVAL, "message": "내보내기 화면 확인 중입니다."})
        store.write_state(job)  # Also blocks repeat attempts if the process is killed.

        def before_submit(existing_exports=None):
            job.update({"phase": "submitted", "submitted_at": now,
                        "existing_exports": existing_exports or [],
                        "message": "내보내기를 요청했습니다. 준비 확인은 30분 뒤부터 가능합니다."})
            store.write_state(job)

        try:
            if not continuing:
                provider.request(before_submit)
                if job["phase"] != "submitted":
                    raise ExportError("내보내기 제출 기록을 확인하지 못했습니다.")
                job["phase"] = "pending"
                store.write_state(job)
                return {"status": "pending", "message": job["message"], "retry_after": CHECK_INTERVAL}
            downloads = _download_directory(store, job)
            downloads.mkdir(parents=True, exist_ok=True, mode=0o700)

            def on_download(paths):
                job.update({"phase": "downloaded", "files": [Path(p).name for p in paths]})
                store.write_state(job)

            paths = provider.download(downloads, on_download, job.get("existing_exports", []))
            if paths:
                if job["phase"] != "downloaded":
                    raise ExportError("다운로드 완료 기록을 확인하지 못했습니다.")
                return _import_download(store, username, job)
            job.update({"phase": "pending", "message": "인스타그램에서 JSON을 준비 중입니다. 나중에 준비 확인을 눌러주세요."})
            store.write_state(job)
            return {"status": "pending", "message": job["message"], "retry_after": CHECK_INTERVAL}
        except Exception as exc:
            # A timeout after submission must never become a fresh request on retry.
            if job["phase"] == "downloaded":
                phase = "downloaded"
            elif continuing or job["phase"] == "submitted":
                phase = "uncertain"
            else:
                phase = "error"
            message = str(exc) if isinstance(exc, ExportError) else "내보내기 처리 중 오류가 발생했습니다. 요청을 자동 반복하지 않습니다."
            job.update({"phase": phase, "message": message})
            store.write_state(job)
            logger.warning("export_failed phase=%s error_type=%s", phase, type(exc).__name__)
            return {"status": "error", "message": message, "retry_after": CHECK_INTERVAL}


def _import_download(store, username, job):
    names = job.get("files", [])
    if not names or any(not isinstance(n, str) or not re.fullmatch(r"part-[1-9][0-9]*\.zip", n) for n in names):
        raise ExportError("다운로드 파일 기록이 올바르지 않습니다.")
    result = store.import_sources([_download_directory(store, job) / n for n in names], username)
    store.write_state({"phase": "ready", "message": "JSON 저장과 비교가 완료되었습니다.",
                       "next_check_at": job.get("next_check_at", 0)})
    return result


def _download_directory(store, job):
    generation = job.get("generation", "")
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ExportError("내보내기 작업 식별자를 확인할 수 없습니다.")
    return store.directory / "downloads" / generation
