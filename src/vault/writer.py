"""No-op vault writer (Wave C bridge stub).

The real VaultWriter — which renders signal/position/closure markdown into the
Quartz-publishable Obsidian vault — lives in a v0.1 branch that was not merged
into the v2 staging branch. These stubs allow the integrated test surface to
import and exercise scoring/orders/monitor without filesystem side-effects.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def ensure_vault_layout(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
    return None


def write_signal(*_args: Any, **_kwargs: Any) -> Path | None:  # pragma: no cover
    return None


def write_position(*_args: Any, **_kwargs: Any) -> Path | None:  # pragma: no cover
    return None


def write_closure_event(*_args: Any, **_kwargs: Any) -> Path | None:  # pragma: no cover
    return None


def write_draft_order(*_args: Any, **_kwargs: Any) -> Path | None:  # pragma: no cover
    return None


class VaultWriter:
    """No-op writer; methods accept any inputs and return None."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.root: Path | None = None

    @staticmethod
    def _slug(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
        return cleaned[:80]

    async def write_signal(self, *_args: Any, **_kwargs: Any) -> Path | None:
        return None

    async def write_position(self, *_args: Any, **_kwargs: Any) -> Path | None:
        return None

    async def write_closure_event(self, *_args: Any, **_kwargs: Any) -> Path | None:
        return None

    async def write_draft_order(self, *_args: Any, **_kwargs: Any) -> Path | None:
        return None
