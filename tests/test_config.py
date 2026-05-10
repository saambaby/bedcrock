"""Tests for src.config — focuses on v2 invariants (mode↔port coupling)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Mode, Settings


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
