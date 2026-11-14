"""
research_system
===============
Decision-support research and recommendation system for Indian listed equities.

Four independent strategy engines (investment, swing, equity derivatives,
intraday short) produce CANDIDATE ideas. Shared services (market state, data
quality, company/event intelligence) inform all of them. A deterministic
central risk engine has final veto. The default output is NO TRADE.

This package never places orders. See docs/ARCHITECTURE.md.
"""

__version__ = "0.1.0"
