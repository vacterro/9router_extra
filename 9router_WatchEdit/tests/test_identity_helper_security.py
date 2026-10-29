"""CLI credential and restore boundaries, with synthetic private fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import urllib.request

import httpx
import pytest

from core import engine_identity as ei


@pytest.fixture
def tool():
    path = Path(__file__).resolve().parents[2] / 'tools/patch_workbuddy_client_identity.py'
    spec = importlib.util.spec_from_file_location('helper_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Reply:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return json.dumps(self.payload).encode()


def fake_http(monkeypatch, payload, status=200):
    options, requests = [], []
    real_client = httpx.Client
    def serve(request):
        requests.append(request)
        return httpx.Response(status, json=payload, headers={'Location':'https://198.51.100.7/elsewhere'})
    def factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(serve), **kwargs)
    monkeypatch.setattr(httpx, 'Client', factory)
    def urllib_serve(request, *args, **kwargs):
        requests.append(request)
        return Reply(payload)
    monkeypatch.setattr(urllib.request, 'urlopen', urllib_serve)
    return options, requests


@pytest.mark.parametrize('endpoint', ['https://198.51.100.7', 'https://example.invalid',
                                      'http://user:password@127.0.0.1:20128'])
def test_unsafe_endpoint_is_refused_before_reading_local_auth(tool, monkeypatch, endpoint):
    reads = []
    monkeypatch.setattr(tool, 'BASE', endpoint)
    monkeypatch.setattr(tool, 'cli_token', lambda: reads.append('token-read') or 'synthetic')
    fake_http(monkeypatch, {'ok':True})
    assert tool.verify(['fixture/model']) == 1
    assert not reads


def test_redirects_and_ambient_proxies_cannot_receive_local_auth(tool, monkeypatch):
    monkeypatch.setattr(tool, 'BASE', 'http://127.0.0.1:20128')
    monkeypatch.setattr(tool, 'cli_token', lambda: 'synthetic')
    options, requests = fake_http(monkeypatch, {'ok':False}, status=302)
    assert tool.verify(['fixture/model']) == 1
    assert options and options[0]['follow_redirects'] is False
    assert options[0]['trust_env'] is False
    assert len(requests) == 1
    assert requests[0].url.host == '127.0.0.1'


def test_provider_error_is_redacted_before_printing(tool, monkeypatch, capsys):
    secret = 'canary' * 8
    monkeypatch.setattr(tool, 'BASE', 'http://127.0.0.1:20128')
    monkeypatch.setattr(tool, 'cli_token', lambda: 'synthetic')
    fake_http(monkeypatch, {'ok':False,'error':'Authorization: Bearer ' + secret})
    assert tool.verify(['fixture/model']) == 1
    assert secret not in capsys.readouterr().out


def test_invalid_payload_is_a_clean_failure(tool, monkeypatch):
    monkeypatch.setattr(tool, 'BASE', 'http://127.0.0.1:20128')
    monkeypatch.setattr(tool, 'cli_token', lambda: 'synthetic')
    fake_http(monkeypatch, [])
    assert tool.verify(['fixture/model']) == 1


def restore_fixture(tool, tmp_path, monkeypatch):
    chunk = tmp_path / 'install' / 'fixture.js'
    chunk.parent.mkdir()
    chunk.write_bytes(b'var current=1;')
    rollback = tmp_path / 'private-rollback'
    rollback.mkdir()
    backup = rollback / 'fixture.js.first.bak'
    backup.write_bytes(b'var restored=1;')
    monkeypatch.setattr(ei, '_backup_dir', lambda: str(rollback))
    monkeypatch.setattr(ei, 'check', lambda: [ei.IdentityState('workbuddy',ei.IDENTITY_PRESENT,chunk=str(chunk))])
    # Keep the same disposable storage boundary for the predecessor CLI,
    # which enumerates chunks directly instead of calling the shared module.
    monkeypatch.setattr(tool, 'candidate_chunks', lambda: [(str(chunk), chunk.read_text())], raising=False)
    monkeypatch.setattr(tool, 'ROLLBACK', str(rollback), raising=False)
    return chunk, backup


def test_restore_respects_an_existing_chunk_writer(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    lock = Path(str(chunk)+'.identity.lock')
    lock.write_text('foreign owner',encoding='utf-8')
    assert tool.restore_patch() == 1
    assert chunk.read_bytes() == b'var current=1;'
    assert lock.read_text(encoding='utf-8') == 'foreign owner'


def test_restore_refuses_hardlinked_chunk(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    outside = tmp_path / 'other.js'
    os.link(chunk, outside)
    assert tool.restore_patch() == 1
    assert outside.read_bytes() == chunk.read_bytes() == b'var current=1;'


def test_restore_does_not_install_syntax_failure(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    backup.write_bytes(b'not valid javascript {{{')
    assert tool.restore_patch() == 1
    assert chunk.read_bytes() == b'var current=1;'


def test_restore_uses_newest_backup_by_mtime_and_keeps_current_rollback(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    older = backup.with_name('fixture.js.z.bak')
    older.write_bytes(b'var older=1;')
    os.utime(older,(1000,1000))
    assert tool.restore_patch() == 0
    assert chunk.read_bytes() == b'var restored=1;'
    assert any(p.read_bytes()==b'var current=1;' for p in backup.parent.glob('*.bak'))


def test_missing_authentication_never_sends_a_request(tool, monkeypatch):
    monkeypatch.setattr(tool, 'BASE', 'http://127.0.0.1:20128')
    def unavailable():
        raise OSError('private authentication fixture is absent')
    monkeypatch.setattr(tool, 'cli_token', unavailable)
    _, requests = fake_http(monkeypatch, {'ok': True})
    assert tool.verify(['fixture/model']) == 1
    assert not requests


def test_loopback_verification_preserves_success(tool, monkeypatch, capsys):
    monkeypatch.setattr(tool, 'BASE', 'http://127.0.0.1:20128')
    monkeypatch.setattr(tool, 'cli_token', lambda: 'synthetic')
    _, requests = fake_http(monkeypatch, {'ok': True, 'latencyMs': 17})
    assert tool.verify(['fixture/model']) == 0
    assert len(requests) == 1
    assert 'synthetic' in requests[0].headers.values()
    assert 'PASS fixture/model: 17 ms' in capsys.readouterr().out


def test_restore_refuses_shared_backup(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    os.link(backup, tmp_path / 'shared-backup.js')
    assert tool.restore_patch() == 1
    assert chunk.read_bytes() == b'var current=1;'


def test_restore_preserves_a_replaced_foreign_lock(tool, tmp_path, monkeypatch):
    chunk, backup = restore_fixture(tool, tmp_path, monkeypatch)
    real_backup = ei.backup_chunk
    lock = Path(str(chunk) + '.identity.lock')
    def replace_lock(path):
        saved = real_backup(path)
        lock.rename(lock.with_suffix('.former-owner'))
        lock.write_text('foreign replacement', encoding='utf-8')
        return saved
    monkeypatch.setattr(ei, 'backup_chunk', replace_lock)
    assert tool.restore_patch() == 1
    assert chunk.read_bytes() == b'var current=1;'
    assert lock.read_text(encoding='utf-8') == 'foreign replacement'
