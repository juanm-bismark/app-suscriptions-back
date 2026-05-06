"""Unit tests for Moabits admin write operations."""

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
        await adapter.set_administrative_status(
            '8934070100000000001',
            {'base_url': 'https://moabits.test', 'api_key': 'k'},
            target=AdministrativeStatus.ACTIVE,
            idempotency_key='idem',
            data_service=True,
        )


@pytest.mark.asyncio
async def test_moabits_active_calls_documented_put_when_enabled(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    captured = {}

    async def fake_put(creds, path, body, idempotency_key=None):
        captured['path'] = path
        captured['body'] = body
        captured['idempotency_key'] = idempotency_key
        return {}

    monkeypatch.setattr(moabits_mod.adapter, '_put', fake_put)

    adapter = MoabitsAdapter()
    await adapter.set_administrative_status(
        '8934070100000000001',
        {'base_url': 'https://moabits.test', 'api_key': 'k'},
        target=AdministrativeStatus.ACTIVE,
        idempotency_key='idem',
        data_service=True,
        sms_service=False,
    )

    assert captured.get('path') == '/api/sim/active/'
    assert captured.get('body') == {
        'iccidList': ['8934070100000000001'],
        'dataService': True,
        'smsService': False,
    }
    assert captured.get('idempotency_key') == 'idem'


@pytest.mark.asyncio
async def test_moabits_suspend_calls_documented_put_when_enabled(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    captured = {}

    async def fake_put(creds, path, body, idempotency_key=None):
        captured['path'] = path
        captured['body'] = body
        return {}

    monkeypatch.setattr(moabits_mod.adapter, '_put', fake_put)

    await MoabitsAdapter().set_administrative_status(
        '8934070100000000001',
        {'base_url': 'https://moabits.test', 'api_key': 'k'},
        target=AdministrativeStatus.SUSPENDED,
        idempotency_key='idem',
        data_service=False,
        sms_service=True,
    )

    assert captured.get('path') == '/api/sim/suspend/'
    assert captured.get('body') == {
        'iccidList': ['8934070100000000001'],
        'dataService': False,
        'smsService': True,
    }


@pytest.mark.asyncio
async def test_moabits_active_suspend_require_at_least_one_service(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    with pytest.raises(ProviderValidationError) as excinfo:
        await MoabitsAdapter().set_administrative_status(
            '8934070100000000001',
            {'base_url': 'https://moabits.test', 'api_key': 'k'},
            target=AdministrativeStatus.ACTIVE,
            idempotency_key='idem',
            data_service=False,
            sms_service=False,
        )

    assert excinfo.value.detail == "No service to active"


@pytest.mark.asyncio
async def test_moabits_rejects_other_status_writes(monkeypatch):
    monkeypatch.setattr(moabits_mod.adapter, 'get_settings', lambda: SimpleNamespace(lifecycle_writes_enabled=True))

    with pytest.raises(UnsupportedOperation):
        await MoabitsAdapter().set_administrative_status(
            '8934070100000000001',
            {'base_url': 'https://moabits.test', 'api_key': 'k'},
            target=AdministrativeStatus.PURGED,
            idempotency_key='idem',
            data_service=True,
        )


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
