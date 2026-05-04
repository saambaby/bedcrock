"""Tests for vault frontmatter rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from src.vault.frontmatter import dump_frontmatter, render_md
from src.vault.writer import VaultWriter


def test_dump_frontmatter_round_trips():
    data = {"ticker": "NVDA", "score": 7.25, "blocked": False, "tags": ["a", "b"]}
    fm = dump_frontmatter(data)
    assert fm.startswith("---\n") and fm.rstrip().endswith("---")
    inner = fm.strip().strip("-").strip()
    parsed = yaml.safe_load(inner)
    assert parsed == data


def test_dump_frontmatter_handles_decimal_and_datetime():
    data = {
        "price": Decimal("123.45"),
        "when": datetime(2026, 5, 3, tzinfo=UTC),
        "path": Path("/tmp/x"),
    }
    fm = dump_frontmatter(data)
    parsed = yaml.safe_load(fm.strip().strip("-").strip())
    assert parsed["price"] == 123.45
    assert parsed["when"].startswith("2026-05-03")
    assert parsed["path"] == "/tmp/x"


def test_render_md_combines_frontmatter_and_body():
    out = render_md({"x": 1}, "  hello world  ")
    assert "---\nx: 1\n---" in out
    assert out.rstrip().endswith("hello world")


def test_vault_slug_is_filesystem_safe():
    assert "/" not in VaultWriter._slug("path/with/slashes")
    assert len(VaultWriter._slug("a" * 200)) <= 80


# ---------------------------------------------------------------------------
# VaultWriter.write_signal and write_position
# ---------------------------------------------------------------------------

import asyncio
import uuid
from unittest.mock import MagicMock

from src.db.models import Action, Mode, SignalSource


def _make_mock_signal():
    """Create a mock Signal with all fields needed by write_signal."""
    sig = MagicMock()
    sig.id = uuid.uuid4()
    sig.mode = Mode.PAPER
    sig.source = SignalSource.QUIVER_CONGRESS
    sig.ticker = "NVDA"
    sig.action = Action.BUY
    sig.disclosed_at = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
    sig.trade_date = None
    sig.size_low_usd = Decimal("50000")
    sig.size_high_usd = Decimal("100000")
    sig.score = 7.5
    sig.score_breakdown = {}
    sig.gate_blocked = False
    sig.gates_failed = []
    sig.raw = {}
    sig.trader = MagicMock()
    sig.trader.slug = "pelosi"
    sig.trader.display_name = "Nancy Pelosi"
    return sig


def _make_mock_position():
    """Create a mock Position with all fields needed by write_position."""
    pos = MagicMock()
    pos.id = uuid.uuid4()
    pos.mode = Mode.PAPER
    pos.ticker = "AAPL"
    pos.side = Action.BUY
    pos.entry_at = datetime(2026, 5, 1, 14, 30, 0, tzinfo=UTC)
    pos.entry_price = Decimal("180.00")
    pos.quantity = Decimal("50")
    pos.stop = Decimal("170.00")
    pos.target = Decimal("200.00")
    pos.setup_at_entry = "breakout"
    pos.trend_at_entry = "uptrend"
    pos.market_regime = "bull"
    pos.source_signal_ids = [str(uuid.uuid4())]
    pos.broker_order_id = "broker-123"
    return pos


def test_write_signal_creates_file_at_correct_path(tmp_path):
    """write_signal creates a .md file in 00 Inbox/ with correct naming."""
    writer = VaultWriter(vault_path=tmp_path)
    sig = _make_mock_signal()

    rel_path = asyncio.get_event_loop().run_until_complete(writer.write_signal(sig))

    assert rel_path.startswith("00 Inbox/")
    assert rel_path.endswith(".md")
    assert "NVDA" in rel_path
    assert "pelosi" in rel_path
    assert "2026-05-03" in rel_path

    full = tmp_path / rel_path
    assert full.exists()


def test_write_signal_contains_frontmatter(tmp_path):
    """Signal file contains valid YAML frontmatter with required keys."""
    writer = VaultWriter(vault_path=tmp_path)
    sig = _make_mock_signal()

    rel_path = asyncio.get_event_loop().run_until_complete(writer.write_signal(sig))

    content = (tmp_path / rel_path).read_text()
    assert content.startswith("---\n")
    # Extract frontmatter
    parts = content.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["type"] == "signal"
    assert fm["ticker"] == "NVDA"
    assert fm["action"] == "buy"
    assert fm["source"] == "quiver_congress"


def test_write_position_creates_file_at_correct_path(tmp_path):
    """write_position creates a .md file in 02 Open Positions/."""
    writer = VaultWriter(vault_path=tmp_path)
    pos = _make_mock_position()

    rel_path = asyncio.get_event_loop().run_until_complete(writer.write_position(pos))

    assert rel_path.startswith("02 Open Positions/")
    assert rel_path.endswith(".md")
    assert "AAPL" in rel_path
    assert "2026-05-01" in rel_path

    full = tmp_path / rel_path
    assert full.exists()


def test_write_position_contains_frontmatter(tmp_path):
    """Position file contains valid YAML frontmatter."""
    writer = VaultWriter(vault_path=tmp_path)
    pos = _make_mock_position()

    rel_path = asyncio.get_event_loop().run_until_complete(writer.write_position(pos))

    content = (tmp_path / rel_path).read_text()
    parts = content.split("---", 2)
    fm = yaml.safe_load(parts[1])
    assert fm["type"] == "position"
    assert fm["status"] == "open"
    assert fm["ticker"] == "AAPL"
    assert fm["side"] == "buy"


def test_atomic_write_no_tmp_file_remains(tmp_path):
    """After write, no .tmp file should remain (atomic rename)."""
    writer = VaultWriter(vault_path=tmp_path)
    sig = _make_mock_signal()

    rel_path = asyncio.get_event_loop().run_until_complete(writer.write_signal(sig))

    full = tmp_path / rel_path
    tmp_file = full.with_suffix(full.suffix + ".tmp")
    assert not tmp_file.exists()
    assert full.exists()
