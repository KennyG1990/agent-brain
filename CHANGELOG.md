# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] â€” 2026-07-31

First public release.

### Added

- **Cross-agent ingestion.** Reads session transcripts from Claude Code, Claude Cowork,
  Codex and Gemini/Antigravity, on Windows, macOS and Linux. Unknown folders are
  auto-detected by file shape rather than by name.
- **MCP server** (`recall`, `read_note`, `brain_stats`) so agents can query your whole
  history â€” including other agents' sessions. Local, read-only, no key, no cost.
  `agentbrain.cli mcp` prints ready-to-paste config per agent.
- **BM25 search** over every conversation, with duplicate collapsing and a term-coverage
  floor so a nonsense query returns nothing instead of a confident coincidence.
- **Semantic graph** (optional, paid). Built-in extractor, or hands off to
  [graphify](https://github.com/safishamsi/graphify) if installed.
- **Desktop app** (tkinter, stdlib) and a **full CLI** that does everything the GUI does.
- **Export** to Obsidian, JSON or Markdown. **Delete everything**, with a size preview
  and typed confirmation.
- **Scheduling** for the free scan only â€” schtasks / launchd / systemd / cron.

### Security & privacy

- API keys go to the **OS keystore** (Windows DPAPI, macOS Keychain, Linux Secret
  Service). With no keystore the app **refuses to save** rather than write plaintext.
  Never written to settings, never logged. Enforced by a canary test and a CI secret scan.
- **Consent gate**: no note text leaves the machine until the user has read the
  disclosure and agreed. `chat()` raises rather than warns. Local endpoints
  (Ollama/LM Studio) need no consent because nothing is uploaded.
- **Hard spend cap**, checked *before* each request so it cannot overshoot.
- Secrets are pattern-redacted from notes before storage.
- Self-ingestion guard: the tool's own maintenance runs are never indexed.

### Notes for people migrating from a hand-rolled pipeline

Several defaults exist because of measured failures, not theory:

- Graph extraction **merges**; it never overwrites. A shrink guard refuses any graph
  under 80% of the previous node count. (An earlier pipeline went 1,781 nodes â†’ 201 in
  five weeks while its corpus tripled.)
- Large notes are **split into linked parts**, because LLM extractors silently drop
  oversized chunks.
- The model ranking weights *no hidden reasoning* (+40) and *structured outputs*
  (+25/âˆ’25) above price. Models that think by default can spend the whole output budget
  reasoning and return nothing at all.
- Windows scripts are **ASCII-only**. PowerShell 5.1 decodes BOM-less files as ANSI, and
  a UTF-8 em-dash becomes a stray double quote that breaks the entire script.

### Known limitations

- ChatGPT cannot be ingested â€” the desktop app is a browser shell with no local
  transcript store. Verified, not assumed.
- Windows long paths (>260 chars) untested.
- Windows DPAPI is `ctypes` against documented Win32 calls, exercised on one machine.
- The current session is never in the index.

[Unreleased]: https://github.com/KennyG1990/agent-brain/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/KennyG1990/agent-brain/releases/tag/v1.0.0
