"""Bounded, temporary local uploads; no Instagram calls."""
import tempfile
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse
from starlette.formparsers import MultiPartException
from starlette.exceptions import HTTPException

from export_data import ExportError, RELATION_FILE
from export_jobs import account_lock
from state import finish_job, try_start_job

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_FILES = 100


class UploadTooLarge(MultiPartException):
    pass


class UploadLimitMiddleware:
    """Bound the multipart stream before the parser can spool it to disk."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/import":
            return await self.app(scope, receive, send)
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > MAX_UPLOAD_BYTES:
                raise UploadTooLarge("upload_too_large")
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except UploadTooLarge:
            await JSONResponse({"status": "error", "code": "upload_too_large",
                                "message": "가져올 파일의 합계가 100MB를 초과했습니다."}, status_code=413)(scope, receive, send)


async def import_files(request, store, username):
    if not try_start_job():
        return {"status": "busy", "message": "다른 작업이 진행 중입니다."}
    try:
        with account_lock(store) as locked:
            if not locked:
                return {"status": "busy", "message": "이 자료의 다른 작업이 진행 중입니다."}
            async with request.form(max_files=MAX_UPLOAD_FILES, max_fields=4) as form:
                uploads = form.getlist("files")
                if not uploads or any(not isinstance(item, UploadFile) for item in uploads):
                    raise ExportError("ZIP 또는 JSON 파일을 선택해주세요.")
                names, kinds = set(), set()
                with tempfile.TemporaryDirectory(prefix="instagram-import-") as directory:
                    paths = []
                    for index, upload in enumerate(uploads):
                        name = upload.filename or ""
                        if not name or len(name) > 255 or "/" in name or "\\" in name or "\x00" in name:
                            raise ExportError("파일 이름이나 경로가 올바르지 않습니다.")
                        if name in names:
                            raise ExportError("같은 이름의 파일이 중복 선택되었습니다.")
                        names.add(name)
                        if RELATION_FILE.fullmatch(name):
                            kinds.add("json")
                            path = Path(directory) / name
                        elif name.lower().endswith(".zip"):
                            kinds.add("zip")
                            path = Path(directory) / f"archive-{index}.zip"
                        else:
                            raise ExportError("내보낸 ZIP 또는 followers/following JSON만 선택하세요.")
                        if len(kinds) > 1:
                            raise ExportError("ZIP과 낱개 JSON을 섞지 말고 한 번의 내보내기만 선택하세요.")
                        with path.open("wb") as target:
                            path.chmod(0o600)
                            while True:
                                chunk = await upload.read(1024 * 1024)
                                if not chunk:
                                    break
                                target.write(chunk)
                        paths.append(path)
                    return await run_in_threadpool(store.import_sources, paths, username)
    except ExportError as exc:
        return {"status": "error", "code": "invalid_export", "message": str(exc)}
    except HTTPException as exc:
        if exc.detail == "upload_too_large":
            return JSONResponse({"status": "error", "code": "upload_too_large",
                                 "message": "가져올 파일의 합계가 100MB를 초과했습니다."}, status_code=413)
        return JSONResponse({"status": "error", "code": "invalid_upload",
                             "message": "파일 업로드 형식이나 개수 제한을 확인해주세요."}, status_code=400)
    except OSError:
        return {"status": "error", "code": "storage_error", "message": "파일을 읽거나 저장하지 못했습니다."}
    finally:
        finish_job()
