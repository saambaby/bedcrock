"""Minimal frontmatter helpers (Wave C bridge stub)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


def _coerce(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    return value


def dump_frontmatter(data: dict[str, Any]) -> str:
    safe = {k: _coerce(v) for k, v in data.items()}
    body = yaml.safe_dump(safe, sort_keys=False, default_flow_style=False).rstrip()
    return f"---\n{body}\n---\n"


def render_md(data: dict[str, Any], body: str) -> str:
    return f"{dump_frontmatter(data)}\n{body.strip()}\n"
