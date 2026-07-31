"""
Agent Brain command line — everything the GUI does, without needing a window.

Exists because tkinter is the single most common thing missing from a fresh Python
install, and because servers and CI have no display at all. Nothing here requires
the GUI to be importable.

    python -m agentbrain.cli check
    python -m agentbrain.cli scan
    python -m agentbrain.cli search "how did we set up the printer"
    python -m agentbrain.cli graph --dry-run
    python -m agentbrain.cli graph --max-spend 5
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import config, consent, data, graph, models, normalize, providers, recall
from . import scheduler, secrets, sources, status


def _vault(a) -> Path:
    return a.vault or config.vault_path()


def cmd_check(a) -> int:
    print(f"Python     : {sys.version.split()[0]}   ({sys.executable})")
    try:
        import tkinter  # noqa: F401  -- availability probe, not a use
        print("tkinter    : yes")
    except ImportError:
        print("tkinter    : MISSING - the GUI will not start, the CLI still works")
    print(f"keystore   : {secrets.backend()}")
    print(f"scheduler  : {scheduler.backend()}")
    print(f"graphify   : {'installed' if graph.graphify_available() else 'not installed (optional)'}")
    print(f"vault      : {_vault(a)}")
    print("\nSession logs on this computer:")
    for s in sources.discover(config.load().get("extra_sources")):
        mark = "  OK " if (s.found and s.files) else ("  -- " if s.found else "     ")
        note = f"   [{s.note}]" if s.note else ""
        print(f"{mark}{s.name:<28}{s.files:>6} files  {s.path}{note}")
    return 0


def cmd_scan(a) -> int:
    cfg = config.load()
    v = _vault(a)
    srcs = sources.active(cfg.get("extra_sources"))
    if not srcs:
        print("No agent session logs found. Nothing to scan.", file=sys.stderr)
        return 1
    normalize.run(v, srcs, cfg.get("max_msg_chars", 4000),
                  cfg.get("max_note_chars", 60000))
    recall.cmd_index(v)
    return 0


def cmd_search(a) -> int:
    v = _vault(a)
    try:
        hits, total = recall.search(v, " ".join(a.query), a.top)
    except FileNotFoundError:
        print("No index yet. Run:  python -m agentbrain.cli scan", file=sys.stderr)
        return 2
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if not hits:
        print(f"No matches across {total} conversations.")
        print("Absence here does not prove it never happened - the current session is "
              "never indexed.")
        return 0
    print(f"{len(hits)} of {total} conversations:\n")
    for h in hits:
        print(f"[{h['score']}] {h['title']}")
        print(f"   {h['source']} - {h['date'] or 'undated'} - notes/{h['note']}")
        if h["topics"]:
            print("   " + ", ".join(h["topics"]))
        print()
    return 0


def cmd_graph(a) -> int:
    v, cfg = _vault(a), config.load()
    res = config.resolve(cfg)
    todo = graph.pending(v)
    if not todo:
        print("Graph is already current - nothing to extract.")
        return 0
    est_in = sum(t[2] for t in todo)
    print(f"{len(todo)} note(s) to extract, ~{est_in/1e6:.1f}M input tokens.")
    if a.dry_run:
        print(f"Provider  : {res['label']}  {res['model']}")
        print(f"Consent   : {'given' if consent.has_consent() else 'NOT GIVEN - would be refused'}")
        print("Dry run - nothing was sent and nothing was spent.")
        return 0
    ok, why = consent.gate(res["base_url"])
    if not ok:
        print(f"Refusing to upload: {why}", file=sys.stderr)
        print("Run:  python -m agentbrain.cli consent --accept   (or --local-only)",
              file=sys.stderr)
        return 3
    meter = providers.Meter(a.price_in, a.price_out, a.max_spend)
    try:
        out = graph.build(v, res, meter=meter, workers=a.workers, log=print)
    except providers.SpendCap as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    print(f"\n{out}")
    print(f"spend: {meter.line()}")
    return 0


def cmd_consent(a) -> int:
    if a.show:
        print(consent.SUMMARY)
        print(f"\nCurrent: answered={consent.answered()} "
              f"upload_allowed={consent.has_consent()} local_only={consent.is_local_only()}")
        return 0
    if a.revoke:
        consent.revoke(); print("Consent revoked. You will be asked again."); return 0
    if a.local_only:
        consent.record(accepted=False, local_only=True)
        print("Recorded: LOCAL ONLY. Nothing will ever be uploaded.")
        return 0
    if a.accept:
        print(consent.SUMMARY)
        consent.record(accepted=True)
        print("\nRecorded: uploads allowed for the graph step.")
        return 0
    print(consent.SUMMARY)
    print("\nRe-run with --accept, --local-only, --revoke or --show.")
    return 0


def cmd_models(a) -> int:
    ranked = models.rank(models.fetch(a.key), a.notes or 500, a.top)
    print(f"{'model':<46}{'$/Min':>8}{'$/Mout':>8}{'think':>7}{'struct':>7}{'job $':>8}")
    for r in ranked:
        print(f"{r['id']:<46}{r['in']:>8.3f}{r['out']:>8.3f}"
              f"{'YES' if r['thinks'] else 'no':>7}{'yes' if r['structured'] else 'NO':>7}"
              f"{r['cost']:>8.2f}")
    print()
    print(models.explain(ranked[0], a.notes or 500))
    return 0


def cmd_status(a) -> int:
    snap = status.snapshot(_vault(a), config.load())
    for k in ("sources", "notes", "index", "graph", "provider"):
        print(f"  {k:<10} {snap[k]['detail']}")
    print(f"\n  NEXT: {snap['next']['text']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="agentbrain", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="can this machine run it, and what history exists")
    sub.add_parser("scan", help="import conversations and rebuild the index (free)")
    sub.add_parser("status", help="current state")

    s = sub.add_parser("search", help="search your history (free)")
    s.add_argument("query", nargs="+"); s.add_argument("--top", type=int, default=6)

    g = sub.add_parser("graph", help="build the semantic graph (COSTS MONEY)")
    g.add_argument("--dry-run", action="store_true", help="show scope and cost, send nothing")
    g.add_argument("--max-spend", type=float, default=0.0, help="hard cap in USD")
    g.add_argument("--workers", type=int, default=4)
    g.add_argument("--price-in", type=float, default=0.0, help="$ per 1M input tokens")
    g.add_argument("--price-out", type=float, default=0.0, help="$ per 1M output tokens")

    c = sub.add_parser("consent", help="review or change what may leave this computer")
    c.add_argument("--accept", action="store_true"); c.add_argument("--local-only", action="store_true")
    c.add_argument("--revoke", action="store_true"); c.add_argument("--show", action="store_true")

    m = sub.add_parser("models", help="rank OpenRouter models for extraction")
    m.add_argument("--key", default=""); m.add_argument("--top", type=int, default=15)
    m.add_argument("--notes", type=int, default=0)

    e = sub.add_parser("export", help="get your data out")
    e.add_argument("format", choices=sorted(data.EXPORTERS)); e.add_argument("--out", type=Path, required=True)

    d = sub.add_parser("delete", help="remove everything this tool stored")
    d.add_argument("--keep-notes", action="store_true"); d.add_argument("--yes", action="store_true")

    k = sub.add_parser("schedule", help="run the free scan automatically")
    k.add_argument("action", choices=["install", "uninstall", "status"])
    k.add_argument("--hour", type=int, default=3)

    sub.add_parser("mcp", help="print MCP registration config for your agents")

    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(f"exported -> {data.EXPORTERS[a.format](_vault(a), a.out)}"); return 0
    if a.cmd == "delete":
        return _delete(a)
    if a.cmd == "schedule":
        ok, msg = {"install": lambda: scheduler.install(_vault(a), a.hour),
                   "uninstall": scheduler.uninstall, "status": scheduler.status}[a.action]()
        print(("OK: " if ok else "FAILED: ") + str(msg)); return 0 if ok else 1
    if a.cmd == "mcp":
        from . import mcp_server
        return mcp_server.main(["--install-help", "--vault", str(_vault(a))])
    return {"check": cmd_check, "scan": cmd_scan, "search": cmd_search, "graph": cmd_graph,
            "consent": cmd_consent, "models": cmd_models, "status": cmd_status}[a.cmd](a)


def _delete(a) -> int:
    v = _vault(a)
    plan = data.deletion_plan(v)
    if not plan:
        print("Nothing to delete."); return 0
    total = sum(i["bytes"] for i in plan)
    print("This would permanently delete:\n")
    for i in plan:
        print(f"  {i['bytes']/1e6:>8.1f} MB  {i['files']:>5} files  {i['what']}")
    print(f"\n  TOTAL {total/1e6:.1f} MB")
    if not a.yes and input("\nType DELETE to confirm: ").strip() != "DELETE":
        print("Cancelled."); return 1
    for line in data.delete_everything(v, keep_notes=a.keep_notes):
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
