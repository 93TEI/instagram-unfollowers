"""Durable, account-scoped export jobs. Status and comparison are local-only."""

import fcntl
import logging
import os
import re
import time
import uuid
import shutil
from contextlib import contextmanager
from pathlib import Path

from export_data import ExportError, error_result

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 30 * 60
# preparing is persisted BEFORE provider.request. Only the before_submit marker
# makes a request possibly submitted; an interrupted preparation can start again.
ACTIVE_PHASES = {"submitted", "pending", "uncertain", "downloading", "downloaded"}


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
            "code": job.get("code"), "actions": job.get("actions", []),
            "has_data": current is not None, "saved_at": current.get("saved_at") if current else None,
            "retry_after": max(0, int(job.get("next_check_at", 0) - time.time()) + 1)}


def refresh(store, username, provider, now=None):
    """Only this explicit action touches the provider. Never automatically polls."""
    now = time.time() if now is None else now
    with account_lock(store) as locked:
        if not locked:
            return {"status": "busy", "message": "내보내기 작업이 진행 중입니다."}
        job = store.read_state()
        if job.get("remote_blocked"):
            return {"status": "error", "code": "submission_unknown", "message": job["message"], "actions": ["import", "compare"]}
        if job.get("phase") == "downloaded":
            try:
                return _import_download(store, username, job)
            except (ExportError, OSError) as exc:
                info = error_result(exc)
                job.update({key: info[key] for key in ("code", "message", "actions")})
                store.write_state(job)
                return info
        remaining = job.get("next_check_at", 0) - now
        if remaining > 0:
            return {"status": "pending", "message": job.get("message") or "같은 요청의 반복을 막기 위해 잠시 기다립니다.",
                    "retry_after": int(remaining) + 1}
        continuing = job.get("phase") in ACTIVE_PHASES or "submitted_at" in job
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
            if not isinstance(exc, (ExportError, OSError)):
                exc = ExportError("내보내기 처리 중 오류가 발생했습니다. 요청을 자동 반복하지 않습니다.", code="export_failed", actions=("check_export", "import"))
            info = error_result(exc)
            if phase == "error":
                info["actions"] = list(dict.fromkeys(info["actions"] + ["retry_preparation"]))
            job.update({"phase": phase, **{key: info[key] for key in ("message", "code", "actions")}})
            store.write_state(job)
            logger.warning("export_failed phase=%s error_type=%s", phase, type(exc).__name__)
            return {**info, "retry_after": CHECK_INTERVAL}


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


def recover(store, action):
    """Local-only repair. An ambiguous/corrupt submission record is never reset."""
    with account_lock(store) as locked:
        if not locked:
            return {"status": "busy", "message": "내보내기 작업이 진행 중입니다."}
        try:
            job = store.read_state()
        except ExportError as exc:
            if action != "recover_record" or exc.code != "job_record_invalid":
                return error_result(exc)
            directory = store.directory / "recovery"
            directory.mkdir(exist_ok=True, mode=0o700)
            backup = directory / ("job-" + uuid.uuid4().hex + ".json")
            shutil.copyfile(store.directory / "job.json", backup)
            backup.chmod(0o600)
            store.write_state({"phase": "uncertain", "remote_blocked": True,
                               "code": "submission_unknown", "actions": ["import", "compare"],
                               "message": "손상된 작업 기록을 보관하고 복구했습니다. 제출 여부를 확인할 수 없어 온라인 요청은 막았습니다. 공식 파일을 직접 가져오거나 기존 자료를 비교해주세요."})
            return {"status": "ok", "message": "기존 기록을 보관했습니다. 로컬 파일 가져오기와 비교를 이용해주세요."}
        if action == "retry_preparation" and job.get("phase") in ("preparing", "error") and "submitted_at" not in job and not job.get("remote_blocked"):
            store.write_state({"phase": "idle", "message": "제출 전 준비를 복구했습니다. JSON 갱신을 눌러 다시 시작할 수 있습니다."})
            return {"status": "ok", "message": "제출 전 준비를 복구했습니다."}
        return {"status": "error", "code": "resubmit_blocked", "actions": ["import", "compare"],
                "message": "제출 기록이 있거나 제출 여부가 불명확해 초기화하지 않았습니다. 기존 자료 또는 직접 받은 파일을 사용해주세요."}
