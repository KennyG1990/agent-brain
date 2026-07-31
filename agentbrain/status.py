"""State for the GUI. No tkinter import here on purpose, so it can be tested headlessly."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, graph, sources


def _age(sec: float) -> str:
    if sec < 90:
        return f"{int(sec)}s ago"
    if sec < 5400:
        return f"{int(sec//60)}m ago"
    if sec < 172800:
        return f"{sec/3600:.1f}h ago"
    return f"{sec/86400:.1f}d ago"


def notes_stats(vault: Path) -> dict:
    d = vault / "notes"
    if not d.is_dir():
        return {"ok": False, "count": 0, "detail": "No notes yet — click Scan My Computer."}
    sizes, newest = [], 0.0
    for p in d.glob("*.md"):
        try:
            st = p.stat()
        except OSError:
            continue
        sizes.append(st.st_size); newest = max(newest, st.st_mtime)
    if not sizes:
        return {"ok": False, "count": 0, "detail": "No notes yet — click Scan My Computer."}
    return {"ok": True, "count": len(sizes),
            "detail": f"{len(sizes)} notes · {sum(sizes)/1e6:.0f} MB · newest {_age(time.time()-newest)}"}


def index_stats(vault: Path) -> dict:
    p = vault / ".brain" / "recall_index.json"
    if not p.exists():
        return {"ok": False, "detail": "Not built — search won't work until you build it."}
    try:
        idx = json.loads(p.read_text(encoding="utf-8"))
        st = p.stat()
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "detail": f"unreadable ({e.__class__.__name__})"}
    n, live = idx.get("n", 0), notes_stats(vault).get("count", 0)
    drift = live - n
    return {"ok": True, "indexed": n, "drift": drift,
            "detail": f"{n} conversations searchable · built {_age(time.time()-st.st_mtime)}"
                      + (f" · {drift:+d} vs notes ⚠ STALE" if drift else "")}


def graph_stats(vault: Path) -> dict:
    p = vault / "graph" / "graph.json"
    if not p.exists():
        return {"ok": False, "detail": "Not built — optional, needs an API key."}
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "detail": "unreadable"}
    nodes, links = g.get("nodes", []), g.get("links", [])
    covered = len({n.get("source_file") for n in nodes if n.get("source_file")})
    live = notes_stats(vault).get("count", 0) or 1
    pct = 100 * covered / live
    warn = "  ⚠ LOW COVERAGE" if pct < 50 else ""
    return {"ok": True, "nodes": len(nodes), "links": len(links), "coverage_pct": pct,
            "detail": f"{len(nodes)} nodes · {len(links)} links · covers {covered}/{live} notes "
                      f"({pct:.0f}%){warn}"}


def next_action(vault: Path, cfg: dict) -> dict:
    src = sources.active(cfg.get("extra_sources") or [])
    n, i, g = notes_stats(vault), index_stats(vault), graph_stats(vault)
    if not src:
        return {"level": "bad", "button": None,
                "text": "No AI session logs found. Use Claude Code / Codex / Gemini CLI first, "
                        "or add a folder in Settings."}
    if not n["ok"]:
        return {"level": "warn", "button": "scan",
                "text": "Click Scan My Computer to import your conversations. Free, no key needed."}
    if not i["ok"] or i.get("drift"):
        return {"level": "warn", "button": "scan",
                "text": "New conversations to import — click Scan My Computer."}
    if not config.has_key(cfg):
        return {"level": "ok", "button": None,
                "text": "Everything works. Search away. (A graph is optional — add an API key "
                        "in Settings if you want one.)"}
    if not g["ok"] or g.get("coverage_pct", 0) < 50:
        return {"level": "warn", "button": "graph",
                "text": "Optional: click Build Graph to map concepts across your history. "
                        "Uses your API key."}
    return {"level": "ok", "button": None, "text": "Everything current. Just use the search box."}


def snapshot(vault: Path, cfg: dict) -> dict:
    return {"notes": notes_stats(vault), "index": index_stats(vault),
            "graph": graph_stats(vault),
            "sources": {"detail": sources.summary(cfg.get("extra_sources") or [])},
            "provider": _provider_line(cfg),
            "next": next_action(vault, cfg)}


def _provider_line(cfg: dict) -> dict:
    r = config.resolve(cfg)
    if not config.has_key(cfg):
        return {"ok": False, "detail": f"No API key ({r['label']}). Optional — search works without it."}
    where = f"from ${r['env_var']}" if r["key_from_env"] else "saved in settings"
    extra = "  · graphify installed" if graph.graphify_available() else ""
    return {"ok": True, "detail": f"{r['label']} · {r['model']} · key {where}{extra}"}
