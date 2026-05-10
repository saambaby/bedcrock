"""Ingestors. Each one polls a single upstream source and yields RawSignals."""

from src.ingestors.base import BaseIngestor, IngestorRegistry
from src.ingestors.earnings import FinnhubEarningsIngestor
from src.ingestors.heavy_movement import HeavyMovementIngestor
from src.ingestors.ohlcv import OHLCVFetcher
from src.ingestors.quiver import QuiverCongressIngestor
from src.ingestors.sec_edgar import SECForm4Ingestor
from src.ingestors.unusual_whales import UWCongressIngestor, UWFlowIngestor

__all__ = [
    "BaseIngestor",
    "IngestorRegistry",
    "FinnhubEarningsIngestor",
    "HeavyMovementIngestor",
    "OHLCVFetcher",
    "QuiverCongressIngestor",
    "SECForm4Ingestor",
    "UWCongressIngestor",
    "UWFlowIngestor",
]
