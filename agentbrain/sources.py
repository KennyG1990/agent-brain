"""
Where AI coding agents keep their session logs, on every OS.

Nothing here is guessed at runtime — each entry is a documented layout with a
`kind` that tells normalize.py which parser to use. Agents you don't have are
simply reported as missing; that is not an error.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# kind -> which parser in normalize.py handles it
KIND_JSONL_CLAUDE = "jsonl_claude"   # Claude Code / Cowork: one JSONL per session
KIND_ROLLOUT_CODEX = "rollout_codex"  # Codex: rollout-<iso>-<uuid>.jsonl
KIND_GEMINI_BRAIN = "gemini_brain"   # Gemini/Antigravity: <id>/.system_generated/logs/


@dataclass
class Source:
    name: str
    kind: str
    path: Path
    glob: str = "*.jsonl"
    found: bool = False
    files: int = 0
    note: str = ""


def _home() -> Path:
    return Path.home()


def _appdata() -> Path | None:
    """Windows %APPDATA%; None elsewhere."""
    v = os.environ.get("APPDATA")
    return Path(v) if v else None


def candidates() -> list[Source]:
    """Every place we know to look, whether or not it exists on this machine."""
    h = _home()
    out: list[Source] = [
        Source("Claude Code", KIND_JSONL_CLAUDE, h / ".claude" / "projects"),
        Source("Codex", KIND_ROLLOUT_CODEX, h / ".codex" / "sessions",
               glob="rollout-*.jsonl"),
        Source("Gemini / Antigravity", KIND_GEMINI_BRAIN,
               h / ".gemini" / "antigravity-ide" / "brain"),
        Source("Gemini / Antigravity (alt)", KIND_GEMINI_BRAIN,
               h / ".gemini" / "antigravity" / "brain"),
    ]

    # Claude Desktop "Cowork" local sessions — different parent per OS.
    if sys.platform == "win32":
        ad = _appdata()
        if ad:
            out.append(Source("Claude Cowork", KIND_JSONL_CLAUDE,
                              ad / "Claude" / "local-agent-mode-sessions"))
    elif sys.platform == "darwin":
        out.append(Source("Claude Cowork", KIND_JSONL_CLAUDE,
                          h / "Library" / "Application Support" / "Claude"
                          / "local-agent-mode-sessions"))
    else:
        out.append(Source("Claude Cowork", KIND_JSONL_CLAUDE,
                          h / ".config" / "Claude" / "local-agent-mode-sessions"))
    return out


def _count(src: Source, cap: int = 4000) -> int:
    """Cheap existence/size probe. Capped so a huge tree can't stall the GUI."""
    n = 0
    try:
        if src.kind == KIND_GEMINI_BRAIN:
            for conv in src.path.iterdir():
                if not conv.is_dir():
                    continue
                logs = conv / ".system_generated" / "logs"
                if (logs / "transcript_full.jsonl").exists() or (logs / "transcript.jsonl").exists():
                    n += 1
                if n >= cap:
                    break
        else:
            for _ in src.path.rglob(src.glob):
                n += 1
                if n >= cap:
                    break
    except (OSError, PermissionError) as e:
        src.note = f"unreadable: {e.__class__.__name__}"
    return n


def discover(extra: list[str] | None = None) -> list[Source]:
    """Probe the machine. Returns every candidate with found/files filled in.

    `extra` lets a user point at a folder we don't know about; it is parsed with
    the Claude-Code JSONL reader, which is the most common shape.
    """
    found: list[Source] = []
    for src in candidates():
        if src.path.is_dir():
            src.found = True
            src.files = _count(src)
            if src.files == 0:
                src.note = src.note or "folder exists but holds no transcripts yet"
        found.append(src)

    for raw in extra or []:
        p = Path(raw).expanduser()
        kind, glob = sniff_kind(p)
        s = Source(f"Custom: {p.name}", kind, p, glob=glob)
        if p.is_dir():
            s.found, s.files = True, _count(s)
            s.note = f"detected as {kind}"
        else:
            s.note = "folder not found"
        found.append(s)
    return found


def sniff_kind(p: Path) -> tuple[str, str]:
    """Work out what kind of transcripts a user-supplied folder holds.

    People paste in whatever folder they think holds their history; guessing wrong
    silently produces empty notes, so detect by shape rather than by folder name.
    """
    if not p.is_dir():
        return KIND_JSONL_CLAUDE, "*.jsonl"
    try:
        # Gemini/Antigravity: <id>/.system_generated/logs/transcript*.jsonl
        for conv in list(p.iterdir())[:50]:
            if conv.is_dir() and (conv / ".system_generated" / "logs").is_dir():
                return KIND_GEMINI_BRAIN, "*.jsonl"
        # Codex: rollout-<iso>-<uuid>.jsonl anywhere below
        for _ in p.rglob("rollout-*.jsonl"):
            return KIND_ROLLOUT_CODEX, "rollout-*.jsonl"
        # otherwise sniff the first JSONL's first line
        for f in p.rglob("*.jsonl"):
            with open(f, encoding="utf-8", errors="replace") as fh:
                head = fh.readline()
            if '"payload"' in head and '"session_meta"' in head:
                return KIND_ROLLOUT_CODEX, "*.jsonl"
            break
    except (OSError, PermissionError):
        pass
    return KIND_JSONL_CLAUDE, "*.jsonl"


def active(extra: list[str] | None = None) -> list[Source]:
    """Only the sources that actually have transcripts to read."""
    return [s for s in discover(extra) if s.found and s.files > 0]


def summary(extra: list[str] | None = None) -> str:
    srcs = discover(extra)
    live = [s for s in srcs if s.found and s.files]
    if not live:
        return ("No AI agent session logs found on this computer. "
                "Install/use Claude Code, Codex, or Gemini CLI first, or add a folder manually "
                "in Settings.")
    return " · ".join(f"{s.name} {s.files}" for s in live)


if __name__ == "__main__":  # quick probe: python -m agentbrain.sources
    for s in discover():
        mark = "OK " if (s.found and s.files) else ("-- " if s.found else "   ")
        print(f"{mark} {s.name:<28} {s.files:>5} files   {s.path}"
              + (f"   [{s.note}]" if s.note else ""))
