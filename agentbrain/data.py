"""
Export and deletion — getting your data OUT, and getting rid of it.

Both matter for a tool that ingests your entire conversation history. If someone
can't leave, they shouldn't have arrived.

Deletion is deliberately explicit: it reports exactly what it will remove and how
big it is BEFORE touching anything, and it never guesses at a path.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, consent, secrets


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_json(vault: Path, out: Path) -> Path:
    """Everything as one JSON document. The portable format."""
    recs_p = vault / ".brain" / "records.json"
    records = json.loads(recs_p.read_text(encoding="utf-8")) if recs_p.exists() else []
    notes = {}
    d = vault / "notes"
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            notes[p.name] = p.read_text(encoding="utf-8", errors="replace")
    graph_p = vault / "graph" / "graph.json"
    payload = {
        "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": "agent-brain",
        "conversations": records,
        "notes": notes,
        "graph": json.loads(graph_p.read_text(encoding="utf-8")) if graph_p.exists() else None,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return out


def export_obsidian(vault: Path, out_dir: Path) -> Path:
    """A ready-to-open Obsidian vault: the notes plus an index that links them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "notes").mkdir(exist_ok=True)
    n = 0
    for p in sorted((vault / "notes").glob("*.md")) if (vault / "notes").is_dir() else []:
        shutil.copy2(p, out_dir / "notes" / p.name)
        n += 1
    recs_p = vault / ".brain" / "records.json"
    records = json.loads(recs_p.read_text(encoding="utf-8")) if recs_p.exists() else []
    by_src: dict[str, list] = {}
    for r in records:
        by_src.setdefault(r.get("source", "?"), []).append(r)
    lines = [
        "# Agent Brain export",
        "",
        f"_{n} notes, {len(records)} conversations, "
        f"exported {datetime.now().strftime('%Y-%m-%d')}._",
        "",
    ]
    for src in sorted(by_src):
        rows = sorted(by_src[src], key=lambda r: r.get("started") or "", reverse=True)
        lines += [f"## {src} ({len(rows)})", ""]
        for r in rows[:500]:
            date = (r.get("started") or "")[:10]
            lines.append(f"- {date} — {r.get('title', '?')[:100]}")
        lines.append("")
    (out_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    return out_dir


def export_markdown(vault: Path, out: Path) -> Path:
    """One readable digest file. For skimming or handing to someone."""
    recs_p = vault / ".brain" / "records.json"
    records = json.loads(recs_p.read_text(encoding="utf-8")) if recs_p.exists() else []
    records.sort(key=lambda r: r.get("started") or "", reverse=True)
    lines = [
        "# My AI work history",
        "",
        f"{len(records)} conversations across {len({r.get('source') for r in records})} agents.",
        "",
    ]
    cur = None
    for r in records:
        month = (r.get("started") or "unknown")[:7]
        if month != cur:
            cur = month
            lines += ["", f"## {month}", ""]
        lines.append(f"- **{r.get('source', '?')}** — {r.get('title', '?')[:120]}")
        if r.get("files"):
            lines.append(f"  - files: {', '.join(str(f) for f in r['files'][:5])}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


EXPORTERS = {"json": export_json, "obsidian": export_obsidian, "markdown": export_markdown}


# --------------------------------------------------------------------------- #
# deletion
# --------------------------------------------------------------------------- #
def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def deletion_plan(vault: Path) -> list[dict]:
    """What WOULD be deleted, with sizes. Always shown before anything is removed."""
    targets = [
        (vault / "notes", "conversation notes"),
        (vault / ".brain", "search index and records"),
        (vault / "graph", "semantic graph"),
        (config.config_path(), "settings (no secrets are stored here)"),
        (config.config_dir() / "consent.json", "your privacy choice"),
        (config.config_dir() / "key.dpapi", "encrypted API key blob"),
    ]
    plan = []
    for p, what in targets:
        if p.exists():
            plan.append(
                {
                    "path": str(p),
                    "what": what,
                    "bytes": _size(p),
                    "files": (1 if p.is_file() else sum(1 for f in p.rglob("*") if f.is_file())),
                }
            )
    return plan


def delete_everything(vault: Path, keep_notes: bool = False) -> list[str]:
    """Actually delete. Caller is responsible for having confirmed with the human.

    keep_notes=True removes indexes, graph, settings and the stored key but leaves the
    notes themselves — for someone who wants the tool gone but their history kept.
    """
    done = []
    for item in deletion_plan(vault):
        p = Path(item["path"])
        if keep_notes and p.name == "notes":
            done.append(f"kept {p} ({item['files']} notes)")
            continue
        try:
            if p.is_file():
                p.unlink()
            else:
                shutil.rmtree(p)
            done.append(f"deleted {p}")
        except OSError as e:
            done.append(f"FAILED to delete {p}: {e}")
    _, msg = secrets.delete()
    done.append(f"keystore: {msg}")
    consent.revoke()
    done.append("revoked consent (you will be asked again next time)")
    return done


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Agent Brain data export / deletion")
    ap.add_argument("--vault", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("format", choices=sorted(EXPORTERS))
    e.add_argument("--out", type=Path, required=True)
    d = sub.add_parser("delete")
    d.add_argument("--keep-notes", action="store_true")
    d.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("plan")
    a = ap.parse_args()
    vault = a.vault or config.vault_path()

    if a.cmd == "export":
        out = EXPORTERS[a.format](vault, a.out)
        print(f"exported {a.format} -> {out}")
        return 0

    plan = deletion_plan(vault)
    if not plan:
        print("Nothing to delete.")
        return 0
    total = sum(i["bytes"] for i in plan)
    print("This would permanently delete:\n")
    for i in plan:
        print(f"  {i['bytes'] / 1e6:>8.1f} MB  {i['files']:>5} files  {i['what']}")
        print(f"            {i['path']}")
    print(f"\n  TOTAL {total / 1e6:.1f} MB")
    if a.cmd == "plan":
        return 0
    if not a.yes:
        if input("\nType DELETE to confirm: ").strip() != "DELETE":
            print("Cancelled. Nothing was removed.")
            return 1
    for line in delete_everything(vault, keep_notes=a.keep_notes):
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
