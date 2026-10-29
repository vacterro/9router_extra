"""Source-review regressions: only disposable installs and mocked requests."""
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

from core import engine_identity as ei
from core.router_client import ComboWriteError, RouterClient


LEGACY = ('class Base{};class WB extends Base{constructor(){super("workbuddy")}'
          'transformRequest(a){return a}};var reg={id:"workbuddy",alias:"wb",models:[]};')


def install(tmp_path, body=LEGACY):
    root = tmp_path / 'install'
    path = root / ei.SERVER_SUBPATH / 'chunks' / 'fixture.js'
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding='utf-8')
    return root, path


def test_absent_install_cannot_pass_the_upgrade_gate(tmp_path):
    tool = Path(__file__).resolve().parents[2] / 'tools' / 'engine_identity_gate.py'
    spec = importlib.util.spec_from_file_location('identity_gate_under_test', tool)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert ei.ensure(str(tmp_path / 'absent'))['outcome'] == 'UNAVAILABLE'
    assert gate.main(['--install', str(tmp_path / 'absent'), '--json']) == 2
    assert gate.main(['--install', str(tmp_path / 'absent'), '--check-only']) == 2


def test_unrelated_patch_marker_does_not_prove_workbuddy_identity(tmp_path):
    root, _ = install(tmp_path, LEGACY + ';var unrelated="' + ei.PATCH_MARKER + '";')
    assert ei.check_one(ei.ENGINE_IDENTITIES[0], str(root)).state == ei.IDENTITY_MISSING


def test_missing_syntax_checker_preserves_the_original(tmp_path, monkeypatch):
    root, chunk = install(tmp_path)
    original = chunk.read_bytes()
    monkeypatch.setattr(ei.shutil, 'which', lambda _: None)
    monkeypatch.setattr(ei, '_backup_dir', lambda: str(tmp_path / 'rollback'))
    assert not ei.repair_one(ei.ENGINE_IDENTITIES[0], str(root)).ok
    assert chunk.read_bytes() == original


def test_owned_chunk_lock_refuses_a_second_repair_without_touching_it(tmp_path, monkeypatch):
    root, chunk = install(tmp_path)
    marker = Path(str(chunk) + '.identity.lock')
    marker.write_text('foreign writer', encoding='utf-8')
    monkeypatch.setattr(ei, '_backup_dir', lambda: str(tmp_path / 'rollback'))
    original = chunk.read_bytes()
    assert not ei.repair_one(ei.ENGINE_IDENTITIES[0], str(root)).ok
    assert chunk.read_bytes() == original
    assert marker.read_text(encoding='utf-8') == 'foreign writer'


def test_linked_chunk_is_never_repaired(tmp_path, monkeypatch):
    root, chunk = install(tmp_path)
    outside = tmp_path / 'outside.js'
    outside.write_bytes(chunk.read_bytes())
    chunk.unlink()
    try:
        chunk.symlink_to(outside)
    except OSError:
        pytest.skip('Host cannot create disposable symlinks')
    original = outside.read_bytes()
    monkeypatch.setattr(ei, '_backup_dir', lambda: str(tmp_path / 'rollback'))
    assert not ei.repair_one(ei.ENGINE_IDENTITIES[0], str(root)).ok
    assert outside.read_bytes() == original
    assert chunk.is_symlink()


def test_hardlinked_chunk_is_never_repaired(tmp_path, monkeypatch):
    root, chunk = install(tmp_path)
    outside = tmp_path / 'second-link.js'
    os.link(chunk, outside)
    original = outside.read_bytes()
    monkeypatch.setattr(ei, '_backup_dir', lambda: str(tmp_path / 'rollback'))
    assert not ei.repair_one(ei.ENGINE_IDENTITIES[0], str(root)).ok
    assert outside.read_bytes() == original == chunk.read_bytes()


def test_error_diagnostic_is_one_line_and_hides_endpoints():
    error = ComboWriteError('transport', None, 'Failed at http://127.0.0.1:23456/api/combos\nretry')
    assert '127.0.0.1' not in error.message_for_ui()
    assert '\n' not in error.message_for_ui()


@pytest.mark.parametrize('method', ['create_combo', 'update_combo'])
@pytest.mark.parametrize('payload', [b'not-json', b'[]', b'null', b'"text"'])
def test_invalid_success_payload_raises_typed_write_error(monkeypatch, method, payload):
    client = RouterClient(base_url='http://127.0.0.1:23456')
    monkeypatch.setattr(client, '_get_headers', lambda: {})
    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return httpx.Response(200, content=payload)
        put = post
    monkeypatch.setattr(httpx, 'Client', FakeClient)
    kwargs = dict(name='fixture', models=['fixture/model'])
    if method == 'update_combo': kwargs['combo_id'] = 'fixture'
    with pytest.raises(ComboWriteError) as caught:
        getattr(client, method)(**kwargs)
    assert caught.value.kind == 'response'


def test_error_detail_uses_existing_redaction_boundary():
    value = 'canary' * 8
    error = ComboWriteError('http', 500, 'Authorization: Bearer ' + value)
    assert value not in str(error)
    assert value not in error.detail
    assert value not in error.message_for_ui()
