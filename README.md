# Agent Brain

**Your AI coding assistants forget everything. This remembers it, and gives it back to them.**

Claude Code, Codex, Cursor and Gemini each keep their own session history, siloed, and
none of them can read the others'. Agent Brain reads all of them, turns every past
conversation into a searchable note, and exposes it back to your agents over MCP — so
Gemini can recall what you and Claude worked out last month, and nobody rebuilds work
that already exists.

```
python -m agentbrain.cli search "how did we set up the printer MCP"

[22.96] I just got a bambu p1s and I want to use an AI 3d model generator...
   gemini · 2026-06-18 · notes/i-just-got-a-bambu-p1s-16332cbb.md
[21.98] research blender MCP and bambu MCP...
   cowork · 2026-06-19 · notes/research-blender-mcp-and-bambu-mcp-11a10396.md
```

---

## Read this first: Claude Code is deleting your history right now

Claude Code purges session transcripts older than **30 days**, every time it starts, by
default. No warning, no recovery. ([issue #59248](https://github.com/anthropics/claude-code/issues/59248))

Before anything else, open `~/.claude/settings.json` and add:

```json
{ "cleanupPeriodDays": 3650 }
```

Then run a scan. Nothing recovers what's already gone, but from that point on your
history is yours.

---

## What you need

**Python 3.10 or newer. That's the whole list.** No pip install, no virtualenv, no
build step, no account, no internet. Every dependency is the Python standard library,
and CI fails the build if that ever stops being true.

On Windows, when installing Python, **tick both boxes**:

- **Add python.exe to PATH**
- **tcl/tk and IDLE** - the window will not open without it

Verify with `python -m tkinter` (a small window should appear) or `python app.py --check`.

### Optional

| | For | Without it |
|---|---|---|
| An API key | Building the semantic graph | Search works fully, free and offline |
| [`graphify`](https://github.com/safishamsi/graphify) (`pip install graphifyy`) | Better graph extraction | The built-in extractor is used |
| [Ollama](https://ollama.com) / LM Studio | A graph with **zero** upload | Use a hosted API, or skip the graph |

---

## Install

```bash
git clone https://github.com/KennyG1990/agent-brain
cd agent-brain
python app.py            # desktop app
```

Windows users can double-click **`Agent Brain.bat`**.

No GUI? Everything works from the command line:

```bash
python -m agentbrain.cli check      # what this machine has, what history exists
python -m agentbrain.cli scan       # import your conversations (free, local)
python -m agentbrain.cli search "your question"
python -m agentbrain.cli status
```

---

## Give it to your agents

This is the part that matters. Print ready-to-paste config for your setup:

```bash
python -m agentbrain.cli mcp
```

Then add one line to your `CLAUDE.md` / `GEMINI.md` / `AGENTS.md`:

> Before any non-trivial task, call the `agent-brain` `recall` tool to check whether
> this was already solved in a past session — including sessions from other agents.

Three tools, all local and read-only, all free: **`recall`**, **`read_note`**,
**`brain_stats`**. You don't run anything — your agent spawns the server over stdio
and shuts it down afterwards.

---

## Privacy, stated plainly

Your transcripts contain client names, file paths, unreleased ideas, and whatever you
pasted at 2am. So:

- **Scanning and searching never touch the network.** Ever. Not once.
- **Only the optional graph step uploads anything**, and it is blocked until you have
  read the disclosure and said yes. `chat()` raises rather than warns.
- **Choose "local only"** and point it at Ollama and nothing leaves your machine at all.
- Obvious secrets (API keys, tokens) are pattern-matched out before storage. **Pattern
  matching is not a guarantee** — it will not catch a password you typed in prose.
- **Delete everything** from the Data tab or `python -m agentbrain.cli delete`. It shows
  you exactly what and how much, first.

### Your API key

Stored in the OS keystore — **Windows DPAPI**, **macOS Keychain**, **Linux Secret
Service**. If no keystore exists, the app **refuses to save it** and tells you to use an
environment variable instead, because writing a key in plaintext is worse than being
inconvenient.

It is never written to `settings.json`, never logged, and never committed. A test
asserts this with a canary string, and CI fails on any key-shaped text in the repo.

*What this does not protect against:* malware already running as you can ask the same
OS to decrypt. No local app can prevent that.

---

## Spending money

Only the graph step costs anything, and only if you choose a hosted API.

```bash
python -m agentbrain.cli graph --dry-run              # scope + cost, sends nothing
python -m agentbrain.cli graph --max-spend 5.00       # hard stop at $5
```

The cap is checked **before** each request, not after, so it cannot overshoot. Work is
saved as it goes — hitting the cap costs you nothing but the batch in flight, and
re-running resumes where it stopped.

The model picker ranks by **suitability, not price**, for reasons learned the hard way:

| Signal | Weight | Why |
|---|---|---|
| No hidden reasoning | **+40** | Models that think by default spend the output budget reasoning and return *nothing* |
| Structured outputs | +25 / **−25** | Others wrap JSON in ` ```json ` fences that fail to parse |
| Context / max output | +18 | Bigger chunks, fewer calls |
| Price | small | Never enough to outrank correctness |

---

## What it reads

| Agent | Location |
|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Claude Cowork | `%APPDATA%\Claude\local-agent-mode-sessions` (and macOS/Linux equivalents) |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` |
| Gemini / Antigravity | `~/.gemini/**/brain/*/.system_generated/logs/` |

Add anything else in Settings — the format is auto-detected.

**ChatGPT is not supported and cannot be.** The desktop app is a browser shell; its
conversations live on OpenAI's servers with no local transcript. Verified, not assumed.

---

## Install with pipx

```bash
pipx install agent-brain
agent-brain check
agent-brain search "your question"
```

Still zero runtime dependencies — pipx just puts the commands on your PATH.

## Development

No dev tooling required to get useful feedback:

```bash
python tools/selfcheck.py                    # stdlib linter - unused imports, dead
                                             # locals, mutable defaults, bare excepts
python -m unittest discover -s tests -v      # 28 tests, no pytest needed
python -m agentbrain.mcp_server --selftest   # MCP protocol conformance
python -m agentbrain.secrets                 # key storage audit
python tools/check_ascii.py                  # Windows script encoding guard
```

With tooling:

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files                   # ruff, ruff-format, gitleaks, mypy
```

**gitleaks runs pre-commit, not just in CI** — a post-push secret scan is too late for a
project whose pitch is careful key handling.

CI runs the suite on Windows, macOS and Linux across Python 3.10 and 3.13, plus ruff,
mypy (advisory), and a secret scan. See [CONTRIBUTING.md](CONTRIBUTING.md) for the four
non-negotiable invariants.

## Known limitations

Stated rather than discovered:

- **Windows long paths (>260 chars)** may break on deep project trees. Enable
  `LongPathsEnabled`. Untested.
- **Windows DPAPI** is `ctypes` against documented Win32 calls; exercised on the
  author's machine only.
- The graph's built-in extractor is simpler than graphify's. Install graphify if you
  want the better one.
- The index is a snapshot. The **current** session is never in it.

## License

MIT — see [LICENSE](LICENSE).
