"""Indicator computation."""
from src.indicators.compute import SECTOR_ETF, IndicatorComputer

# Alias for callers that prefer the more verbose name
DEFAULT_SECTOR_ETFS = SECTOR_ETF

__all__ = ["IndicatorComputer", "SECTOR_ETF", "DEFAULT_SECTOR_ETFS"]
