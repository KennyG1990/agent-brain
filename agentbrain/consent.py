"""
Informed consent before anything leaves this computer.

This app reads every conversation you have had with your AI coding tools. Those
transcripts routinely contain client names, file paths, unreleased ideas, and things
you typed without thinking because you believed you were talking to a tool on your
own machine.

Scanning and searching are entirely local and always have been. But building the
semantic GRAPH sends note text to whichever LLM API you configured. That is a real
disclosure to a third party, and the person running this deserves to be told plainly,
once, before it happens — not in a README they did not read.

So: no API call in this app happens until has_consent() is True.

The escape hatch is genuine, not decorative: point base_url at Ollama or LM Studio and
nothing leaves the machine at all. record(local_only=True) records that choice.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

VERSION = 1  # bump to re-ask everyone when the disclosure materially changes


SUMMARY = """\
WHAT THIS APP DOES WITH YOUR DATA

Reads (always local, never uploaded):
  Your saved session transcripts from Claude Code, Claude Cowork, Codex and
  Gemini/Antigravity. It turns each conversation into a Markdown note and builds a
  keyword search index. This never touches the network.

Uploads (ONLY if you build the semantic graph):
  The text of your notes is sent, in chunks, to the LLM API you configure. That
  provider's terms apply to it. Depending on your provider and plan, it may be
  retained, logged, or used for training. This app cannot control that.

What your notes contain:
  Everything you and the assistant said. File paths. Project and client names.
  Anything you pasted. Obvious secrets (API keys, tokens) are pattern-matched and
  replaced with [REDACTED-SECRET] before storage, but pattern matching is not a
  guarantee and it will not catch a password you typed in prose.

If that is not acceptable, and it may well not be:
  Choose "local only". Point the app at Ollama or LM Studio in Settings and the graph
  is built by a model running on your own hardware. Nothing leaves the machine.
  Search and scanning work fully in this mode.

You can change your mind later in Settings, and delete everything at any time.
"""


def _path() -> Path:
    return config.config_dir() / "consent.json"


def state() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def has_consent() -> bool:
    """True only if this exact disclosure version was accepted for uploading."""
    s = state()
    return bool(s.get("accepted")) and s.get("version") == VERSION and not s.get("local_only")


def is_local_only() -> bool:
    return bool(state().get("local_only"))


def answered() -> bool:
    """Has the user been asked at all? False means show the gate."""
    s = state()
    return bool(s) and s.get("version") == VERSION


def record(accepted: bool, local_only: bool = False) -> Path:
    p = _path()
    p.write_text(
        json.dumps(
            {
                "version": VERSION,
                "accepted": bool(accepted),
                "local_only": bool(local_only),
                "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def revoke() -> None:
    p = _path()
    if p.exists():
        p.unlink()


def gate(base_url: str = "") -> tuple[bool, str]:
    """Call before ANY upload. (allowed, reason_if_not).

    A local endpoint is always allowed: nothing leaves the machine, so there is
    nothing to consent to.
    """
    if _is_local(base_url):
        return True, ""
    if is_local_only():
        return False, (
            "You chose local-only. Point Base URL at Ollama or LM Studio, "
            "or change the choice in Settings."
        )
    if not has_consent():
        return False, "Consent for uploading note text has not been given yet."
    return True, ""


def _is_local(url: str) -> bool:
    u = (url or "").lower()
    return any(
        h in u
        for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1", ".local:", "host.docker.internal")
    )


def sample_upload(vault: Path, chars: int = 1500) -> str:
    """Exactly the kind of text that would be uploaded. Shown in the gate on request."""
    notes = sorted((vault / "notes").glob("*.md")) if (vault / "notes").is_dir() else []
    if not notes:
        return "(no notes yet — scan first and this will show real text from your own data)"
    raw = notes[len(notes) // 2].read_text(encoding="utf-8", errors="replace")
    body = raw.split("## Conversation", 1)[-1].strip()
    return f"From {notes[len(notes) // 2].name}:\n\n{body[:chars]}" + (
        "\n\n…(truncated for display)" if len(body) > chars else ""
    )
