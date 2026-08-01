#!/usr/bin/env python3
"""Fail if a Windows script contains a byte above 0x7F.

PowerShell 5.1 decodes BOM-less files as ANSI. A UTF-8 em-dash (E2 80 94) becomes
three chars, one of which is a double quote - which opens a string literal and breaks
parsing for the ENTIRE script, with errors pointing at innocent lines far away.

This cost one silently failed scheduled run on 2026-07-31. Never again.
"""

from __future__ import annotations

import sys
from pathlib import Path

SUFFIXES = {".ps1", ".bat", ".cmd"}


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or [
        p for p in Path(".").rglob("*") if p.suffix.lower() in SUFFIXES
    ]
    bad = 0
    for p in paths:
        if p.suffix.lower() not in SUFFIXES or not p.is_file():
            continue
        raw = p.read_bytes()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            offenders = {c for c in line if ord(c) > 127}
            if offenders and not has_bom:
                chars = " ".join(f"{c!r}(U+{ord(c):04X})" for c in sorted(offenders))
                print(f"{p}:{i}: non-ASCII in a BOM-less Windows script: {chars}")
                bad += 1
    if bad:
        print(f"\n{bad} line(s). Replace with ASCII, or save the file as UTF-8 WITH BOM.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
