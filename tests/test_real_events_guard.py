"""Static guard: every analytics query on ``events`` excludes test events.

A ``.where(...)`` that references ``Event.`` must also contain ``REAL_EVENTS``,
unless the statement carries the marker comment
``# includes test events on purpose`` (debug views). ``delete(Event)`` is
exempt (retention must delete test rows too). Raw SQL strings that read
``FROM events`` must contain ``NOT is_test``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
MARKER = "# includes test events on purpose"
_EVENT_REF = re.compile(r"\bEvent\.")
_FROM_EVENTS = re.compile(r"\bFROM\s+events\b", re.IGNORECASE)


def _is_delete_chain(node: ast.AST) -> bool:
    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id == "delete"
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source)
    lines = source.splitlines()
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "where"
        ):
            args_src = " ".join(ast.get_source_segment(source, a) or "" for a in node.args)
            if not _EVENT_REF.search(args_src) or "REAL_EVENTS" in args_src:
                continue
            if _is_delete_chain(node.func.value):
                continue
            start = max(node.lineno - 3, 0)
            window = "\n".join(lines[start : node.end_lineno])
            if MARKER in window:
                continue
            out.append(f"{filename}:{node.lineno}: .where() on Event without REAL_EVENTS")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _FROM_EVENTS.search(node.value) and "NOT is_test" not in node.value:
                out.append(f"{filename}:{node.lineno}: raw SQL on events without NOT is_test")
    return out


def test_guard_catches_unfiltered_query():
    bad = (
        "q = select(Event).where(Event.project_id == pid)\n"
        "s = text('SELECT 1 FROM events WHERE project_id = :p')\n"
    )
    assert len(find_violations(bad)) == 2


def test_guard_accepts_filtered_marked_and_delete():
    ok = (
        "q = select(Event).where(Event.project_id == pid, REAL_EVENTS)\n"
        "# includes test events on purpose\n"
        "r = select(Event).where(Event.project_id == pid)\n"
        "d = delete(Event).where(Event.received_at < cutoff)\n"
        "s = text('SELECT 1 FROM events WHERE project_id = :p AND NOT is_test')\n"
    )
    assert find_violations(ok) == []


def test_no_unfiltered_event_queries_in_app():
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = str(path.relative_to(APP_DIR.parent))
        violations += find_violations(path.read_text(), rel)
    assert violations == [], "Unfiltered event queries:\n" + "\n".join(violations)
