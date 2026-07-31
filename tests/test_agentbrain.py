"""
Agent Brain test suite. stdlib unittest only — no pytest, no pip install.

    python -m unittest discover -s tests -v
    python tests/test_agentbrain.py

The security tests are the ones that matter most. They exist so that a future edit
cannot quietly reintroduce a plaintext API key or an upload without consent, which
are the two ways this project could actually hurt someone.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Sandboxed(unittest.TestCase):
    """Each test gets its own config dir so nothing touches the developer's real one."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ab-test-"))
        self._env = dict(os.environ)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp)
        os.environ["APPDATA"] = str(self.tmp)
        for v in ("AGENTBRAIN_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                  "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            os.environ.pop(v, None)

    def tearDown(self) -> None:
        os.environ.clear(); os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
class TestSecretsNeverLeak(Sandboxed):
    """A plaintext key must never reach disk. This is the load-bearing test."""

    def test_key_never_written_to_settings(self):
        from agentbrain import config
        config.save({"provider": "openrouter", "api_key": "sk-or-v1-LEAKCANARY",
                     "token": "t", "secret": "s", "model": "m"})
        raw = config.config_path().read_text(encoding="utf-8")
        self.assertNotIn("LEAKCANARY", raw)
        self.assertNotIn("api_key", raw)
        self.assertNotIn("secret", raw)
        self.assertIn("openrouter", raw)          # non-secret settings DO persist

    def test_legacy_plaintext_key_is_stripped_and_flagged(self):
        from agentbrain import config, secrets
        config.config_path().write_text(
            json.dumps({"provider": "openai", "api_key": "sk-LEGACY"}), encoding="utf-8")
        self.assertNotIn("api_key", config.load())
        self.assertTrue(any("PLAINTEXT KEY" in p for p in secrets.audit()))

    def test_refuses_to_persist_without_a_keystore(self):
        from agentbrain import secrets
        if secrets.can_store():
            self.skipTest("this machine has a keystore; covered by test_roundtrip")
        ok, msg = secrets.save("sk-or-v1-SHOULDNOTPERSIST")
        self.assertFalse(ok)
        self.assertIn("NOT saved", msg)
        found = [p for p in self.tmp.rglob("*")
                 if p.is_file() and "SHOULDNOTPERSIST" in
                 p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(found, [], f"key leaked to {found}")

    def test_roundtrip_when_a_keystore_exists(self):
        from agentbrain import secrets
        if not secrets.can_store():
            self.skipTest("no OS keystore in this environment")
        ok, _ = secrets.save("sk-or-v1-roundtrip-canary")
        self.assertTrue(ok)
        self.assertEqual(secrets.load()[0], "sk-or-v1-roundtrip-canary")
        secrets.delete()
        self.assertEqual(secrets.load()[0], "")

    def test_env_var_wins_and_is_never_written(self):
        from agentbrain import config, secrets
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-FROMENV"
        key, src = secrets.load()
        self.assertEqual(key, "sk-or-v1-FROMENV")
        self.assertIn("environment", src)
        config.save(config.load())
        self.assertNotIn("FROMENV", config.config_path().read_text(encoding="utf-8"))

    def test_mask_never_reveals_short_keys(self):
        from agentbrain import secrets
        self.assertNotIn("secret", secrets.mask("secret-ish"))
        self.assertEqual(secrets.mask(""), "(none)")


class TestConsentGate(Sandboxed):
    """No upload may happen before the user has been told what is uploaded."""

    def test_blocked_on_a_fresh_install(self):
        from agentbrain import consent
        self.assertFalse(consent.answered())
        self.assertFalse(consent.gate("https://api.openai.com/v1")[0])

    def test_local_endpoints_need_no_consent(self):
        from agentbrain import consent
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8787/v1"):
            self.assertTrue(consent.gate(url)[0], url)

    def test_chat_refuses_before_consent(self):
        from agentbrain import providers
        with self.assertRaises(providers.ProviderError) as cm:
            providers.chat({"provider": "openai", "api_key": "x", "model": "m",
                            "base_url": "https://api.openai.com/v1"}, "s", "u")
        self.assertIn("Refusing to send", str(cm.exception))

    def test_local_only_choice_keeps_remote_blocked(self):
        from agentbrain import consent
        consent.record(accepted=False, local_only=True)
        self.assertFalse(consent.gate("https://api.openai.com/v1")[0])
        self.assertTrue(consent.gate("http://localhost:11434/v1")[0])

    def test_version_bump_reasks(self):
        from agentbrain import consent
        consent.record(accepted=True)
        self.assertTrue(consent.has_consent())
        old, consent.VERSION = consent.VERSION, consent.VERSION + 1
        try:
            self.assertFalse(consent.has_consent())
        finally:
            consent.VERSION = old


class TestSpendCap(unittest.TestCase):
    def test_cap_blocks_before_breaching_not_after(self):
        from agentbrain.providers import Meter, SpendCap
        m = Meter(0.40, 1.60, cap_usd=0.50)
        chunk = "x" * 800_000
        for _ in range(20):
            try:
                m.check(*m.estimate(chunk))
            except SpendCap:
                break
            m.add(200_000, 20_000)
        else:
            self.fail("cap never fired")
        self.assertLessEqual(m.usd, m.cap, "spend exceeded the cap")

    def test_no_cap_never_blocks(self):
        from agentbrain.providers import Meter
        m = Meter(1.0, 1.0, cap_usd=0)
        m.add(10**9, 10**9)
        m.check(10**9, 10**9)


class TestNormalize(unittest.TestCase):
    def test_secrets_are_redacted_from_notes(self):
        from agentbrain.normalize import redact
        for probe in ("sk-or-v1-" + "a" * 40, "sk-ant-" + "b" * 30,
                      "AIza" + "c" * 35, "ghp_" + "d" * 30):
            self.assertIn("[REDACTED-SECRET]", redact(f"my key is {probe} ok"))

    def test_harness_scaffolding_is_stripped(self):
        from agentbrain.normalize import strip_scaffold
        self.assertEqual(strip_scaffold(
            "<recommended_plugins>\nlots\n</recommended_plugins>\nreal question"),
            "real question")
        self.assertEqual(strip_scaffold(
            "# AGENTS.md instructions\n<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>\nask"), "ask")
        self.assertEqual(strip_scaffold(
            "<codex_internal_context source='goal'><objective>do the thing</objective>"
            "</codex_internal_context>"), "do the thing")

    def test_self_referential_sessions_are_excluded(self):
        from agentbrain.normalize import is_self_referential
        self.assertTrue(is_self_referential('<scheduled-task name="agent-brain-upkeep">'))
        self.assertFalse(is_self_referential("fix the login bug"))


class TestModelRanking(unittest.TestCase):
    """Correctness must outrank price. Regression guard on the scoring weights."""

    @staticmethod
    def _m(mid, ctx, mo, thinks, struct, pin, pout):
        return {"id": mid, "name": mid, "description": "", "context_length": ctx,
                "top_provider": {"max_completion_tokens": mo},
                "pricing": {"prompt": str(pin / 1e6), "completion": str(pout / 1e6),
                            **({"internal_reasoning": "0.000001"} if thinks else {})},
                "supported_parameters": (["reasoning"] if thinks else [])
                + (["structured_outputs"] if struct else [])}

    def test_cheap_but_unparseable_ranks_below_good_and_pricey(self):
        from agentbrain import models as mp
        rows = mp.rank([
            self._m("cheap/no-struct", 200_000, 8_192, False, False, 0.01, 0.02),
            self._m("good/pricey", 200_000, 32_768, False, True, 2.00, 8.00),
        ], 500)
        self.assertEqual(rows[0]["id"], "good/pricey")

    def test_tiny_context_is_excluded_entirely(self):
        from agentbrain import models as mp
        self.assertIsNone(mp.analyse(self._m("x/tiny", 8_000, 4_096, False, True, 0.01, 0.01), 10))

    def test_budget_scales_with_output_ceiling(self):
        from agentbrain import models as mp
        big = mp.analyse(self._m("a/big-out", 500_000, 65_535, False, True, 1, 1), 500)
        small = mp.analyse(self._m("a/small-out", 500_000, 8_192, False, True, 1, 1), 500)
        self.assertGreater(mp.suggest_token_budget(big)["budget"],
                           mp.suggest_token_budget(small)["budget"])


class TestMCPServer(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = Path(tempfile.mkdtemp(prefix="ab-mcp-"))
        (self.vault / "notes").mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.vault, ignore_errors=True)

    def _run(self, msgs):
        import io
        from agentbrain import mcp_server
        out = io.StringIO()
        mcp_server.serve(self.vault,
                         stdin=io.StringIO("\n".join(json.dumps(m) for m in msgs)),
                         stdout=out)
        return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]

    def test_handshake_and_tool_list(self):
        r = self._run([{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertEqual(r[0]["result"]["serverInfo"]["name"], "agent-brain")
        self.assertEqual({t["name"] for t in r[1]["result"]["tools"]},
                         {"recall", "read_note", "brain_stats"})

    def test_notifications_get_no_reply(self):
        r = self._run([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                       {"jsonrpc": "2.0", "id": 9, "method": "ping"}])
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["id"], 9)

    def test_path_traversal_is_refused(self):
        for evil in ("../../etc/passwd", "..\\..\\Windows\\System32\\config\\SAM",
                     "/etc/shadow"):
            r = self._run([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "read_note", "arguments": {"note": evil}}}])
            self.assertIn("No such note", r[0]["result"]["content"][0]["text"])

    def test_unknown_tool_is_a_protocol_error(self):
        r = self._run([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "drop_tables", "arguments": {}}}])
        self.assertEqual(r[0]["error"]["code"], -32601)

    def test_empty_brain_says_so_rather_than_denying_history(self):
        r = self._run([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "recall", "arguments": {"query": "anything"}}}])
        txt = r[0]["result"]["content"][0]["text"]
        self.assertIn("NOT evidence", txt)

    def test_malformed_json_gets_a_parse_error_not_a_crash(self):
        import io
        from agentbrain import mcp_server
        out = io.StringIO()
        mcp_server.serve(self.vault, stdin=io.StringIO("{not json\n"), stdout=out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


class TestSources(unittest.TestCase):
    def test_discovery_never_raises_on_a_bare_machine(self):
        from agentbrain import sources
        for s in sources.discover():
            self.assertIsInstance(s.files, int)

    def test_codex_rollouts_are_detected_by_shape(self):
        from agentbrain import sources
        d = Path(tempfile.mkdtemp())
        try:
            (d / "2026" / "07").mkdir(parents=True)
            (d / "2026" / "07" / "rollout-2026-07-01T00-00-00-abc.jsonl").write_text("{}")
            kind, _ = sources.sniff_kind(d)
            self.assertEqual(kind, sources.KIND_ROLLOUT_CODEX)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestNoThirdPartyImports(unittest.TestCase):
    """The zero-dependency promise, enforced."""

    def test_only_stdlib_is_imported(self):
        import ast
        allowed = set(sys.stdlib_module_names) | {"agentbrain"}
        offenders = {}
        for p in (ROOT / "agentbrain").glob("*.py"):
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                mods = []
                if isinstance(n, ast.Import):
                    mods = [a.name.split(".")[0] for a in n.names]
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    mods = [n.module.split(".")[0]]
                for m in mods:
                    if m not in allowed:
                        offenders.setdefault(p.name, set()).add(m)
        self.assertEqual(offenders, {}, f"third-party imports: {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
