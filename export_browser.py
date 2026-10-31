"""Accounts Center UI adapter. No private follower/profile endpoints or retries.

No undocumented export endpoint is assumed. Use visible controls, verify selections
before submitting, and stop if the UI no longer matches.
"""

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from export_data import ExportError, atomic_json

EXPORT_URL = "https://accountscenter.instagram.com/info_and_permissions/dyi/"
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class ExportUIError(ExportError):
    pass


class ExportAuthRequired(ExportUIError):
    pass


def label(*names):
    return re.compile(r"^(?:" + "|".join(re.escape(n) for n in names) + r")$", re.I)


def block_request(request):
    path = urlparse(request.url).path
    return (request.resource_type in ("image", "media", "font")
            or bool(re.search(r"/friendships/|/users/[^/]+/(?:info|usernameinfo)/|/web_profile_info/", path)))


class ExportPage:
    """Each action is attempted once; locator waits do not resubmit network requests."""

    def __init__(self, page, username, password=None):
        self.page, self.username, self.password = page, username, password

    def scope(self):
        dialogs = self.page.get_by_role("dialog").filter(visible=True)
        return dialogs.last if dialogs.count() else self.page

    def control(self, names, scope=None):
        scope = scope if scope is not None else self.scope()
        pattern = label(*names)
        # Wait for the transition locally; never reload/retry the page.
        roles = scope.get_by_role("button", name=pattern).or_(scope.get_by_role("link", name=pattern)).or_(
            scope.get_by_role("radio", name=pattern))
        text = scope.get_by_text(pattern)
        roles.or_(text).filter(visible=True).first.wait_for(state="visible")
        found = roles.filter(visible=True)
        if found.count() == 1:
            return found
        if found.count() > 1:
            raise ExportUIError("내보내기 화면의 항목을 하나로 식별할 수 없어 중단했습니다.")
        # A visible text label inside a custom Meta control is the final fallback.
        found = text.filter(visible=True)
        if found.count() == 1:
            return found
        raise ExportUIError("공식 내보내기 화면 구성이 예상과 다릅니다. 자동 요청을 중단했습니다.")

    def click(self, *names):
        self.control(names).click()

    def wait_text(self, *names):
        self.scope().get_by_text(label(*names)).first.wait_for(state="visible")

    def authenticate(self):
        """Only reauthentication on Accounts Center; never starts a second login flow."""
        if urlparse(self.page.url).hostname != "accountscenter.instagram.com":
            raise ExportAuthRequired("기존 세션으로 계정 센터에 접근하지 못했습니다. 앱에서 다시 로그인해주세요.")
        field = self.page.locator('input[type="password"]:visible')
        if field.count():
            if not self.password:
                raise ExportAuthRequired("내보내기 확인에 비밀번호가 필요합니다. 앱에서 다시 로그인한 뒤 준비 확인을 누르세요.")
            field.fill(self.password)
            self.click("Continue", "계속", "Submit", "제출")
            field.wait_for(state="hidden")
        if self.page.locator('input[autocomplete="one-time-code"]:visible').count():
            raise ExportAuthRequired("계정 센터에서 추가 인증이 필요합니다. 공식 인스타그램에서 인증을 완료해주세요.")

    def open(self):
        self.page.goto(EXPORT_URL, wait_until="domcontentloaded", timeout=45000)
        # Wait for a known UI state rather than a fixed sleep or repeated refreshes.
        self.page.get_by_text(re.compile(
            r"Create export|내보내기 만들기|Export your information|내 정보 내보내기|정보 내보내기|Log in|로그인", re.I
        )).first.wait_for(state="visible")
        self.authenticate()

    def _choose_account(self):
        self.wait_text("Select profiles", "Select your profiles", "프로필 선택")
        scope = self.scope()
        boxes = scope.get_by_role("checkbox")
        if boxes.count():
            target = scope.get_by_role("checkbox", name=re.compile(
                r"(?<![\w.])@?" + re.escape(self.username) + r"(?![\w.])", re.I))
            if target.count() != 1:
                raise ExportUIError("내보낼 로그인 계정을 하나로 확인할 수 없습니다.")
            for box in boxes.all():
                if box.is_checked():
                    box.uncheck()
            target.check()
            self.click("Next", "다음")
        else:
            # Some Accounts Center versions immediately select a single profile.
            self.click(self.username, "@" + self.username)

    def _configure(self):
        self.wait_text("Export to device", "기기로 내보내기")
        self.click("Export to device", "기기로 내보내기")
        self.wait_text("Customize information", "정보 맞춤 설정", "정보 맞춤설정")
        self.click("Customize information", "정보 맞춤 설정", "정보 맞춤설정")
        self.wait_text("Followers and following", "팔로워 및 팔로잉", "팔로워와 팔로잉")
        boxes = self.scope().get_by_role("checkbox")
        target = self.scope().get_by_role("checkbox", name=label(
            "Followers and following", "팔로워 및 팔로잉", "팔로워와 팔로잉"))
        if target.count() != 1 or not boxes.count():
            raise ExportUIError("팔로워·팔로잉만 선택되었는지 검증할 수 없어 중단했습니다.")
        # Clear every selected category before enabling the one needed category.
        for box in boxes.all():
            if box.is_checked():
                box.uncheck()
        target.check()
        checked = [box for box in boxes.all() if box.is_checked()]
        if len(checked) != 1 or not target.is_checked():
            raise ExportUIError("내보내기 정보 선택을 확인할 수 없습니다.")
        self.click("Save", "저장")
        self.wait_text("Date range", "기간", "날짜 범위")
        self.click("Date range", "기간", "날짜 범위")
        self.wait_text("All time", "전체 기간")
        self.click("All time", "전체 기간")
        self.click("Save", "저장")
        self.wait_text("Format", "형식")
        self.click("Format", "형식")
        self.wait_text("JSON")
        self.click("JSON")
        self.click("Save", "저장")
        self.wait_text("Start export", "내보내기 시작")
        text = self.scope().inner_text() if self.scope() is not self.page else self.page.locator("body").inner_text()
        if not re.search(r"\bJSON\b", text) or not re.search(r"All time|전체 기간", text, re.I):
            raise ExportUIError("JSON 형식과 전체 기간 설정을 확인할 수 없어 제출하지 않았습니다.")
        if not re.search(r"Followers and following|팔로워 및 팔로잉|팔로워와 팔로잉", text, re.I):
            raise ExportUIError("내보내기 요약에서 팔로워·팔로잉 선택을 확인할 수 없습니다.")

    def request(self, before_submit):
        self.open()
        if self.page.get_by_text(re.compile(r"In progress|Preparing|진행 중|준비 중", re.I)).count():
            raise ExportUIError("계정 센터에 이미 진행 중인 내보내기가 있습니다. 중복 제출하지 않았습니다.")
        existing_exports = [self.export_key(card) for card in self.cards().all()]
        self.wait_text("Create export", "내보내기 만들기")
        self.click("Create export", "내보내기 만들기")
        self._choose_account()
        self._configure()
        before_submit(existing_exports)  # Durable marker BEFORE the one and only submit click.
        self.click("Start export", "내보내기 시작")
        self.authenticate()
        self.page.get_by_text(re.compile(
            r"In progress|Preparing|export.*started|export.*requested|진행 중|준비 중|내보내기.*시작|내보내기.*요청", re.I
        )).first.wait_for(state="visible")

    def cards(self):
        # Only an unambiguous export card belonging to the logged-in account.
        # Do not choose the first download on an accounts center with multiple exports.
        candidates = self.page.get_by_role("button").filter(
            has_text=re.compile(r"(?<![\w.])@?" + re.escape(self.username) + r"(?![\w.])", re.I)
        ).filter(has_text=re.compile(r"Download|다운로드", re.I))
        links = self.page.get_by_role("link").filter(
            has_text=re.compile(r"(?<![\w.])@?" + re.escape(self.username) + r"(?![\w.])", re.I)
        ).filter(has_text=re.compile(r"Download|다운로드", re.I))
        return candidates.or_(links).filter(visible=True)

    @staticmethod
    def export_key(card):
        # No authenticated download URLs are written to disk or logs.
        return hashlib.sha256(card.inner_text().strip().encode()).hexdigest()

    def download(self, destination, on_download, existing_exports):
        self.open()
        # Do not mistake an older ready export for the newly requested one.
        if self.page.get_by_text(re.compile(r"In progress|Preparing|진행 중|준비 중", re.I)).count():
            return None
        candidates = [card for card in self.cards().all() if self.export_key(card) not in existing_exports]
        if not candidates:
            raise ExportUIError("이 계정의 준비된 내보내기를 확인하지 못했습니다. 새 요청은 만들지 않았습니다.")
        if len(candidates) != 1:
            raise ExportUIError("이 계정에 여러 내보내기가 있어 자동으로 하나를 고르지 않았습니다.")
        export_key = self.export_key(candidates[0])
        candidates[0].click()
        self.authenticate()
        self.wait_text("Download", "Download files", "다운로드", "파일 다운로드")
        scope = self.scope()
        buttons = scope.get_by_role("button", name=label("Download", "Download file", "다운로드", "파일 다운로드"))
        links = scope.get_by_role("link", name=label("Download", "Download file", "다운로드", "파일 다운로드"))
        controls = buttons.or_(links).filter(visible=True)
        if not controls.count() or controls.count() > 100:
            raise ExportUIError("다운로드할 파일 목록을 확인할 수 없습니다.")
        manifest_path = Path(destination) / "transfer.json"
        manifest = {"export": export_key, "count": controls.count(), "parts": {}}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
                if manifest["export"] != export_key or manifest["count"] != controls.count() or not isinstance(manifest["parts"], dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ExportUIError("이전 다운로드와 현재 내보내기가 일치하지 않습니다. 파일을 섞지 않고 중단했습니다.") from None
        paths = []
        for index, control in enumerate(controls.all()):
            path = Path(destination) / f"part-{index + 1}.zip"
            if path.name in manifest["parts"]:
                if not path.exists() or file_hash(path) != manifest["parts"][path.name]:
                    raise ExportUIError("이미 받은 파일이 변경되거나 없어졌습니다. 자동으로 다시 다운로드하지 않습니다.")
                paths.append(path)
                continue
            with self.page.expect_download(timeout=120000) as event:
                control.click()
            download = event.value
            download.save_as(path)
            path.chmod(0o600)
            if path.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise ExportUIError("내보내기 파일이 너무 큽니다. 팔로워·팔로잉만 포함한 내보내기를 사용하세요.")
            manifest["parts"][path.name] = file_hash(path)
            atomic_json(manifest_path, manifest)
            paths.append(path)
        on_download(paths)
        return paths


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BrowserExporter:
    def __init__(self, sessionid, username, password=None):
        self.sessionid, self.username, self.password = sessionid, username, password

    @contextmanager
    def page(self):
        if not self.sessionid:
            raise ExportAuthRequired("내보내기에 사용할 세션이 없습니다. 다시 로그인해주세요.")
        from playwright.sync_api import sync_playwright, Error
        try:
            with sync_playwright() as playwright:
                # Use installed Chrome when present; otherwise playwright install chromium.
                options = {"headless": True}
                if Path("/Applications/Google Chrome.app").exists():
                    options["channel"] = "chrome"
                browser = playwright.chromium.launch(**options)
                try:
                    context = browser.new_context(locale="en-US", accept_downloads=True, service_workers="block")
                    context.add_cookies([{"name": "sessionid", "value": self.sessionid,
                                         "domain": ".instagram.com", "path": "/", "secure": True, "httpOnly": True}])
                    context.route("**/*", lambda route: route.abort() if block_request(route.request) else route.continue_())
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    yield ExportPage(page, self.username, self.password)
                finally:
                    browser.close()
        except ExportError:
            raise
        except Error:
            # Playwright errors can contain authenticated URLs. Do not expose them.
            raise ExportUIError("내보내기 화면을 처리하지 못했습니다. 화면 변경·추가 인증·브라우저 설치 상태를 확인하세요. 요청은 자동 반복하지 않습니다.") from None

    def request(self, before_submit):
        with self.page() as page:
            page.request(before_submit)

    def download(self, destination, on_download, existing_exports):
        with self.page() as page:
            return page.download(destination, on_download, existing_exports)
