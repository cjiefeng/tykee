"""`extra=` keys must not clash with LogRecord attributes: logging raises KeyError at the call
site ("Attempt to overwrite 'created' in LogRecord"), which crashed startup in production."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def _extra_keys() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                    for key in kw.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            found.append((str(path.relative_to(APP)), node.lineno, key.value))
    return found


def test_no_log_extra_key_overwrites_a_logrecord_attribute() -> None:
    keys = _extra_keys()
    assert keys, "the scan found no extra= keys; is it still looking in the right place?"
    clashes = [f"{p}:{line} {k!r}" for p, line, k in keys if k in RESERVED]
    assert clashes == []


def test_pytest_builds_records_for_info_calls() -> None:
    """Guards the pyproject `log_level`: dynamic keys (``extra=stats``) are only checked when a
    record is actually built during tests."""
    assert logging.getLogger("app").isEnabledFor(logging.INFO)
