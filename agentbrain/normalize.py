"""
Session transcripts -> one clean Markdown note per conversation.

One note per CONVERSATION, not per event. A conversation is the unit a human
reasons about ("the session where we fixed the crash"); per-event notes produce
an unusable hairball.

Handles three on-disk shapes (see sources.py):
  jsonl_claude   Claude Code / Cowork      {"type":"user|assistant","message":{...}}
  rollout_codex  Codex                     {"type":"response_item","payload":{...}}
  gemini_brain   Gemini / Antigravity      {"source":"USER_EXPLICIT|MODEL", ...}
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .sources import KIND_GEMINI_BRAIN, KIND_JSONL_CLAUDE, KIND_ROLLOUT_CODEX, Source

TOOL_FILE_KEYS = ("file_path", "path", "notebook_path", "filePath", "target_file")
PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'<>|]+|/(?:home|Users)/[^\s\"'<>|]+|file:///[^\s\"'<>|]+"
)

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
    ]
)

# Redact obvious secrets so a pasted key never lands in a note or gets sent to an LLM.
SECRET_RES = [
    re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|bearer)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-.]{16,}"
    ),
]

# Harness scaffolding injected into USER-role turns. Left in, it titles every note
# "<recommended_plugins>..." and destroys keyword extraction.
SCAFFOLD_RES = [
    re.compile(r"<recommended_plugins>.*?</recommended_plugins>", re.S | re.I),
    re.compile(r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.S | re.I),
    re.compile(r"<user_instructions>.*?</user_instructions>", re.S | re.I),
    re.compile(r"<environment_context>.*?</environment_context>", re.S | re.I),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I),
    re.compile(r"^#\s*AGENTS\.md instructions.*$", re.M | re.I),
    re.compile(r"^#\s*Files mentioned by the user:.*?(?=\n#\s|\Z)", re.S | re.M | re.I),
    # Codex/IDE context injection: "# Context from my IDE setup:" + ## Open tabs / ## Active file
    re.compile(r"^#\s*Context from my IDE setup:.*?(?=\n#\s|\Z)", re.S | re.M | re.I),
]
OBJECTIVE_RE = re.compile(r"<objective>(.*?)</objective>", re.S | re.I)
INTERNAL_RE = re.compile(
    r"<codex_internal_context\b.*?>(.*?)</codex_internal_context>", re.S | re.I
)
USER_REQ_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)

# Don't ingest this app's own scheduled maintenance — a corpus that records its own
# upkeep converges on describing itself.
EXCLUDE_RES = [
    re.compile(r"<scheduled-task\s+name=", re.I),
    re.compile(r"\bagent-?brain-(upkeep|maintenance)\b", re.I),
]

GEMINI_PROSE = {"PLANNER_RESPONSE", "ASK_QUESTION", "GENERIC"}
CODEX_ROLES = {"user", "assistant"}


# --------------------------------------------------------------------------- #
def redact(t: str) -> str:
    for rx in SECRET_RES:
        t = rx.sub("[REDACTED-SECRET]", t)
    return t


def slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:maxlen] or "session").strip("-")


def keywords(text: str, k: int = 8) -> list[str]:
    freq: dict[str, int] = {}
    for w in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{2,}", text.lower()):
        if w in STOP or len(w) < 3:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:k]]


def is_self_referential(title: str, first_msg: str = "") -> bool:
    blob = f"{title}\n{first_msg[:2000]}"
    return any(rx.search(blob) for rx in EXCLUDE_RES)


def strip_scaffold(txt: str) -> str:
    if not txt:
        return ""
    m = OBJECTIVE_RE.search(txt)
    if m:
        return m.group(1).strip()
    m = USER_REQ_RE.search(txt)
    if m:
        txt = m.group(1)
    if INTERNAL_RE.search(txt):
        txt = INTERNAL_RE.sub("", txt)
    for rx in SCAFFOLD_RES:
        txt = rx.sub("", txt)
    return txt.strip()


def iter_events(p: Path):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return


def norm_path(fp: str) -> str:
    return fp.replace("\\", "/")


def _paths_in(blob: str) -> list[str]:
    return [
        norm_path(m.replace("file:///", "").replace("%20", " "))
        for m in PATH_RE.findall(blob or "")
    ]


# --------------------------------------------------------------------------- #
def _parse_claude(p: Path) -> dict | None:
    user, asst, tools, files, ts = [], [], [], [], []
    cwd = branch = title = None
    for e in iter_events(p):
        if not isinstance(e, dict):
            continue
        cwd = cwd or e.get("cwd")
        branch = branch or e.get("gitBranch")
        if e.get("timestamp"):
            ts.append(e["timestamp"])
        if e.get("type") == "summary" and e.get("summary"):
            title = e["summary"]
        if e.get("type") not in ("user", "assistant"):
            continue
        msg = e.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else msg
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "tool_use":
                    tools.append(b.get("name", "tool"))
                    inp = b.get("input") or {}
                    if isinstance(inp, dict):
                        for k in TOOL_FILE_KEYS:
                            if inp.get(k):
                                files.append(norm_path(str(inp[k])))
                                break
        txt = "\n".join(x for x in texts if x).strip()
        if not txt:
            continue
        if e["type"] == "user":
            txt = strip_scaffold(txt)
            if txt:
                user.append(redact(txt))
        else:
            asst.append(redact(txt))
    if not user and not asst:
        return None
    return {
        "user": user,
        "asst": asst,
        "tools": tools,
        "files": files,
        "ts": ts,
        "cwd": cwd,
        "branch": branch,
        "title": title,
    }


def _parse_codex(p: Path) -> dict | None:
    user, asst, tools, files, ts = [], [], [], [], []
    sid = cwd = None
    for e in iter_events(p):
        if not isinstance(e, dict):
            continue
        payload = e.get("payload")
        if not isinstance(payload, dict):
            continue
        if e.get("timestamp"):
            ts.append(e["timestamp"])
        if e.get("type") == "session_meta":
            sid = sid or payload.get("session_id") or payload.get("id")
            cwd = cwd or payload.get("cwd")
            continue
        if e.get("type") != "response_item":
            continue
        pt = payload.get("type")
        if pt == "message":
            role = (payload.get("role") or "").lower()
            if role not in CODEX_ROLES:
                continue  # "developer" is the injected system prompt
            c = payload.get("content")
            txt = ""
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                txt = "\n".join(
                    b.get("text", "")
                    for b in c
                    if isinstance(b, dict)
                    and b.get("type") in ("input_text", "output_text", "text")
                )
            txt = txt.strip()
            if role == "user":
                files.extend(_paths_in(txt))  # harvest before stripping
                txt = strip_scaffold(txt)
                if txt:
                    user.append(redact(txt))
            elif txt:
                asst.append(redact(txt))
        elif pt in ("function_call", "custom_tool_call"):
            tools.append(str(payload.get("name") or "tool"))
            files.extend(_paths_in(json.dumps(payload.get("arguments") or "")))
    if not user and not asst:
        return None
    return {
        "user": user,
        "asst": asst,
        "tools": tools,
        "files": files,
        "ts": ts,
        "cwd": cwd,
        "branch": None,
        "title": None,
        "sid": sid,
    }


def _gem_text(o: dict) -> str:
    c = o.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return c.get("text", "") or ""
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def _parse_gemini(p: Path) -> dict | None:
    user, asst, files, ts = [], [], [], []
    for o in iter_events(p):
        if not isinstance(o, dict):
            continue
        if o.get("created_at"):
            ts.append(o["created_at"])
        src, typ = o.get("source"), o.get("type")
        if src == "USER_EXPLICIT":
            txt = strip_scaffold(_gem_text(o))
            txt = re.sub(
                r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", txt, flags=re.S
            ).strip()
            if txt:
                user.append(redact(txt))
        elif src == "MODEL":
            files.extend(_paths_in(json.dumps(o.get("tool_calls") or "")))
            if typ in GEMINI_PROSE:
                txt = _gem_text(o).strip()
                if txt and not txt.startswith("Created At"):
                    asst.append(redact(txt))
    if not user and not asst:
        return None
    return {
        "user": user,
        "asst": asst,
        "tools": [],
        "files": files,
        "ts": ts,
        "cwd": None,
        "branch": None,
        "title": None,
    }


PARSERS = {
    KIND_JSONL_CLAUDE: _parse_claude,
    KIND_ROLLOUT_CODEX: _parse_codex,
    KIND_GEMINI_BRAIN: _parse_gemini,
}


# --------------------------------------------------------------------------- #
def _source_label(src: Source, cwd: str | None) -> str:
    if src.kind == KIND_ROLLOUT_CODEX:
        return "codex"
    if src.kind == KIND_GEMINI_BRAIN:
        return "gemini"
    return (
        "cowork" if cwd and "local-agent-mode-sessions" in cwd.replace("\\", "/") else "claude-code"
    )


def write_note(
    vault: Path, rec: dict, user: list[str], asst: list[str], max_msg: int, max_body: int
) -> list[Path]:
    notes = vault / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    uid = hashlib.md5(rec["id"].encode()).hexdigest()[:8]
    slug = f"{slugify(rec['title'])}-{uid}"

    fm = {
        "id": rec["id"],
        "source": rec["source"],
        "title": rec["title"],
        "project": rec.get("project"),
        "started": rec.get("started"),
        "ended": rec.get("ended"),
        "messages": rec.get("messages"),
    }
    if rec.get("duplicates_merged"):
        fm["duplicates_merged"] = rec["duplicates_merged"]
    head = ["---"]
    for k, v in fm.items():
        head.append(f'{k}: "{"" if v is None else str(v).replace(chr(34), chr(39))}"')
    for key in ("files", "tools", "topics"):
        head.append(f"{'files_touched' if key == 'files' else key}:")
        for item in rec.get(key, [])[:40]:
            head.append(f'  - "{item}"')
    head.append("---\n")
    head.append(f"# {rec['title']}\n")
    if rec.get("topics"):
        head.append("**Topics:** " + " ".join(f"[[topic-{t}]]" for t in rec["topics"]) + "\n")

    def clip(t: str) -> str:
        return (
            t if len(t) <= max_msg else t[:max_msg] + f"\n\n_[clipped {len(t) - max_msg:,} chars]_"
        )

    body = []
    for i in range(max(len(user), len(asst))):
        if i < len(user):
            body.append(f"**User:** {clip(user[i])}\n")
        if i < len(asst):
            body.append(f"**Assistant:** {clip(asst[i])}\n")

    # Split rather than truncate: LLM extractors silently drop oversized inputs.
    chunks, cur, n = [], [], 0
    for blk in body:
        if cur and n + len(blk) > max_body:
            chunks.append(cur)
            cur, n = [], 0
        cur.append(blk)
        n += len(blk)
    chunks.append(cur)
    written = []
    for i, chunk in enumerate(chunks, 1):
        name = slug if i == 1 else f"{slug}-part{i}"
        lines = list(head)
        if len(chunks) > 1:
            lines.append(f"_Part {i} of {len(chunks)}._\n")
        lines.append("## Conversation\n")
        lines += chunk
        p = notes / f"{name}.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        written.append(p)
    return written


def run(
    vault: Path, srcs: list[Source], max_msg: int = 4000, max_body: int = 60000, log=print
) -> dict:
    """Scrape every source into notes/. Incremental via .brain/manifest.json."""
    brain = vault / ".brain"
    brain.mkdir(parents=True, exist_ok=True)
    man_p, rec_p = brain / "manifest.json", brain / "records.json"
    is_full_rescan = not man_p.exists()
    manifest = json.loads(man_p.read_text()) if man_p.exists() else {}
    records = json.loads(rec_p.read_text()) if (rec_p.exists() and not is_full_rescan) else []
    by_id = {r["id"]: r for r in records}
    if is_full_rescan:
        notes_dir = vault / "notes"
        if notes_dir.exists():
            for f in notes_dir.glob("*.md"):
                try:
                    f.unlink()
                except OSError:
                    pass
    new = skipped = 0

    candidates_list = []
    for src in srcs:
        parser = PARSERS.get(src.kind)
        if not parser:
            continue
        if src.kind == KIND_GEMINI_BRAIN:
            files = []
            for conv in sorted(src.path.glob("*")):
                logs = conv / ".system_generated" / "logs"
                for cand in ("transcript_full.jsonl", "transcript.jsonl"):
                    if (logs / cand).exists():
                        files.append(logs / cand)
                        break
        else:
            files = sorted(src.path.rglob(src.glob))
        log(f"  {src.name}: {len(files)} transcript(s)")
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            sig = f"{st.st_mtime_ns}:{st.st_size}"
            if manifest.get(str(p)) == sig:
                skipped += 1
                continue
            parsed = parser(p)
            if not parsed:
                manifest[str(p)] = sig
                continue
            title = parsed["title"] or (
                parsed["user"][0][:70] + "..." if parsed["user"] else p.stem
            )
            first_user = parsed["user"][0].strip() if parsed["user"] else ""
            if is_self_referential(title, first_user):
                manifest[str(p)] = sig
                continue
            sid = parsed.get("sid") or hashlib.md5(str(p.resolve()).encode()).hexdigest()[:12]
            label = _source_label(src, parsed.get("cwd"))
            cwd = parsed.get("cwd") or ""
            fp = (
                hashlib.md5(f"{first_user}||{cwd}".encode()).hexdigest()
                if first_user
                else f"nofp-{sid}"
            )
            rec = {
                "id": f"{label}-{sid}",
                "title": title,
                "source": label,
                "project": parsed.get("cwd"),
                "branch": parsed.get("branch"),
                "started": min(parsed["ts"]) if parsed["ts"] else None,
                "ended": max(parsed["ts"]) if parsed["ts"] else None,
                "messages": len(parsed["user"]) + len(parsed["asst"]),
                "files": sorted({f for f in parsed["files"] if f})[:40],
                "tools": sorted(set(parsed["tools"])),
                "topics": keywords(title + " " + " ".join(parsed["user"][:3])),
            }
            candidates_list.append(
                {
                    "path": str(p),
                    "sig": sig,
                    "fp": fp,
                    "rec": rec,
                    "user": parsed["user"],
                    "asst": parsed["asst"],
                }
            )

    # Deduplicate subagent transcripts sharing the same fingerprint (first_user + cwd)
    groups: dict[str, list[dict]] = {}
    for item in candidates_list:
        groups.setdefault(item["fp"], []).append(item)

    for _fp, group in groups.items():
        # Keep the transcript with the most total messages
        best = max(group, key=lambda x: x["rec"]["messages"])
        if len(group) > 1:
            best["rec"]["duplicates_merged"] = len(group) - 1

        write_note(vault, best["rec"], best["user"], best["asst"], max_msg, max_body)
        by_id[best["rec"]["id"]] = best["rec"]
        new += 1

        for item in group:
            manifest[item["path"]] = item["sig"]

    records = list(by_id.values())
    rec_p.write_text(json.dumps(records, indent=2), encoding="utf-8")
    man_p.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"total": len(records), "new": new, "skipped": skipped}
