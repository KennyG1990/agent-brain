"""
MCP server — lets your AI agents query your own cross-agent history.

This is the point of the whole project. Without it, Agent Brain is a search box a
human uses. With it, Claude Code / Codex / Cursor / Gemini CLI can ask "what did we
decide about X" and get the real answer out of every past session, including sessions
from a DIFFERENT agent.

Protocol: MCP over stdio, JSON-RPC 2.0, newline-delimited. Implemented directly on the
stdlib so the app keeps its zero-dependency promise.

Run:      python -m agentbrain.mcp_server [--vault PATH]
Register: see `--install-help` for the exact config block per agent.

Everything here is LOCAL and read-only. No network, no API key, no cost. The graph
step is the only thing that ever uploads, and this server does not touch it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config, recall

PROTOCOL = "2024-11-05"
NAME, VERSION = "agent-brain", "1.0.0"

TOOLS = [
    {
        "name": "recall",
        "description": (
            "Search the user's ENTIRE history of past AI coding sessions - across Claude "
            "Code, Claude Cowork, Codex and Gemini - for prior work on a topic. Use this "
            "BEFORE reconstructing, rebuilding, or assuming anything about past work, and "
            "whenever the user says 'we did this before', 'pick up where we left off', "
            "'what did we decide', or names a project with history. Returns matching "
            "conversations with their date, which agent, files touched, and the note file "
            "to read for the verbatim exchange."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Plain-language question. Phrase by MEANING, not "
                    "filename, e.g. 'how did we set up the printer MCP'.",
                },
                "top": {
                    "type": "integer",
                    "default": 6,
                    "description": "How many conversations to return (1-20).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": (
            "Read the full text of one conversation note returned by recall. "
            "Use when the recall summary is not enough and you need what was "
            "actually said."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "Note filename from a recall result."}
            },
            "required": ["note"],
        },
    },
    {
        "name": "brain_stats",
        "description": (
            "How much history is actually indexed, broken down by agent, and "
            "how fresh it is. Call this when recall returns nothing, to tell "
            "the difference between 'never happened' and 'not indexed yet'."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    def __init__(self, vault: Path) -> None:
        self.vault = vault

    # ---- tools -----------------------------------------------------------
    def t_recall(self, args: dict) -> str:
        q = (args.get("query") or "").strip()
        if not q:
            return "No query given."
        top = max(1, min(int(args.get("top") or 6), 20))
        try:
            hits, total = recall.search(self.vault, q, top)
        except FileNotFoundError:
            return (
                "The brain has no index yet. The user needs to run a scan in Agent "
                "Brain first. This is NOT evidence that past work does not exist."
            )
        except ValueError as e:
            return f"{e} Try a question with real nouns in it."
        if total == 0:
            return (
                "The brain has no indexed conversations yet. The user needs to run a "
                "scan in Agent Brain first. Do NOT treat this as 'it never happened'."
            )
        if not hits:
            return (
                f"No matches for {q!r} across {total} indexed conversations.\n\n"
                "IMPORTANT: absence here does not prove it never happened. The current "
                "session is never indexed, and Claude Code deletes transcripts older "
                "than 30 days by default. Say so rather than asserting it is new."
            )
        out = [f"{len(hits)} of {total} indexed conversations matched {q!r}:\n"]
        for h in hits:
            out.append(f"## [{h['score']}] {h['title']}")
            out.append(f"   {h['source']} - {h['date'] or 'undated'} - read_note: {h['note']}")
            if h["topics"]:
                out.append("   topics: " + ", ".join(h["topics"]))
            if h["files"]:
                out.append("   files: " + ", ".join(h["files"][:6]))
            out.append("")
        return "\n".join(out)

    def t_read_note(self, args: dict) -> str:
        name = Path((args.get("note") or "").strip()).name  # never escape notes/
        if not name:
            return "No note given."
        p = self.vault / "notes" / name
        if not p.is_file():
            return f"No such note: {name}. Use recall first to get valid names."
        text = p.read_text(encoding="utf-8", errors="replace")
        cap = 60_000
        return text if len(text) <= cap else text[:cap] + "\n\n[truncated]"

    def t_stats(self, args: dict) -> str:
        return recall.stats_text(self.vault)

    # ---- protocol --------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            return self._ok(
                mid,
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": NAME, "version": VERSION},
                },
            )
        if method in ("notifications/initialized", "initialized"):
            return None  # notification: no reply
        if method == "tools/list":
            return self._ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), (params.get("arguments") or {})
            fn = {
                "recall": self.t_recall,
                "read_note": self.t_read_note,
                "brain_stats": self.t_stats,
            }.get(name)
            if not fn:
                return self._err(mid, -32601, f"Unknown tool: {name}")
            try:
                text = fn(args)
            except Exception as e:
                return self._ok(
                    mid,
                    {"content": [{"type": "text", "text": f"Tool failed: {e}"}], "isError": True},
                )
            return self._ok(mid, {"content": [{"type": "text", "text": text}]})
        if method == "ping":
            return self._ok(mid, {})
        if mid is None:
            return None  # unknown notification: ignore
        return self._err(mid, -32601, f"Unknown method: {method}")

    @staticmethod
    def _ok(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(vault: Path, stdin=None, stdout=None) -> int:
    srv = Server(vault)
    rin, rout = stdin or sys.stdin, stdout or sys.stdout
    for line in rin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            rout.write(json.dumps(Server._err(None, -32700, "Parse error")) + "\n")
            rout.flush()
            continue
        reply = srv.handle(msg)
        if reply is not None:
            rout.write(json.dumps(reply) + "\n")
            rout.flush()
    return 0


INSTALL_HELP = """\
REGISTERING AGENT BRAIN WITH YOUR AGENTS

Everything below is local and read-only. No API key, no network, no cost.

Claude Code    - .mcp.json in your project root (or `claude mcp add -s user ...`)
Codex          - ~/.codex/config.toml
Cursor         - .cursor/mcp.json
Gemini CLI     - ~/.gemini/settings.json

JSON form (Claude Code / Cursor / most others):

  {
    "mcpServers": {
      "agent-brain": {
        "command": "%PY%",
        "args": ["-m", "agentbrain.mcp_server", "--vault", "%VAULT%"],
        "env": {"PYTHONPATH": "%ROOT%"}
      }
    }
  }

TOML form (Codex):

  [mcp_servers.agent-brain]
  command = "%PY%"
  args = ["-m", "agentbrain.mcp_server", "--vault", "%VAULT%"]
  env = { PYTHONPATH = "%ROOT%" }

Windows note: in JSON, every backslash must be doubled ("C:\\\\Users\\\\..."). A path
with FOUR backslashes is wrong and is a real bug we hit - it produces literal doubled
separators. Two per separator, no more.

Then tell your agent to consult it before rebuilding anything. Claude Code users can
add to CLAUDE.md:

  Before any non-trivial task, call the agent-brain `recall` tool to check whether
  this was already solved in a past session (any agent, not just this one).
"""


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Agent Brain MCP server")
    ap.add_argument("--vault", type=Path, default=None)
    ap.add_argument(
        "--install-help", action="store_true", help="print ready-to-paste config for each agent"
    )
    ap.add_argument(
        "--selftest", action="store_true", help="exercise the protocol locally and exit"
    )
    a = ap.parse_args(argv)
    vault = a.vault or config.vault_path()

    if a.install_help:
        root = Path(__file__).resolve().parents[1]
        print(
            INSTALL_HELP.replace("%PY%", sys.executable.replace("\\", "\\\\"))
            .replace("%VAULT%", str(vault).replace("\\", "\\\\"))
            .replace("%ROOT%", str(root).replace("\\", "\\\\"))
        )
        return 0
    if a.selftest:
        return selftest(vault)
    return serve(vault)


def selftest(vault: Path) -> int:
    """Drive the real protocol through the real handler. No mocks."""
    import io

    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "brain_stats", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "mcp server", "top": 2}},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "read_note", "arguments": {"note": "../../etc/passwd"}},
        },
    ]
    out = io.StringIO()
    serve(vault, stdin=io.StringIO("\n".join(json.dumps(r) for r in reqs)), stdout=out)
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    print(f"vault: {vault}")
    print(f"replies: {len(lines)} (expected 6 - the notification must NOT get one)")
    for r in lines:
        if "error" in r:
            print(f"  id={r.get('id')} ERROR {r['error']['code']}: {r['error']['message']}")
        else:
            res = r["result"]
            if "tools" in res:
                print(f"  id={r['id']} tools: {[t['name'] for t in res['tools']]}")
            elif "serverInfo" in res:
                print(f"  id={r['id']} init: {res['serverInfo']} proto={res['protocolVersion']}")
            else:
                txt = (res.get("content") or [{}])[0].get("text", "")
                print(f"  id={r['id']} -> {txt.splitlines()[0][:88] if txt else '(empty)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
