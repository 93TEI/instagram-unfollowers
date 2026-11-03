"""Offline setup/build tests; no user apps, session data, browsers or servers started."""
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import browser_runtime
import build_desktop_app
import desktop_launcher
import export_data
import main
import setup_check


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project with spaces'
    (root / 'static').mkdir(parents=True)
    for name in ('main.py','desktop_launcher.py','static/index.html'):
        (root / name).write_text('synthetic project')
    (root / 'requirements.lock.txt').write_text('fastapi==1.0.0\n')
    return root


def report(project, **kwargs):
    defaults = dict(version_lookup=lambda name:'1.0.0', browser_lookup=lambda:None,
                    python_version=(3,11,0), tls=('OpenSSL 3.0.0',(3,0,0)))
    defaults.update(kwargs)
    return setup_check.check_setup(project,**defaults)


def test_missing_browser_keeps_local_setup_ready(project):
    result = report(project)
    assert result['local_ready'] and not result['auto_export_environment_ready']
    assert result['account_integration_verified'] is False
    assert all(message['level'] != 'error' for message in result['messages'])


def test_missing_or_wrong_dependency_is_actionable(project):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    result = report(project,version_lookup=missing)
    assert not result['local_ready']
    assert result['packages'][0]['installed'] is None
    assert any('requirements.txt' in message['text'] for message in result['messages'])
    assert report(project,version_lookup=lambda name:'wrong')['status'] == 'needs_setup'


def test_tls_warning_does_not_disable_local_files(project):
    result = report(project,tls=('LibreSSL 2.8.3',(2,8,3)),browser_lookup=lambda:{'label':'Chrome'})
    assert result['local_ready'] and not result['auto_export_environment_ready']
    assert any('SSL' in message['text'] for message in result['messages'])


def test_diagnostics_never_reads_session_or_starts_process(project,monkeypatch):
    (project / 'session.json').write_text('private sentinel')
    original = Path.read_text
    def read(path,*args,**kwargs):
        if path.name == 'session.json':
            raise AssertionError('must not read account data')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    monkeypatch.setattr(subprocess,'Popen',Mock(side_effect=AssertionError('must not start browser/server')))
    result = report(project)
    assert result['local_ready']
    assert 'private sentinel' not in json.dumps(result)


def test_missing_assets_and_python_version_are_reported(project):
    (project / 'static/index.html').unlink()
    result = report(project,python_version=(3,8,0))
    assert not result['local_ready']
    assert len([item for item in result['messages'] if item['level']=='error']) == 2


def test_browser_lookup_uses_exact_playwright_revision(tmp_path):
    manifest = tmp_path / 'browsers.json'
    manifest.write_text(json.dumps({'browsers':[{'name':'chromium','revision':'test123'}]}))
    cache = tmp_path / 'cache'
    executable = cache / 'chromium-test123/chrome-linux64/chrome'
    executable.parent.mkdir(parents=True)
    executable.write_text('synthetic executable')
    executable.chmod(0o700)
    result = browser_runtime.find_browser(chrome_paths=[],cache_root=cache,browser_manifest=manifest)
    assert result['channel'] == 'chromium'
    manifest.write_text(json.dumps({'browsers':[{'name':'chromium','revision':'different'}]}))
    assert browser_runtime.find_browser(chrome_paths=[],cache_root=cache,browser_manifest=manifest) is None


def test_chrome_lookup_needs_no_playwright_process(tmp_path):
    executable = tmp_path / 'Chrome'
    executable.write_text('synthetic executable')
    executable.chmod(0o700)
    result = browser_runtime.find_browser(chrome_paths=[executable],browser_manifest=tmp_path / 'missing')
    assert result['channel'] == 'chrome'


def test_static_app_files_work_outside_project_cwd(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'try_resume_session',lambda:False)
    monkeypatch.chdir(tmp_path)
    with TestClient(main.app) as client:
        assert '<html lang="ko">' in client.get('/').text
        assert client.get('/static/index.html').status_code == 200


def test_setup_endpoint_is_local_and_does_not_require_login(monkeypatch):
    monkeypatch.setattr(main,'try_resume_session',lambda:False)
    monkeypatch.setattr(main,'check_setup',lambda:{'status':'ok','local_ready':True})
    with TestClient(main.app) as client:
        assert client.get('/api/setup').json()['local_ready']


def test_bundle_rebuild_quotes_moved_project_and_does_not_launch_it(project,tmp_path):
    applications,desktop = tmp_path / 'Applications',tmp_path / 'Desktop'
    app = build_desktop_app.build_app(project,applications,desktop)
    wrapper = app / 'Contents/MacOS/InstagramMutual'
    assert '[ ! -x ' in wrapper.read_text()
    assert 'setup_check.py' in wrapper.read_text()
    subprocess.run(['/bin/sh','-n',str(wrapper)],check=True)
    moved = project.with_name('moved project')
    project.rename(moved)
    build_desktop_app.build_app(moved,applications,desktop)
    assert str(moved) in wrapper.read_text()
    assert str(project) not in wrapper.read_text()
    assert (desktop / build_desktop_app.NAME).resolve() == app


def test_builder_preserves_other_desktop_apps(project,tmp_path):
    desktop = tmp_path / 'Desktop'
    other = desktop / build_desktop_app.NAME
    other.mkdir(parents=True)
    (other / 'keep.txt').write_text('other app')
    with pytest.raises(RuntimeError):
        build_desktop_app.build_app(project,tmp_path / 'Applications',desktop)
    assert (other / 'keep.txt').read_text() == 'other app'
    assert not (tmp_path / 'Applications').exists()


def test_launcher_checks_setup_before_starting_server(tmp_path,monkeypatch):
    monkeypatch.setattr(desktop_launcher,'LOG_DIR',tmp_path / 'logs')
    monkeypatch.setattr(desktop_launcher,'healthy',lambda:False)
    monkeypatch.setattr(desktop_launcher,'check_setup',lambda:{'local_ready':False,'messages':[{'level':'error','text':'missing dependency'}]})
    process = Mock(side_effect=AssertionError('must not start a server'))
    monkeypatch.setattr(desktop_launcher.subprocess,'Popen',process)
    with pytest.raises(RuntimeError,match='missing dependency'):
        desktop_launcher.launch()
    process.assert_not_called()


def test_lock_matches_the_installed_offline_validation_environment():
    for name,version in setup_check.locked_versions(setup_check.ROOT).items():
        assert importlib.metadata.version(name) == version


def test_cli_import_defaults_to_local_without_account_id(tmp_path,monkeypatch,capsys):
    import sys
    import import_export
    import data_library
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'following.json').write_text('[]')
    (source / 'followers.json').write_text('[]')
    monkeypatch.setattr(export_data,'DATA_ROOT',tmp_path / 'exports')
    monkeypatch.setattr(sys,'argv',['import_export.py',str(source)])
    import_export.main()
    assert json.loads(capsys.readouterr().out)['count'] == 0
    assert data_library.selected_store().directory.name == 'local'
