"""Synthetic file caches and local data only; no browser or account connections."""
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import data_library
import data_management
import export_data
import main
from download_cache import DownloadCache
from export_data import ExportError, ExportStore
from test_non_followers import logged_in, store, files, write_export


def archive(path, text):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('part.txt', text)
    return path


def test_download_resume_uses_identity_after_file_order_changes(tmp_path):
    first = DownloadCache(tmp_path, 'export', ['A', 'B'])
    first_key = first.keys[0]
    a = first.begin(first_key)
    archive(a, 'first')
    first.complete(first_key)
    second = DownloadCache(tmp_path, 'export', ['B', 'A'])
    assert second.existing(second.keys[1]) == a
    assert second.existing(second.keys[0]) is None
    b = second.begin(second.keys[0])
    assert b != a
    archive(b, 'second')
    second.complete(second.keys[0])
    assert second.paths() == [a, b]
    with zipfile.ZipFile(a) as z:
        assert z.read('part.txt') == b'first'


def test_unidentified_partial_download_does_not_resume_by_position(tmp_path):
    cache = DownloadCache(tmp_path, 'export', [None, None])
    path = cache.begin(cache.keys[0])
    archive(path, 'kept')
    cache.complete(cache.keys[0])
    before = path.read_bytes()
    with pytest.raises(ExportError, match='순서'):
        DownloadCache(tmp_path, 'export', [None, None])
    assert path.read_bytes() == before


def test_finished_file_recovers_crash_before_completion_marker(tmp_path):
    cache = DownloadCache(tmp_path, 'export', ['A', 'B'])
    a = cache.begin(cache.keys[0])
    archive(a, 'complete bytes installed atomically')
    restarted = DownloadCache(tmp_path, 'export', ['B', 'A'])
    assert restarted.existing(restarted.keys[1]) == a
    assert restarted.data['parts'][restarted.keys[1]]['state'] == 'complete'


def test_completed_cache_rejects_modified_zip(tmp_path):
    cache = DownloadCache(tmp_path, 'export', [None])
    path = cache.begin(cache.keys[0])
    archive(path, 'original')
    cache.complete(cache.keys[0])
    archive(path, 'changed')
    with pytest.raises(ExportError, match='변경'):
        cache.existing(cache.keys[0])


def test_legacy_cache_is_preserved_instead_of_resumed_by_index(tmp_path):
    path = tmp_path / 'transfer.json'
    path.write_text(json.dumps({'export':'old', 'count':2, 'parts':{'part-1.zip':'hash'}}))
    before = path.read_bytes()
    with pytest.raises(ExportError):
        DownloadCache(tmp_path, 'old', ['A','B'])
    assert path.read_bytes() == before


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(export_data, 'DATA_ROOT', tmp_path / 'exports')
    data_library.select_source('local')
    store = data_library.local_store()
    store.import_sources([write_export(tmp_path / 'one')], '')
    old = store.metadata()['snapshot']
    changed = files()
    changed['following.json'] = b'[]'
    store.import_sources([write_export(tmp_path / 'two', changed)], '')
    return store, old


def test_cleanup_preserves_current_snapshot_job_and_active_download(library):
    store, old = library
    for generation in ('a' * 32, 'b' * 32):
        directory = store.directory / 'downloads' / generation
        directory.mkdir(parents=True)
        (directory / 'part-1.zip').write_bytes(b'synthetic archive')
    store.write_state({'phase':'pending', 'generation':'a' * 32, 'submitted_at':100})
    before = (store.directory / 'job.json').read_bytes()
    plan = data_management.preview('local', 'cleanup')
    assert len(plan['files']) == 2
    assert any(old in item['path'] for item in plan['files'])
    result = data_management.execute('local', 'cleanup', plan['token'], True)
    assert result['removed'] == 2
    assert store.compare()['count'] == 0
    assert (store.directory / 'downloads' / ('a' * 32)).exists()
    assert not (store.directory / 'downloads' / ('b' * 32)).exists()
    assert (store.directory / 'job.json').read_bytes() == before


def test_delete_requires_confirmation_and_preserves_export_job(library):
    store, old = library
    store.write_state({'phase':'uncertain', 'remote_blocked':True})
    before = (store.directory / 'job.json').read_bytes()
    plan = data_management.preview('local', 'delete_data')
    with pytest.raises(ExportError, match='확인'):
        data_management.execute('local', 'delete_data', plan['token'], False)
    assert store.metadata()
    data_management.execute('local', 'delete_data', plan['token'], True)
    assert store.compare()['status'] == 'no_data'
    assert (store.directory / 'job.json').read_bytes() == before


def test_stale_preview_never_removes_new_import(library, tmp_path):
    store, old = library
    plan = data_management.preview('local', 'delete_data')
    store.import_sources([write_export(tmp_path / 'three')], '')
    with pytest.raises(ExportError, match='변경'):
        data_management.execute('local', 'delete_data', plan['token'], True)
    assert store.compare()['count'] == 1


def test_cleanup_refuses_symlinked_data_directory(library, tmp_path):
    store, old = library
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'kept.txt').write_text('keep')
    (store.directory / 'downloads').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError):
        data_management.preview('local', 'cleanup')
    assert (outside / 'kept.txt').read_text() == 'keep'


def test_management_api_preview_and_confirmation(logged_in, store, tmp_path):
    store.import_sources([write_export(tmp_path / 'source')], 'fake')
    with TestClient(main.app) as client:
        preview = client.post('/api/data/preview', json={'source':'12345','mode':'delete_data'}).json()
        assert preview['files'] and store.metadata()
        denied = client.post('/api/data/manage', json={'source':'12345','mode':'delete_data','token':preview['token']}).json()
        assert denied['code'] == 'confirmation_required'
        result = client.post('/api/data/manage', json={'source':'12345','mode':'delete_data','token':preview['token'],'confirmed':True}).json()
        assert result['status'] == 'ok'
        assert store.metadata() is None
        assert data_library.selected_store().directory.name == 'local'


def test_move_failure_restores_data_before_delete_commit(library, monkeypatch):
    store, old = library
    plan = data_management.preview('local', 'delete_data')
    original = data_management.os.replace
    calls = []
    def fail_second(source, destination):
        calls.append((source,destination))
        if len(calls) == 2:
            raise OSError('synthetic failure')
        return original(source,destination)
    monkeypatch.setattr(data_management.os, 'replace', fail_second)
    with pytest.raises(OSError):
        data_management.execute('local','delete_data',plan['token'],True)
    assert store.metadata() is not None
    assert store.compare()['count'] == 0
    assert (store.directory / 'snapshots' / old).exists()


def test_complete_batch_imports_after_crash_without_any_provider_calls(store):
    import export_jobs
    from unittest.mock import Mock
    directory = store.directory / 'downloads' / ('a' * 32)
    cache = DownloadCache(directory,'export',['A','B'])
    for index,key in enumerate(cache.keys):
        path = cache.begin(key)
        with zipfile.ZipFile(path,'w') as z:
            for name,body in files().items():
                if (index == 0) == (name != 'followers_2.json'):
                    z.writestr(name,body)
        cache.complete(key)
    store.write_state({'phase':'submitted','submitted_at':100,'generation':'a'*32})
    provider = Mock()
    provider.request.side_effect = AssertionError('no new request')
    provider.download.side_effect = AssertionError('no readiness check or download')
    result = export_jobs.refresh(store,'fake',provider,now=2000)
    assert result['status'] == 'ok' and result['count'] == 1
    provider.request.assert_not_called()
    provider.download.assert_not_called()
