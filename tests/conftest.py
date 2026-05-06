"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def moabits_creds() -> dict:
    return {
        "base_url": "https://api.moabits.test",
        "x_api_key": "test-key",
        "company_codes": ["ACME"],
        "company_id": "00000000-0000-0000-0000-000000000001",
    }


@pytest.fixture
def kite_creds() -> dict:
    return {
        "endpoint": "https://kite.test/soap",
        "username": "kite-user",
        "password": "kite-pass",
        "company_id": "00000000-0000-0000-0000-000000000001",
    }
