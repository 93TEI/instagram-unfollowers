"""Synthetic exports only. Real accounts and outgoing networking are forbidden."""
import io
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import export_data
import export_jobs
import data_library
import instagram
import main
import state
from export_browser import ExportUIError, block_request
from export_data import ExportError, ExportStore, compare_files, parse_usernames


def entry(name, *, title=False):
    item = {"href": "https://www.instagram.com/" + name.strip().lstrip("@") + "/", "timestamp": 123}
    if not title:
        item["value"] = name
    return {"title": name if title else "", "string_list_data": [item]}


def content(data):
    return json.dumps(data).encode()


def files():
    return {
        "following.json": content({"relationships_following": [entry("Alice", title=True), entry("Bob", title=True), entry("zoe", title=True)]}),
        "followers_1.json": content([entry("alice"), entry("someone_else")]),
        "followers_2.json": content([entry("ALICE"), entry("bob")]),
    }


def write_export(path, data=None):
    path.mkdir(parents=True, exist_ok=True)
    for name, value in (data if data is not None else files()).items():
        (path / name).write_bytes(value)
    return path


@pytest.fixture
def store(tmp_path):
    return ExportStore("12345", tmp_path / "exports")


@pytest.fixture
def logged_in(monkeypatch, store):
    monkeypatch.setattr(export_data, "DATA_ROOT", store.directory.parent)
    data_library.select_source("12345", require_data=False)
    monkeypatch.setattr(state, "_job_lock", threading.Lock())
    monkeypatch.setattr(instagram, "_store", lambda: store)
    monkeypatch.setattr(instagram, "cl", SimpleNamespace(sessionid="fake-session", password=None))
    monkeypatch.setattr(main, "try_resume_session", lambda: True)
    monkeypatch.setitem(state.app_state, "logged_in", True)
    monkeypatch.setitem(state.app_state, "username", "test_account")
    monkeypatch.setitem(state.app_state, "user_id", "12345")
    monkeypatch.setitem(state.app_state, "non_followers", [])
    monkeypatch.setitem(state.app_state, "job_status", "idle")


def test_following_minus_followers_not_the_reverse():
    result = compare_files(files())
    assert result["users"] == [{"username": "zoe"}]
    assert result["following_count"] == result["followers_count"] == 3
    assert result["count"] == 1
    assert all(set(user) == {"username"} for user in result["users"])


@pytest.mark.parametrize("wrapper", [False, True])
def test_old_and_new_json_structures(wrapper):
    data = [entry("Alice"), entry(" @ALICE "), entry("bob", title=True)]
    if wrapper:
        data = {"relationships_followers": data}
    assert parse_usernames(data, "followers") == {"alice", "bob"}


def test_href_only_is_parsed_locally():
    data = [{"string_list_data": [{"href": "https://www.instagram.com/_u/alice/"}]}]
    assert parse_usernames(data, "following") == {"alice"}


def test_empty_valid_lists_are_not_missing_files():
    assert compare_files({"following.json": b"[]", "followers.json": b"[]"})["count"] == 0
    assert compare_files({"following.json": content([entry("alice")]), "followers.json": b"[]"})["users"] == [{"username": "alice"}]


def test_results_are_unique_and_sorted():
    result = compare_files({"following.json": content([entry("zoe"), entry("Amy"), entry("amy")]), "followers.json": b"[]"})
    assert result["users"] == [{"username": "amy"}, {"username": "zoe"}]


@pytest.mark.parametrize("bad", [None, {}, {"relationships_following": {}}, [None], [{"string_list_data": []}], [entry("<script>")], [entry("a/b")]])
def test_bad_records_fail_instead_of_silently_dropping_followers(bad):
    with pytest.raises(ExportError):
        parse_usernames(bad, "following")


@pytest.mark.parametrize("missing", ["following.json", "followers_1.json", "followers_2.json"])
def test_missing_files_detected_by_snapshot_manifest(store, tmp_path, missing):
    store.import_sources([write_export(tmp_path / "source")], "test_account")
    metadata = store.metadata()
    (store.directory / "snapshots" / metadata["snapshot"] / missing).unlink()
    with pytest.raises(ExportError):
        store.compare()


def test_gap_in_numbered_files_is_rejected():
    data = files()
    data["followers_3.json"] = data.pop("followers_2.json")
    with pytest.raises(ExportError, match="누락"):
        compare_files(data)


def test_mixed_single_and_numbered_lists_rejected():
    data = files()
    data["followers.json"] = b"[]"
    with pytest.raises(ExportError, match="섞여"):
        compare_files(data)


def test_corrupt_refresh_does_not_replace_good_snapshot(store, tmp_path):
    store.import_sources([write_export(tmp_path / "good")], "test_account")
    previous = store.metadata()
    bad = files()
    bad["followers_2.json"] = b"{broken"
    with pytest.raises(ExportError):
        store.import_sources([write_export(tmp_path / "bad", bad)], "test_account")
    assert store.metadata() == previous
    assert store.compare()["users"] == [{"username": "zoe"}]


def test_reimport_is_idempotent_and_keeps_snapshot_timestamp(store, tmp_path):
    source = write_export(tmp_path / "source")
    first = store.import_sources([source], "test_account")
    second = store.import_sources([source], "test_account")
    assert first == second
    assert len(list((store.directory / "snapshots").iterdir())) == 1


def test_modified_json_is_rejected(store, tmp_path):
    store.import_sources([write_export(tmp_path / "source")], "test_account")
    path = store.directory / "snapshots" / store.metadata()["snapshot"] / "followers_1.json"
    path.write_text("[]")
    with pytest.raises(ExportError, match="변경"):
        store.compare()
    with pytest.raises(ExportError, match="변경"):
        store.import_sources([tmp_path / "source"], "test_account")


def test_accounts_never_mix(store, tmp_path):
    store.import_sources([write_export(tmp_path / "source")], "test_account")
    other = ExportStore("67890", tmp_path / "exports")
    assert other.compare()["status"] == "no_data"
    assert other.read_state()["phase"] == "idle"


@pytest.mark.parametrize("account", ["../escape", "", None, "name", "1/2"])
def test_account_path_is_not_user_controlled(account, tmp_path):
    with pytest.raises(ExportError):
        ExportStore(account, tmp_path)


def test_zip_multipart_and_unrelated_files(store, tmp_path):
    paths = []
    for index, (name, data) in enumerate(files().items()):
        path = tmp_path / f"part-{index}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("connections/followers_and_following/" + name, data)
            archive.writestr("messages/ignored.json", "not read")
            archive.writestr("media/photo.jpg", b"not read")
        paths.append(path)
    result = store.import_sources(paths, "test_account")
    assert result["users"] == [{"username": "zoe"}]
    saved = store.directory / "snapshots" / store.metadata()["snapshot"]
    assert {p.name for p in saved.iterdir()} == set(files())


def test_zip_traversal_rejected(store, tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../followers_1.json", "[]")
    with pytest.raises(ExportError, match="경로"):
        store.import_sources([path], "test_account")


def test_two_accounts_in_one_export_rejected(store, tmp_path):
    source = tmp_path / "source"
    write_export(source / "one")
    write_export(source / "two")
    with pytest.raises(ExportError, match="중복"):
        store.import_sources([source], "test_account")


def test_size_limit(store, tmp_path, monkeypatch):
    source = write_export(tmp_path / "source")
    monkeypatch.setattr(export_data, "MAX_JSON_BYTES", 8)
    with pytest.raises(ExportError, match="용량"):
        store.import_sources([source], "test_account")


class Provider:
    def __init__(self):
        self.requests = 0
        self.checks = 0
        self.fail_after_submit = False
        self.ready = False

    def request(self, before_submit):
        self.requests += 1
        before_submit(["old-export"])
        if self.fail_after_submit:
            raise TimeoutError("sensitive authenticated URL must not be returned")

    def download(self, directory, on_download, existing_exports):
        self.checks += 1
        assert existing_exports == ["old-export"]
        if not self.ready:
            return None
        path = Path(directory) / "part-1.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files().items():
                archive.writestr(name, data)
        on_download([path])
        return [path]


def test_repeated_refresh_and_restart_submit_only_once(store):
    provider = Provider()
    assert export_jobs.refresh(store, "test_account", provider, now=100)["status"] == "pending"
    restarted = ExportStore("12345", store.directory.parent)
    for t in (101, 110, 500, 1000):
        assert export_jobs.refresh(restarted, "test_account", provider, now=t)["status"] == "pending"
    assert provider.requests == 1
    assert provider.checks == 0
    export_jobs.refresh(restarted, "test_account", provider, now=1901)
    assert provider.requests == 1 and provider.checks == 1


def test_timeout_after_submission_does_not_resubmit(store):
    provider = Provider()
    provider.fail_after_submit = True
    result = export_jobs.refresh(store, "test_account", provider, now=100)
    assert result["status"] == "error"
    assert "sensitive" not in result["message"]
    assert store.read_state()["phase"] == "uncertain"
    export_jobs.refresh(store, "test_account", provider, now=1901)
    assert provider.requests == 1 and provider.checks == 1


def test_crash_before_submission_restarts_preparation_not_readiness(store):
    provider = Provider()
    provider.request = Mock(side_effect=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        export_jobs.refresh(store, "test_account", provider, now=100)
    assert store.read_state()["phase"] == "preparing"
    assert "submitted_at" not in store.read_state()
    restarted = ExportStore("12345", store.directory.parent)
    next_provider = Provider()
    export_jobs.refresh(restarted, "test_account", next_provider, now=101)
    assert next_provider.requests == next_provider.checks == 0
    export_jobs.refresh(restarted, "test_account", next_provider, now=1901)
    assert next_provider.requests == 1 and next_provider.checks == 0
    assert restarted.read_state()["phase"] == "pending"


def test_submission_marker_wins_over_inconsistent_preparing_phase(store):
    store.write_state({"phase": "preparing", "submitted_at": 100,
                       "generation": "a" * 32, "existing_exports": ["old-export"]})
    provider = Provider()
    export_jobs.refresh(store, "test_account", provider, now=2000)
    assert provider.requests == 0 and provider.checks == 1


def test_failed_ui_before_submit_can_retry_but_not_immediately(store):
    provider = Provider()
    provider.request = Mock(side_effect=ExportUIError("화면 변경"))
    export_jobs.refresh(store, "test_account", provider, now=100)
    assert store.read_state()["phase"] == "error"
    export_jobs.refresh(store, "test_account", provider, now=101)
    assert provider.request.call_count == 1
    export_jobs.refresh(store, "test_account", provider, now=1901)
    assert provider.request.call_count == 2


def test_completed_download_is_imported_and_later_fetch_is_offline(store, logged_in):
    provider = Provider()
    export_jobs.refresh(store, "test_account", provider, now=100)
    provider.ready = True
    assert export_jobs.refresh(store, "test_account", provider, now=1901)["status"] == "ok"
    assert store.read_state()["phase"] == "ready"
    for _ in range(5):
        assert instagram.fetch_non_followers()["users"] == [{"username": "zoe"}]
    assert provider.requests == 1 and provider.checks == 1


def test_completed_download_survives_crash_before_import(store, tmp_path):
    provider = Provider()
    directory = store.directory / "downloads" / ("a" * 32)
    directory.mkdir(parents=True)
    provider.ready = True
    provider.download(directory, lambda paths: None, ["old-export"])
    store.write_state({"phase": "downloaded", "generation": "a" * 32, "files": ["part-1.zip"]})
    provider.request = Mock(side_effect=AssertionError("must not request"))
    provider.download = Mock(side_effect=AssertionError("must not redownload"))
    assert export_jobs.refresh(store, "test_account", provider)["status"] == "ok"


def test_corrupt_job_record_never_creates_new_request(store):
    store.directory.mkdir(parents=True)
    (store.directory / "job.json").write_text("{")
    provider = Provider()
    with pytest.raises(ExportError):
        export_jobs.refresh(store, "test_account", provider)
    assert provider.requests == provider.checks == 0


def test_account_file_lock_prevents_second_worker(store):
    provider = Provider()
    with export_jobs.account_lock(store) as locked:
        assert locked
        assert export_jobs.refresh(store, "test_account", provider)["status"] == "busy"
    assert provider.requests == 0


def test_durable_submit_marker_written_before_network_commit(store):
    provider = Provider()
    def request(before_submit):
        before_submit([])
        assert store.read_state()["phase"] == "submitted"
        raise KeyboardInterrupt()  # Simulate process death, not an ordinary handled error.
    provider.request = request
    with pytest.raises(KeyboardInterrupt):
        export_jobs.refresh(store, "test_account", provider, now=100)
    assert store.read_state()["phase"] == "submitted"


def test_fetch_without_login_does_not_call_client(monkeypatch, logged_in):
    monkeypatch.setitem(state.app_state, "logged_in", False)
    assert instagram.fetch_non_followers()["status"] == "no_data"


def test_http_contract_and_removed_mutations(logged_in, store, tmp_path):
    store.import_sources([write_export(tmp_path / "source")], "test_account")
    with TestClient(main.app) as client:
        for _ in range(3):
            assert client.post("/api/fetch").json()["users"] == [{"username": "zoe"}]
            assert client.get("/api/status").json()["export"]["has_data"]
        assert client.post("/api/fetch?dry_run=true").json()["count"] == 1
        for endpoint in ("/api/bulk-unfollow", "/api/bulk-block"):
            assert client.post(endpoint, json={"user_ids": ["1"]}).status_code == 404


def test_no_data_does_not_automatically_export(logged_in):
    with TestClient(main.app) as client:
        assert client.post("/api/fetch").json()["status"] == "no_data"
        assert client.get("/api/status").json()["export"]["phase"] == "idle"


def test_login_logout_cannot_change_account_while_exporting(logged_in, monkeypatch):
    login = Mock()
    logout = Mock()
    monkeypatch.setattr(main, "ig_login", login)
    monkeypatch.setattr(main, "ig_logout", logout)
    with TestClient(main.app) as client:
        assert state.try_start_job()
        try:
            assert client.post("/api/login", json={"username": "other", "password": "fake"}).json()["status"] == "busy"
            assert client.post("/api/logout").json()["status"] == "busy"
        finally:
            state.finish_job()
    login.assert_not_called()
    logout.assert_not_called()


def test_saved_session_restore_is_local_and_preserves_username(monkeypatch, tmp_path, logged_in):
    path = tmp_path / "session.json"
    path.write_text("{}")
    client = SimpleNamespace(load_settings=Mock(return_value={"app_username": "test_account"}),
                             set_settings=Mock(), username="", user_id="12345")
    monkeypatch.setattr(instagram, "SESSION_FILE", str(path))
    monkeypatch.setattr(instagram, "cl", client)
    assert instagram.try_resume_session()
    assert state.app_state["username"] == "test_account"
    assert state.app_state["user_id"] == "12345"
    client.load_settings.assert_called_once_with(str(path))


@pytest.mark.parametrize("url,kind,blocked", [
    ("https://i.instagram.com/api/v1/friendships/123/followers/", "xhr", True),
    ("https://i.instagram.com/api/v1/friendships/destroy/123/", "xhr", True),
    ("https://i.instagram.com/api/v1/users/123/info/", "xhr", True),
    ("https://www.instagram.com/api/v1/users/web_profile_info/", "xhr", True),
    ("https://cdn.instagram.com/pic.jpg", "image", True),
    ("https://accountscenter.instagram.com/info_and_permissions/dyi/", "document", False),
])
def test_browser_blocks_relationship_and_profile_fetches(url, kind, blocked):
    assert block_request(SimpleNamespace(url=url, resource_type=kind)) is blocked


def test_ui_contains_only_text_results_and_no_background_polling():
    html = Path("static/index.html").read_text()
    assert 'row.textContent = "@" + user.username' in html
    for removed in ("profile_pic_url", "full_name", "bulk-unfollow", "bulk-block", "setInterval", 'createElement("img")'):
        assert removed not in html


def test_upload_json_files_without_numeric_account_input(logged_in, store):
    with TestClient(main.app) as client:
        response = client.post("/api/import", files=[("files", (name, body, "application/json")) for name, body in files().items()])
        assert response.json()["users"] == [{"username": "zoe"}]
        assert client.post("/api/fetch").json()["count"] == 1


def test_upload_zip_and_failed_import_preserve_previous_data(logged_in, store):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        for name, body in files().items():
            z.writestr("connections/followers_and_following/" + name, body)
    with TestClient(main.app) as client:
        assert client.post("/api/import", files={"files": ("export.zip", archive.getvalue())}).json()["status"] == "ok"
        previous = data_library.local_store().metadata()
        bad = client.post("/api/import", files={"files": ("following.json", b"broken")}).json()
        assert bad["status"] == "error"
        assert data_library.local_store().metadata() == previous


@pytest.mark.parametrize("name", ["../followers.json", "profile.html", "nested/following.json"])
def test_upload_rejects_unexpected_paths_and_formats(logged_in, name):
    with TestClient(main.app) as client:
        assert client.post("/api/import", files={"files": (name, b"[]")}).json()["status"] == "error"


def test_upload_stream_limit_releases_job_lock(logged_in, monkeypatch):
    import file_import
    monkeypatch.setattr(file_import, "MAX_UPLOAD_BYTES", 128)
    with TestClient(main.app) as client:
        response = client.post("/api/import", files={"files": ("export.zip", b"x" * 256)})
        assert response.status_code == 413
        assert response.json()["code"] == "upload_too_large"
    assert state.try_start_job()
    state.finish_job()


def test_offline_import_compare_and_restart_never_construct_client(logged_in, monkeypatch):
    monkeypatch.setitem(state.app_state, "logged_in", False)
    client_factory = Mock(side_effect=AssertionError("Instagram client must not be constructed"))
    monkeypatch.setattr(instagram, "_client", client_factory)
    with TestClient(main.app) as client:
        result = client.post("/api/import", files=[("files", (name, body)) for name, body in files().items()]).json()
        assert result["status"] == "ok"
        assert client.post("/api/fetch").json()["users"] == [{"username": "zoe"}]
        assert client.get("/api/status").json()["data"]["source"] == "local"
    with TestClient(main.app) as restarted:
        assert restarted.post("/api/fetch").json()["count"] == 1
        assert restarted.post("/api/export/refresh").json()["status"] == "login_required"
    client_factory.assert_not_called()


def test_data_selection_is_independent_of_login_and_logout(logged_in, store, tmp_path, monkeypatch):
    store.import_sources([write_export(tmp_path / "account")], "test_account")
    data_library.local_store().import_sources([write_export(tmp_path / "local", {"following.json": b"[]", "followers.json": b"[]"})], "")
    data_library.select_source("local")
    monkeypatch.setattr(main, "ig_logout", lambda: state.app_state.update(logged_in=False, user_id=None, username=None))
    with TestClient(main.app) as client:
        assert client.post("/api/fetch").json()["count"] == 0
        assert client.post("/api/logout").json()["status"] == "ok"
        assert client.get("/api/status").json()["data"]["source"] == "local"
        assert client.post("/api/data/select", json={"source": "12345"}).json()["status"] == "ok"
        assert client.post("/api/fetch").json()["count"] == 1
        assert client.post("/api/data/select", json={"source": "../../session.json"}).json()["status"] == "error"
