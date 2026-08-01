#!/usr/bin/env python3
"""
A stdlib approximation of the checks ruff and mypy will run.

WHY THIS EXISTS: ruff and mypy need `pip install`, and this project's whole promise is
that it needs nothing. This runs anywhere Python does, catches the highest-value subset
(unused imports, unused locals, undefined names, mutable defaults, bare excepts), and
means a contributor with no dev tooling can still get the important feedback.

It does NOT replace ruff. `pre-commit run --all-files` is the real gate. This is the
floor, not the ceiling.

    python tools/selfcheck.py
    python tools/selfcheck.py --fix-hints
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["agentbrain", "tools", "tests"]


class Visitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.problems: list[tuple[int, str, str]] = []
        self.imported: dict[str, int] = {}
        self.used: set[str] = set()
        self.star = False

    # ---- imports ----
    def visit_Import(self, n):
        for a in n.names:
            if a.name == "*":
                self.star = True
            self.imported[(a.asname or a.name).split(".")[0]] = n.lineno
        self.generic_visit(n)

    def visit_ImportFrom(self, n):
        for a in n.names:
            if a.name == "*":
                self.star = True
                continue
            self.imported[a.asname or a.name] = n.lineno
        self.generic_visit(n)

    def visit_Name(self, n):
        self.used.add(n.id)
        self.generic_visit(n)

    def visit_Attribute(self, n):
        cur = n
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name):
            self.used.add(cur.id)
        self.generic_visit(n)

    # ---- real bug shapes ----
    def visit_FunctionDef(self, n):
        for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
            if isinstance(d, ast.List | ast.Dict | ast.Set):
                self.problems.append(
                    (d.lineno, "B006", f"mutable default in {n.name}() - shared between calls")
                )
        self.generic_visit(n)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, n):
        if n.type is None:
            self.problems.append(
                (n.lineno, "E722", "bare except - catches KeyboardInterrupt and SystemExit too")
            )
        self.generic_visit(n)

    def visit_Compare(self, n):
        for op, cmp in zip(n.ops, n.comparators):
            if (
                isinstance(op, ast.Is | ast.IsNot)
                and isinstance(cmp, ast.Constant)
                and isinstance(cmp.value, str | int | float)
                and cmp.value is not None
            ):
                self.problems.append((n.lineno, "F632", "`is` with a literal - use == "))
        self.generic_visit(n)

    def visit_Assert(self, n):
        if isinstance(n.test, ast.Tuple) and n.test.elts:
            self.problems.append((n.lineno, "F631", "assert on a tuple is always true"))
        self.generic_visit(n)


def dead_locals(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Assigned, never read, not a throwaway. This is the class of bug that left
    `_finish_note = ''` sitting in graph.py after a refactor."""
    out = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]:
        assigned: dict[str, int] = {}
        read: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    assigned.setdefault(n.id, n.lineno)
                else:
                    read.add(n.id)
            elif isinstance(n, ast.Attribute):
                cur = n
                while isinstance(cur, ast.Attribute):
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    read.add(cur.id)
        for name, line in assigned.items():
            if name in read or name.startswith("_") or name in ("self", "cls"):
                continue
            out.append((line, "F841", f"local `{name}` assigned but never used"))
    return out


def check(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [(e.lineno or 0, "E999", f"syntax error: {e.msg}")]
    v = Visitor(path)
    v.visit(tree)
    problems = list(v.problems) + dead_locals(tree)

    if not v.star:
        text_after = src
        for name, line in v.imported.items():
            if name in v.used:
                continue
            # __future__ and re-exports in __init__ are legitimate
            if path.name == "__init__.py" or name == "annotations":
                continue
            if f'"{name}"' in text_after or f"'{name}'" in text_after:
                continue
            problems.append((line, "F401", f"`{name}` imported but unused"))

    # non-ASCII in a file Windows tooling may read as ANSI
    if path.suffix in (".ps1", ".bat", ".cmd"):
        for i, line in enumerate(src.splitlines(), 1):
            if any(ord(c) > 127 for c in line):
                problems.append((i, "ASCII", "non-ASCII in a Windows script"))

    # Honour noqa suppressions the way ruff does, so a deliberate suppression silences BOTH
    # tools and a contributor never has to justify the same line twice.
    lines = src.splitlines()
    kept = []
    for line_no, code, msg in problems:
        raw = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        if "# noqa" in raw:
            after = raw.split("# noqa", 1)[1].lstrip()
            if not after.startswith(":") or code in after:
                continue
        kept.append((line_no, code, msg))
    return sorted(kept)


def main() -> int:
    files: list[Path] = []
    for t in TARGETS:
        d = ROOT / t
        if d.is_dir():
            files += sorted(d.rglob("*.py"))
    for extra in ("app.py",):
        if (ROOT / extra).is_file():
            files.append(ROOT / extra)

    total = 0
    for f in files:
        probs = check(f)
        if probs:
            print(f"\n{f.relative_to(ROOT)}")
            for line, code, msg in probs:
                print(f"  {line:>4}  {code:<6} {msg}")
            total += len(probs)
    print(f"\n{len(files)} files checked, {total} finding(s).")
    if total:
        print("Run `pre-commit run --all-files` for the full ruff/mypy pass.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
