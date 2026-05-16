"""
EdgeLane data-provider abstraction.

Two providers (atlas, tradier) implement the same interface. Selection happens
in config via DATA_PROVIDER=atlas|tradier; downstream callers use:

    from data_providers import get_provider
    provider = get_provider(cfg)
    quote = provider.stock_quote("SPY")
    chain = provider.options_chain("SPY", "2026-05-23")
    gex   = provider.greek_exposures("SPY", 3)

Until the Tradier client and the validation probe land, only the atlas provider
is wired. tests/utils.py continues to work via direct atlas_call(); this module
is the on-ramp for the migration without disturbing existing imports.
"""
from .gex_local import compute_dealer_exposures

__all__ = ["compute_dealer_exposures"]
