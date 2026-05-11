"""Tests for src.config — focuses on v2 invariants (mode↔port coupling)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Broker, Mode, Settings


def test_mode_port_validator_rejects_mismatch():
    """MODE=paper with a live port must refuse to construct."""
    with pytest.raises((ValidationError, ValueError)):
        Settings(mode=Mode.PAPER, ibkr_port=4001)


def test_mode_port_validator_accepts_match():
    """MODE=paper with the paper Gateway port (4002) is accepted."""
    s = Settings(mode=Mode.PAPER, ibkr_port=4002)
    assert s.mode == Mode.PAPER
    assert s.ibkr_port == 4002


def test_mode_port_validator_accepts_tws_paper():
    """MODE=paper with TWS paper port (7497) is also accepted."""
    s = Settings(mode=Mode.PAPER, ibkr_port=7497)
    assert s.ibkr_port == 7497


def test_mode_port_validator_rejects_live_with_paper_port():
    """MODE=live with the paper Gateway port (4002) must refuse."""
    with pytest.raises((ValidationError, ValueError)):
        Settings(mode=Mode.LIVE, ibkr_port=4002)


def test_mode_port_validator_accepts_live_match():
    """MODE=live with the live Gateway port (4001) is accepted."""
    s = Settings(mode=Mode.LIVE, ibkr_port=4001)
    assert s.mode == Mode.LIVE
    assert s.ibkr_port == 4001


# --- v4 broker-mode truth table ---


def test_broker_ibkr_paper_ok():
    s = Settings(broker=Broker.IBKR, mode=Mode.PAPER, ibkr_port=4002)
    assert s.broker is Broker.IBKR
    assert s.mode == Mode.PAPER


def test_broker_ibkr_paper_with_live_port_rejected():
    with pytest.raises((ValidationError, ValueError)):
        Settings(broker=Broker.IBKR, mode=Mode.PAPER, ibkr_port=4001)


def test_broker_ibkr_live_ok():
    s = Settings(broker=Broker.IBKR, mode=Mode.LIVE, ibkr_port=4001)
    assert s.broker is Broker.IBKR
    assert s.mode == Mode.LIVE


def test_broker_alpaca_paper_with_keys_ok():
    s = Settings(
        broker=Broker.ALPACA,
        mode=Mode.PAPER,
        alpaca_api_key="ak",
        alpaca_api_secret="sk",
    )
    assert s.broker is Broker.ALPACA
    assert s.alpaca_api_key is not None
    assert s.alpaca_api_secret is not None


def test_broker_alpaca_paper_without_keys_rejected():
    with pytest.raises((ValidationError, ValueError)):
        Settings(broker=Broker.ALPACA, mode=Mode.PAPER)


def test_broker_alpaca_live_refused():
    with pytest.raises((ValidationError, ValueError)) as excinfo:
        Settings(
            broker=Broker.ALPACA,
            mode=Mode.LIVE,
            alpaca_api_key="ak",
            alpaca_api_secret="sk",
        )
    assert "US-only" in str(excinfo.value)
