from app.providers.base import SearchableProvider, SubscriptionProvider
from app.providers.kite.adapter import KiteAdapter
from app.providers.moabits.adapter import MoabitsAdapter
from app.providers.tele2.adapter import Tele2Adapter


def test_core_adapters_implement_subscription_provider_protocol() -> None:
    for adapter in (KiteAdapter(), Tele2Adapter(), MoabitsAdapter()):
        assert isinstance(adapter, SubscriptionProvider)


def test_core_adapters_expose_provider_scoped_listing_capability() -> None:
    for adapter in (KiteAdapter(), Tele2Adapter(), MoabitsAdapter()):
        assert isinstance(adapter, SearchableProvider)
