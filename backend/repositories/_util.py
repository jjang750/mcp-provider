"""Shared repository helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now() -> str:
    """ISO-8601 UTC timestamp string (Assumption 10)."""
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    """Serialize a Python value to JSON text for a TEXT column."""
    return json.dumps(value, ensure_ascii=False)


def loads(text: Optional[str], default: Any = None) -> Any:
    """Deserialize JSON text from a TEXT column; ``default`` on NULL/empty."""
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default
