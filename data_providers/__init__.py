"""
EdgeLane data-provider abstraction.

Tradier is the sole data provider. It supplies raw quotes + options chains;
dealer GEX is computed locally from the chain. Downstream callers use:

    from data_providers import get_provider
    provider = get_provider(cfg)
    quote = provider.stock_quote("SPY")
    chain = provider.options_chain("SPY", "2026-05-23")
    gex   = provider.greek_exposures("SPY", 3)
"""
from .gex_local import compute_dealer_exposures

__all__ = ["compute_dealer_exposures"]
