# Contributing

Thanks for looking. A few things are load-bearing here and worth knowing before you start.

## The rules that are not negotiable

1. **Zero runtime dependencies.** `agentbrain/` imports the standard library only.
   `TestNoThirdPartyImports` AST-walks every module and fails the build otherwise.
   Optional tooling belongs in `[project.optional-dependencies]`.
2. **No secret ever reaches disk in plaintext.** Keys go through `secrets.py` and the OS
   keystore. If there is no keystore, the app refuses to save. `TestSecretsNeverLeak`
   asserts this with a canary string.
3. **Nothing uploads without consent.** `providers.chat()` calls `consent.gate()` and
   raises. Do not add a code path that bypasses it.
4. **Windows scripts are ASCII-only.** PowerShell 5.1 reads BOM-less files as ANSI; a
   UTF-8 em-dash becomes a stray double quote and breaks the whole script. This cost a
   silent failed run once already.

## Setup

```bash
git clone https://github.com/YOURNAME/agent-brain && cd agent-brain
pip install pre-commit && pre-commit install     # optional, but please
```

## Before you open a PR

```bash
python tools/selfcheck.py                        # stdlib linter, no install needed
python -m unittest discover -s tests -v
python -m agentbrain.mcp_server --selftest
pre-commit run --all-files                       # the real gate: ruff, mypy, gitleaks
```

`tools/selfcheck.py` exists so contributors with no dev tooling still get the important
feedback. It is the floor; `pre-commit` is the ceiling. It honours `# noqa` the same way
ruff does.

## Tests

Prefer asserting the **invariant** over the output. The spend cap looked correct in
printed logs while overshooting by one call every time; the test that caught it asserted
`meter.usd <= meter.cap`, not a specific number.

Anything touching keys, consent or spending needs a test. Those are the three ways this
project could actually harm someone.

## Things that will be turned down

- Runtime dependencies in `agentbrain/`
- Telemetry, analytics, or any phone-home
- Storing a key anywhere except the OS keystore
- Making the graph step default-on, or automatic
