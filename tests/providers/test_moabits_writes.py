"""Unit tests for Moabits write operations (set_administrative_status and purge) behind feature flag."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.providers.moabits.adapter import MoabitsAdapter
from app.providers import moabits as moabits_mod
from app.shared.errors import ProviderValidationError, UnsupportedOperation
from app.subscriptions.domain import AdministrativeStatus


@pytest.mark.asyncio
async def test_moabits_set_administrative_status_respects_flag(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=False))
    adapter = MoabitsAdapter()
    with pytest.raises(UnsupportedOperation):
        await adapter.set_administrative_status('8934070100000000001', {}, target=AdministrativeStatus.ACTIVE, idempotency_key='k')


@pytest.mark.asyncio
async def test_moabits_set_administrative_status_calls_put_when_enabled(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    captured = {}

    async def fake_put(creds, path, body, idempotency_key=None):
        captured['creds'] = creds
        captured['path'] = path
        captured['body'] = body
        captured['idempotency_key'] = idempotency_key
        return {}

    monkeypatch.setattr(moabits_mod.adapter, '_put', fake_put)

    adapter = MoabitsAdapter()
    await adapter.set_administrative_status('8934070100000000001', {'base_url': 'https://moabits.test', 'api_key': 'k'}, target=AdministrativeStatus.ACTIVE, idempotency_key='idem')

    assert captured.get('path') == '/api/sim/active/'
    assert 'iccidList' in captured.get('body', {})


@pytest.mark.asyncio
async def test_moabits_purge_respects_flag_and_calls_put_when_enabled(monkeypatch):
    adapter = MoabitsAdapter()

    # flag disabled -> raise
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=False))
    with pytest.raises(UnsupportedOperation):
        await adapter.purge('8934070100000000001', {}, idempotency_key='k')

    # flag enabled -> call _put
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))
    captured = {}

    async def fake_put2(creds, path, body, idempotency_key=None):
        captured['path'] = path
        captured['body'] = body
        captured['idempotency_key'] = idempotency_key
        return {"status": "Ok", "info": {"purged": True}}

    monkeypatch.setattr(moabits_mod.adapter, '_put', fake_put2)
    await adapter.purge('8934070100000000001', {'base_url': 'https://moabits.test', 'api_key': 'k'}, idempotency_key='idem')
    assert captured.get('path') == '/api/sim/purge/'
    assert captured.get('body') == {'iccidList': ['8934070100000000001']}
    assert captured.get('idempotency_key') == 'idem'


@pytest.mark.asyncio
async def test_moabits_purge_requires_purged_confirmation(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    async def fake_put(creds, path, body, idempotency_key=None):
        return {"status": "Ok", "info": {"purged": False}}

    monkeypatch.setattr(moabits_mod.adapter, '_put', fake_put)

    with pytest.raises(ProviderValidationError):
        await MoabitsAdapter().purge(
            '8934070100000000001',
            {'base_url': 'https://moabits.test', 'api_key': 'k'},
            idempotency_key='idem',
        )
