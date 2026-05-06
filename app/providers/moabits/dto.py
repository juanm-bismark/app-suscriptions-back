"""Typed credential DTOs for Moabits provider adapter."""

from typing import TypedDict


class MoabitsCredentials(TypedDict, total=False):
    base_url: str
    api_key: str
    company_codes: list[str]
    company_id: str
