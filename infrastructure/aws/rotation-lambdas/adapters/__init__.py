"""Vendor adapter registry for secret rotation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base_adapter import VendorAdapter


def get_adapter(adapter_name: str) -> VendorAdapter:
    """Get a vendor adapter by name.

    Lazy imports to avoid loading unnecessary vendor SDKs.
    """
    if adapter_name == "stripe":
        from .stripe_adapter import StripeAdapter
        return StripeAdapter()
    elif adapter_name == "twilio":
        from .twilio_adapter import TwilioAdapter
        return TwilioAdapter()
    elif adapter_name == "openai":
        from .openai_adapter import OpenAIAdapter
        return OpenAIAdapter()
    elif adapter_name == "internal":
        from .internal_adapter import InternalAdapter
        return InternalAdapter()
    elif adapter_name == "supabase":
        from .supabase_adapter import SupabaseAdapter
        return SupabaseAdapter()
    else:
        raise ValueError(f"Unknown adapter: {adapter_name}")
