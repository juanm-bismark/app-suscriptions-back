"""Typed credential DTOs for Tele2 provider adapter."""

from typing import TypedDict


class Tele2Credentials(TypedDict, total=False):
    base_url: str
    # Required by Cisco Control Center REST catalog: Basic base64(username:api_key).
    username: str
    api_key: str
    account_id: str
    api_version: str
    company_id: str
