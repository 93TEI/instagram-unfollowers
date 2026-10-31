"""Opt-in browser checks. Every URL is fulfilled locally or aborted.

RUN_OFFLINE_BROWSER_TESTS=1 venv/bin/python -m pytest -q
No Instagram session is loaded, and no app server is started.
"""
import base64
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from export_browser import EXPORT_URL, ExportPage, ExportUIError

pytestmark = pytest.mark.skipif(os.environ.get("RUN_OFFLINE_BROWSER_TESTS") != "1", reason="offline browser checks are opt-in")


@pytest.fixture
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True,
                                    args=["--host-resolver-rules=MAP * ~NOTFOUND"])
        yield browser
        browser.close()


FLOW = r'''<!doctype html><html><body>
<button onclick="accounts()">Create export</button>
<script>
window.submissions = 0;
window.chosen = [];
window.format = 'HTML';
window.range = 'Last year';
function accounts() {
 document.body.innerHTML = '<div role="dialog"><h2>Select profiles</h2><label><input type="checkbox" id="ours">test_account Instagram</label><label><input type="checkbox" id="other" checked>another_account Instagram</label><button onclick="accountNext()">Next</button></div>';
}
function accountNext() {
 if (!document.querySelector('#ours').checked || document.querySelector('#other').checked) throw Error('wrong account');
 document.body.innerHTML = '<div role="dialog"><button onclick="summary()">Export to device</button></div>';
}
function summary() {
 document.body.innerHTML = '<div role="dialog"><h2>Confirm your export</h2><p>test_account</p><button onclick="categories()">Customize information</button><p>' + chosen.join(', ') + '</p><button onclick="dates()">Date range</button><p>' + range + '</p><button onclick="formats()">Format</button><p>' + format + '</p><button onclick="submitExport()">Start export</button></div>';
}
function categories() {
 document.body.innerHTML = '<div role="dialog"><label><input type="checkbox" value="Posts" checked>Posts</label><label><input type="checkbox" value="Messages" checked>Messages</label><label><input type="checkbox" value="Followers and following">Followers and following</label><button onclick="saveCategories()">Save</button></div>';
}
function saveCategories() { chosen = Array.from(document.querySelectorAll('input:checked')).map(e => e.value); summary(); }
function dates() {
 document.body.innerHTML = '<div role="dialog"><label><input type="radio" name="date" value="All time">All time</label><label><input type="radio" name="date" value="Last year" checked>Last year</label><button onclick="range = document.querySelector(\'input:checked\').value; summary()">Save</button></div>';
}
function formats() {
 document.body.innerHTML = '<div role="dialog"><label><input type="radio" name="format" value="JSON">JSON</label><label><input type="radio" name="format" value="HTML" checked>HTML</label><button onclick="format = document.querySelector(\'input:checked\').value; summary()">Save</button></div>';
}
function submitExport() { submissions++; document.body.innerHTML = '<h2>In progress</h2>'; }
</script></body></html>'''


def context_with_page(browser, html):
    context = browser.new_context(service_workers="block", accept_downloads=True)
    calls = []
    def route(r):
        calls.append(r.request.url)
        if r.request.url == EXPORT_URL:
            r.fulfill(status=200, content_type="text/html", body=html)
        else:
            r.abort()
    context.route("**/*", route)
    page = context.new_page()
    page.set_default_timeout(1000)
    return context, page, calls


def test_export_selects_only_relationships_json_all_time_and_submits_once(browser):
    context, page, calls = context_with_page(browser, FLOW)
    markers = []
    def before_submit(existing):
        assert page.evaluate('submissions') == 0
        markers.append(existing)
    ExportPage(page, "test_account").request(before_submit)
    assert page.evaluate('submissions') == 1
    assert page.evaluate('chosen') == ['Followers and following']
    assert page.evaluate('format') == 'JSON'
    assert page.evaluate('range') == 'All time'
    assert markers == [[]]
    assert calls == [EXPORT_URL]
    context.close()


def test_changed_ui_never_reaches_submit(browser):
    changed = FLOW.replace('value="Followers and following">Followers and following', 'value="Unknown">Followers and following')
    context, page, _ = context_with_page(browser, changed)
    markers = []
    with pytest.raises(ExportUIError):
        ExportPage(page, "test_account").request(lambda existing: markers.append(existing))
    assert page.evaluate('submissions') == 0
    assert markers == []
    context.close()


def test_existing_pending_export_does_not_create_another(browser):
    context, page, _ = context_with_page(browser, '<h1>Export your information</h1><p>In progress</p><button>Create export</button>')
    markers = []
    with pytest.raises(ExportUIError, match='중복'):
        ExportPage(page, 'test_account').request(lambda existing: markers.append(existing))
    assert markers == []
    context.close()


def test_old_ready_export_is_not_downloaded_while_new_one_is_preparing(browser, tmp_path):
    html = '<h1>Export your information</h1><p>In progress</p><button>test_account Download yesterday</button>'
    context, page, _ = context_with_page(browser, html)
    assert ExportPage(page, 'test_account').download(tmp_path, lambda paths: None, []) is None
    assert list(tmp_path.iterdir()) == []
    context.close()


def test_duplicate_download_cards_are_not_arbitrarily_chosen(browser, tmp_path):
    html = '<h1>Export your information</h1><button>test_account Download today</button><button>test_account Download yesterday</button>'
    context, page, _ = context_with_page(browser, html)
    with pytest.raises(ExportUIError, match='여러'):
        ExportPage(page, 'test_account').download(tmp_path, lambda paths: None, [])
    assert list(tmp_path.iterdir()) == []
    context.close()


def test_download_saves_archive_before_context_closes(browser, tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('following.json', '{"relationships_following": []}')
        z.writestr('followers_1.json', '[]')
    encoded = base64.b64encode(archive.getvalue()).decode()
    html = '''<h1>Export your information</h1><button onclick="document.getElementById('files').hidden = false; this.remove()">test_account Download today</button>
    <div role="dialog" id="files" hidden><button onclick="saveFile()">Download</button></div>
    <script>function saveFile() {
      const bytes = Uint8Array.from(atob('ENCODED'), c => c.charCodeAt(0));
      const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([bytes], {type:'application/zip'}));
      a.download = 'export.zip'; a.click();
    }</script>'''.replace('ENCODED', encoded)
    context, page, _ = context_with_page(browser, html)
    completed = []
    download_events = []
    page.on('download', lambda event: download_events.append(event))
    paths = ExportPage(page, 'test_account').download(tmp_path, lambda items: completed.extend(items), [])
    again = ExportPage(page, 'test_account').download(tmp_path, lambda items: None, [])
    assert again == paths
    assert len(download_events) == 1
    context.close()
    assert paths == completed == [tmp_path / 'part-1.zip']
    assert zipfile.is_zipfile(paths[0])


def test_app_username_only_search_and_no_image_or_poll_requests(browser):
    context = browser.new_context(service_workers='block')
    calls = []
    html = Path('static/index.html').read_text()
    def route(r):
        path = r.request.url.removeprefix('https://app.invalid')
        calls.append(path)
        if path == '/':
            r.fulfill(content_type='text/html', body=html)
        elif path == '/api/status':
            r.fulfill(json={'logged_in': True, 'username': 'test_account', 'export': {'phase': 'ready', 'saved_at': 123}})
        elif path == '/api/fetch':
            r.fulfill(json={'status': 'ok', 'count': 2, 'users': [{'username': 'alice'}, {'username': 'zoe'}], 'saved_at': 123})
        else:
            r.abort()
    context.route('**/*', route)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('https://app.invalid/')
    page.get_by_role('button', name='맞팔 안한 목록').click()
    page.get_by_text('맞팔이 아닌 유저: 2명').wait_for()
    assert page.locator('#list-user-list').inner_text() == '@alice\n@zoe'
    assert page.locator('img').count() == 0
    assert page.get_by_role('button', name='차단', exact=True).count() == 0
    page.get_by_placeholder('아이디 검색').fill('ZOE')
    assert page.locator('#list-user-list .user-item:visible').inner_text() == '@zoe'
    page.wait_for_timeout(1200)
    assert calls.count('/api/status') == 1
    assert calls.count('/api/fetch') == 1
    assert not any('instagram.com' in path or '.jpg' in path for path in calls)
    assert not errors
    context.close()


def offline_app(browser, responses):
    context = browser.new_context(service_workers='block', accept_downloads=True)
    calls = []
    def route(r):
        url = r.request.url
        if not url.startswith('https://app.invalid/'):
            r.abort(); return
        path = url.removeprefix('https://app.invalid')
        calls.append((path, r.request.method))
        if path == '/':
            r.fulfill(content_type='text/html', body=Path('static/index.html').read_text())
        elif path in responses:
            value = responses[path]
            r.fulfill(json=value(r) if callable(value) else value)
        else:
            r.abort()
    context.route('**/*', route)
    page = context.new_page()
    page.goto('https://app.invalid/')
    return context, page, calls


def test_file_picker_and_drop_upload_locally(browser):
    uploads = []
    def uploaded(route):
        uploads.append(route.request.post_data_buffer)
        return {'status': 'ok', 'count': 0, 'users': []}
    context, page, calls = offline_app(browser, {
        '/api/status': {'logged_in': True, 'username': 'test_account', 'export': {'phase': 'idle'}},
        '/api/import': uploaded,
    })
    page.locator('#import-files').set_input_files([
        {'name': 'following.json', 'mimeType': 'application/json', 'buffer': b'[]'},
        {'name': 'followers_1.json', 'mimeType': 'application/json', 'buffer': b'[]'},
    ])
    page.get_by_text('파일을 가져왔습니다. 맞팔 안한 목록을 눌러 확인하세요.').wait_for()
    assert len(uploads) == 1 and b'following.json' in uploads[0]
    assert b'account_id' not in uploads[0]
    page.evaluate('''() => {
        const data = new DataTransfer();
        data.items.add(new File(['[]'], 'followers_1.json', {type:'application/json'}));
        document.getElementById('drop-zone').dispatchEvent(new DragEvent('drop', {dataTransfer:data, bubbles:true}));
    }''')
    page.wait_for_function("!operationPending")
    assert len(uploads) == 2
    assert all(path in ('/', '/api/status', '/api/import') for path, method in calls)
    context.close()


def test_home_and_local_comparison_work_without_login(browser):
    context, page, calls = offline_app(browser, {
        '/api/status': {'logged_in': False, 'export': {'phase': 'idle'},
                        'data': {'source': 'local', 'sources': [{'id': 'local', 'label': '가져온 파일'}], 'saved_at': 123}},
        '/api/fetch': {'status': 'ok', 'count': 1, 'users': [{'username': 'alice'}], 'saved_at': 123},
    })
    page.get_by_role('button', name='맞팔 안한 목록').wait_for()
    assert page.locator('#import-files').is_visible()
    assert not page.locator('#login-view').is_visible()
    assert not page.locator('#refresh-btn').is_visible()
    page.get_by_role('button', name='맞팔 안한 목록').click()
    page.get_by_text('맞팔이 아닌 유저: 1명').wait_for()
    assert page.locator('#list-user-list').inner_text() == '@alice'
    assert not any(path in ('/api/login', '/api/export/refresh') for path, method in calls)
    context.close()


def test_validation_shows_counts_files_and_unverified_coverage(browser):
    data = {'source': 'local', 'sources': [{'id': 'local', 'label': '가져온 파일'}],
            'following_count': 3, 'followers_count': 2,
            'validation': {'note': '마지막 분할 파일 누락은 확인할 수 없습니다.',
                           'files': [{'name': 'followers_1.json', 'records': 2}]}}
    context, page, calls = offline_app(browser, {'/api/status': {'logged_in': False, 'data': data}})
    page.locator('#data-validation summary').click()
    assert '팔로잉 3명 / 팔로워 2명' in page.locator('#data-validation').inner_text()
    assert '전체 자료 여부는 미확인' in page.locator('#data-validation').inner_text()
    assert 'followers_1.json: 2개 항목' in page.locator('#data-validation').inner_text()
    assert not any(method == 'POST' for path, method in calls)
    context.close()


def test_export_and_import_times_are_distinct_and_unknown_is_explicit(browser):
    context, page, calls = offline_app(browser, {'/api/status': {
        'logged_in': False, 'data': {'source': 'local', 'sources': [],
                                     'saved_at': 1000, 'imported_at': 2000, 'exported_at': 600, 'exported_at_source': 'user'}}})
    page.wait_for_function("document.getElementById('snapshot-info').textContent.includes('직접 입력')")
    text = page.locator('#snapshot-info').inner_text()
    assert '가져온 시각:' in text and '내보내기 생성 시각:' in text
    assert '(직접 입력)' in text and '실시간 상태가 아닙니다.' in text
    legacy = page.evaluate('savedText({saved_at: 1000})')
    assert '기존 파일 저장 기록:' in legacy
    assert '내보내기 생성 시각: 미확인' in legacy
    context.close()
