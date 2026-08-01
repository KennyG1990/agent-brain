#!/usr/bin/env python3
"""
Agent Brain - recall over the NOTES (the corpus that actually exists)
====================================================================
Replaces query_brain.py for day-to-day recall.

WHY THIS EXISTS (measured 2026-07-31, not assumed):
  * query_brain.py's neighbors() looks for node ids prefixed "s:" / "t:".
    The current graphify graph uses "topic-<term>" and "<slug>-<hash8>".
    0 of 201 nodes carry the old prefixes -> related_sessions was ALWAYS empty
    and the graph was never actually consulted. It was substring matching over
    records.json wearing a GraphRAG docstring.
  * Its score() counted substrings across concatenated absolute file paths, so a
    session touching 60 files under .../x4-forge/... buried a session that was
    genuinely about the query. Top hits for "x4 forge validation" were two
    duplicate "<local-command-caveat>" sessions.
  * graph.json indexes ~27-44 conversations. notes/ holds 573. The semantic
    graph covers roughly 5% of history, so retrieval MUST be note-first.

So: BM25 over notes/ (full coverage), with graph.json used only to enrich a hit
that happens to be in it. Honest about which tier answered.

Usage:
  python brain_recall.py index                     # build/refresh the index
  python brain_recall.py query "x4 forge validation"
  python brain_recall.py query --json --top 8 "how did we fix the bridge"
  # --vault defaults to the parent of this script; override for odd mounts:
  python brain_recall.py --vault /mnt/DEV_ENV/"Agent Brain Vault" query "..."
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_VAULT = Path(__file__).resolve().parents[1]
INDEX_REL = Path(".brain") / "recall_index.json"
BODY_CAP = 40000  # chars of body indexed per note; titles/topics carry the signal
K1, B = 1.5, 0.75  # BM25

STOP = set(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "by",
        "from",
        "into",
        "out",
        "up",
        "down",
        "off",
        "over",
        "under",
        "your",
        "you",
        "we",
        "our",
        "i",
        "me",
        "my",
        "he",
        "she",
        "they",
        "them",
        "their",
        "but",
        "if",
        "then",
        "else",
        "so",
        "do",
        "does",
        "did",
        "done",
        "can",
        "could",
        "should",
        "would",
        "will",
        "just",
        "not",
        "no",
        "yes",
        "new",
        "use",
        "used",
        "using",
        "get",
        "got",
        "make",
        "made",
        "work",
        "works",
        "working",
        "file",
        "files",
        "run",
        "running",
        "help",
        "need",
        "want",
        "like",
        "about",
        "more",
        "most",
        "some",
        "any",
        "all",
        "each",
        "both",
        "few",
        "very",
        "much",
        "many",
        "now",
        "here",
        "there",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "how",
        "why",
        "has",
        "have",
        "had",
        "having",
        "also",
        "than",
        "too",
        "only",
        "same",
        "other",
        "another",
        "such",
        "own",
        "said",
        "say",
        "says",
        "get",
        "gets",
        "go",
        "goes",
        "going",
        "one",
        "two",
        "three",
        "first",
        "second",
        "next",
        "last",
        "still",
        "even",
        "back",
        "way",
        "thing",
        "things",
        "something",
    ]
)
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]{1,}", re.I)
FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "") if t.lower() not in STOP and len(t) > 2]


def parse_front_matter(raw: str) -> tuple[dict, str]:
    m = FM_RE.match(raw)
    if not m:
        return {}, raw
    fm, body = {}, raw[m.end() :]
    key = None
    for line in m.group(1).splitlines():
        if line.startswith("  - "):
            if key:
                fm.setdefault(key, []).append(line[4:].strip().strip('"'))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"')
            fm[key] = val if val else []
    return fm, body


def load_graph(vault: Path) -> dict:
    """basename(note) -> {community, related[]}. Enrichment only; may be empty."""
    out: dict[str, dict] = {}
    for cand in (vault / "graph.json", vault / "graphify-out" / "graph.json"):
        if not cand.exists():
            continue
        try:
            g = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        adj: dict[str, set] = defaultdict(set)
        for l in g.get("links", []):
            a = l.get("source")
            b = l.get("target")
            a = a.get("id") if isinstance(a, dict) else a
            b = b.get("id") if isinstance(b, dict) else b
            if a and b:
                adj[a].add(b)
                adj[b].add(a)
        labels = {n.get("id"): (n.get("label") or n.get("id")) for n in g.get("nodes", [])}
        for n in g.get("nodes", []):
            sf = n.get("source_file")
            if not sf:
                continue
            rel = [
                labels.get(x, x)
                for x in adj.get(n.get("id"), [])
                if not str(x).startswith("topic-")
            ]
            out[Path(sf.replace("\\", "/")).name] = {
                "community": n.get("community_name") or n.get("community"),
                "related": rel[:5],
            }
        break
    return out


def cmd_index(vault: Path) -> int:
    notes_dir = vault / "notes"
    if not notes_dir.is_dir():
        print(f"ERROR: no notes/ under {vault}", file=sys.stderr)
        return 2
    docs, df = [], Counter()
    PART_RE = re.compile(r"-part\d+\.md$", re.I)
    for p in sorted(notes_dir.glob("*.md")):
        if PART_RE.search(p.name):
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, body = parse_front_matter(raw)

        # Concatenate body text from all subsequent -partN files for this parent note
        parent_stem = p.stem
        part_num = 2
        extra_bodies = []
        while True:
            part_path = notes_dir / f"{parent_stem}-part{part_num}.md"
            if not part_path.exists():
                break
            try:
                part_raw = part_path.read_text(encoding="utf-8", errors="replace")
                _, part_body = parse_front_matter(part_raw)
                extra_bodies.append(part_body)
            except Exception:
                pass
            part_num += 1

        full_body = body + ("\n".join(extra_bodies) if extra_bodies else "")

        title = fm.get("title") or p.stem
        topics = fm.get("topics") if isinstance(fm.get("topics"), list) else []
        files = fm.get("files_touched") if isinstance(fm.get("files_touched"), list) else []
        # basenames only: full paths made every long session match everything
        base = [Path(str(f).replace("\\", "/")).name for f in files]
        # title and topics are the signal; full concatenated body is context (no cap truncation for parts)
        body_cap_for_note = max(BODY_CAP, len(full_body))
        weighted = " ".join(
            [str(title)] * 3 + [" ".join(topics)] * 3 + base + [full_body[:body_cap_for_note]]
        )
        toks = tokenize(weighted)
        if not toks:
            continue
        tf = Counter(toks)
        df.update(tf.keys())
        docs.append(
            {
                "note": p.name,
                "title": str(title),
                "source": fm.get("source", "?"),
                "started": (fm.get("started") or "")[:10],
                "topics": topics[:8],
                "files": base[:8],
                "len": len(toks),
                "tf": tf,
            }
        )
    n = len(docs)
    avg = (sum(d["len"] for d in docs) / n) if n else 0.0
    (vault / ".brain").mkdir(parents=True, exist_ok=True)
    (vault / INDEX_REL).write_text(
        json.dumps(
            {
                "n": n,
                "avglen": avg,
                "df": df,
                "docs": [{**d, "tf": dict(d["tf"])} for d in docs],
            }
        ),
        encoding="utf-8",
    )
    print(f"indexed {n} notes | vocab {len(df):,} | avg {avg:.0f} tokens -> {vault / INDEX_REL}")
    return 0


def search(vault: Path, query: str, top: int = 6) -> tuple[list[dict], int]:
    """Programmatic recall. Returns (hits, total_indexed).

    The MCP server and the GUI both go through here, so there is exactly one ranking
    implementation to reason about.
      FileNotFoundError - no index built yet
      ValueError        - query had no usable terms
    """
    idx_p = vault / INDEX_REL
    if not idx_p.exists():
        raise FileNotFoundError(idx_p)
    idx = json.loads(idx_p.read_text(encoding="utf-8"))
    n, avg, df = idx["n"], idx["avglen"] or 1.0, idx["df"]
    terms = tokenize(query)
    if not terms:
        raise ValueError("Query has no usable terms (all stopwords?).")
    # A hit must cover enough of the query to be a real answer. Without this a
    # nonsense query ("zxqwv flurbington quantum tapioca") still returned a hit
    # on one coincidental term, which reads as confident recall of nothing.
    need = 1 if len(terms) <= 2 else 2
    scored = []
    for d in idx["docs"]:
        tf, dl, s, covered = d["tf"], d["len"] or 1, 0.0, 0
        for t in terms:
            f = tf.get(t, 0)
            if not f:
                continue
            covered += 1
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / avg))
        if s > 0 and covered >= need:
            scored.append((s, d))
    scored.sort(key=lambda x: -x[0])
    # drop long-tail noise well below the best hit
    if scored:
        floor = scored[0][0] * 0.25
        scored = [(s, d) for s, d in scored if s >= floor]
    # collapse duplicate conversations (same title re-ingested under different ids)
    seen, hits = set(), []
    for s, d in scored:
        k = d["title"][:80]
        if k in seen:
            continue
        seen.add(k)
        hits.append((s, d))
        if len(hits) >= top:
            break
    graph = load_graph(vault)
    out = []
    for s, d in hits:
        g = graph.get(d["note"], {})
        out.append(
            {
                "score": round(s, 2),
                "title": d["title"][:120],
                "source": d["source"],
                "date": d["started"],
                "topics": d["topics"],
                "files": d["files"],
                "note": d["note"],
                "community": g.get("community"),
                "related": g.get("related", []),
            }
        )
    return out, n


def stats_text(vault: Path) -> str:
    """Human/agent readable corpus summary. Used by the MCP brain_stats tool."""
    from collections import Counter

    idx_p = vault / INDEX_REL
    if not idx_p.exists():
        return (
            "No index has been built yet, so nothing can be recalled. The user must "
            "run a scan in Agent Brain first. This is NOT evidence that past work "
            "does not exist."
        )
    idx = json.loads(idx_p.read_text(encoding="utf-8"))
    docs = idx.get("docs", [])
    by_src = Counter(d.get("source", "?") for d in docs)
    dates = sorted(d["started"] for d in docs if d.get("started"))
    import time as _t

    age = _t.time() - idx_p.stat().st_mtime
    lines = [
        f"{len(docs)} conversations indexed, {len(idx.get('df', {})):,} distinct terms.",
        "By agent: " + ", ".join(f"{k} {v}" for k, v in by_src.most_common()),
    ]
    if dates:
        lines.append(f"Covers {dates[0][:10]} to {dates[-1][:10]}.")
    lines.append(f"Index built {age / 3600:.1f}h ago.")
    lines.append("")
    lines.append(
        "Caveats worth stating to the user: the CURRENT session is never indexed, "
        "and Claude Code deletes its own transcripts older than 30 days by default "
        "(cleanupPeriodDays), so history before that may never have been captured."
    )
    return "\n".join(lines)


def cmd_query(vault: Path, query: str, top: int, as_json: bool) -> int:
    try:
        out, n = search(vault, query, top)
    except FileNotFoundError:
        print("No index. Run:  recall.py index", file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    if not out:
        print(f"No matching conversations for: {query}")
        print(
            "Absence here does NOT mean it never happened — the current session is never indexed,"
        )
        print("and claude.ai web chat is not ingested at all. Say so plainly rather than guessing.")
        return 0
    print(f"# Brain recall: {query}   ({len(out)} of {n} indexed conversations)\n")
    for h in out:
        print(f"## [{h['score']}] {h['title']}")
        print(f"   {h['source']} · {h['date'] or 'undated'} · read: notes/{h['note']}")
        if h["topics"]:
            print("   topics: " + ", ".join(h["topics"]))
        if h["files"]:
            print("   files: " + ", ".join(h["files"][:6]))
        if h["community"]:
            print(f"   graph community: {h['community']}")
        if h["related"]:
            print("   graph-related: " + "; ".join(str(r)[:50] for r in h["related"]))
        print()
    ing = sum(1 for h in out if h["community"])
    print(
        f"_Graph enrichment available on {ing}/{len(out)} hits — graph.json covers only a small"
        f" slice of notes/; ranking above is note-based (full coverage)._"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    q = sub.add_parser("query")
    q.add_argument("query", nargs="+")
    q.add_argument("--top", type=int, default=6)
    q.add_argument("--json", action="store_true")
    a = ap.parse_args()
    vault = a.vault.resolve()
    if not vault.is_dir():
        print(f"ERROR: vault not found: {vault}", file=sys.stderr)
        return 2
    if a.cmd == "index":
        return cmd_index(vault)
    return cmd_query(vault, " ".join(a.query), a.top, a.json)


if __name__ == "__main__":
    raise SystemExit(main())
