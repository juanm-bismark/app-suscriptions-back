"""Provider registry — maps Provider enum → adapter singleton.

Built once at startup and stored in app.state.provider_registry.
Adapters are registered by name; unknown providers raise KeyError.
"""

from app.providers.base import Provider, SubscriptionProvider
from app.shared.errors import ProviderUnavailable


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SubscriptionProvider] = {}

    def register(self, provider: Provider, adapter: SubscriptionProvider) -> None:
        self._adapters[provider.value] = adapter

    def get(self, provider: str) -> SubscriptionProvider:
        try:
            return self._adapters[provider]
        except KeyError:
            raise ProviderUnavailable(detail=f"No adapter registered for provider '{provider}'")

    def registered_providers(self) -> list[str]:
        return list(self._adapters)
