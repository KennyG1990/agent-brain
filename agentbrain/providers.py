"""
LLM calls with ZERO third-party dependencies — urllib only.

That is a deliberate constraint: it means someone can unzip this and run it with
nothing but Python. No `pip install requests`, no venv, no build step.

Three request shapes are covered:
  * OpenAI-compatible  (OpenAI, OpenRouter, Together, Groq, Ollama, LM Studio, vLLM)
  * Anthropic          (api.anthropic.com — different auth header and body)
  * Gemini             (generativelanguage.googleapis.com — different again)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 120
UA = "AgentBrain/1.0 (+https://github.com/)"


class ProviderError(RuntimeError):
    """Raised with a message meant to be shown to a human, not logged and swallowed."""


# Transient failures are normal at volume; one-shot requests turn a rate limit into
# a lost chunk. Retry only what is actually retryable.
RETRY_CODES = (408, 409, 425, 429, 500, 502, 503, 504, 529)
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], log=None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", UA)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code not in RETRY_CODES or attempt == MAX_ATTEMPTS:
                raise ProviderError(_explain(e.code, detail)) from e
            # honour Retry-After when the provider bothers to send one
            wait = BACKOFF_BASE ** attempt
            try:
                wait = max(wait, float(e.headers.get("Retry-After") or 0))
            except (TypeError, ValueError):
                pass
            last = e
        except urllib.error.URLError as e:
            if attempt == MAX_ATTEMPTS:
                raise ProviderError(
                    f"Could not reach {url}.\n\n{e.reason}\n\n"
                    "Check your internet connection, or the Base URL in Settings if "
                    "you're pointing at a local server."
                ) from e
            wait, last = BACKOFF_BASE ** attempt, e
        except TimeoutError as e:
            if attempt == MAX_ATTEMPTS:
                raise ProviderError(f"Timed out after {TIMEOUT}s talking to {url}.") from e
            wait, last = BACKOFF_BASE ** attempt, e

        if log:
            log(f"    retry {attempt}/{MAX_ATTEMPTS - 1} in {wait:.0f}s "
                f"({last.__class__.__name__})")
        time.sleep(min(wait, 60))

    raise ProviderError(f"Gave up after {MAX_ATTEMPTS} attempts: {last}")


def _explain(code: int, detail: str) -> str:
    common = {
        401: "The API key was rejected (401). Check for a stray space, or that the key "
             "matches the provider you selected.",
        403: "Access forbidden (403). The key may lack permission for this model.",
        404: "Not found (404). Usually the model name is wrong for this provider.",
        429: "Rate limited or out of quota (429). Wait, or check your billing/free-tier limits.",
        500: "The provider had a server error (500). Not your fault — try again.",
        529: "The provider is overloaded (529). Try again shortly.",
    }
    return f"{common.get(code, f'HTTP {code} from the provider.')}\n\n{detail}"


# --------------------------------------------------------------------------- #
class SpendCap(RuntimeError):
    """Raised the moment a run would exceed the user's ceiling. Hard stop, not a warning."""


class Meter:
    """Tracks tokens and money across a run, and enforces the cap.

    Exists because a hidden scheduled task once burned ~$13 unnoticed. Any spend this
    app causes must be visible while it happens and stoppable at a number the user set.
    """

    def __init__(self, usd_per_m_in: float = 0.0, usd_per_m_out: float = 0.0,
                 cap_usd: float = 0.0) -> None:
        self.pin, self.pout, self.cap = usd_per_m_in, usd_per_m_out, cap_usd
        self.tin = self.tout = self.calls = 0
        self._lock = threading.Lock()

    @property
    def usd(self) -> float:
        return (self.tin / 1e6) * self.pin + (self.tout / 1e6) * self.pout

    def add(self, tin: int, tout: int) -> None:
        with self._lock:
            self.tin += tin; self.tout += tout; self.calls += 1

    def check(self, est_in: int = 0, est_out: int = 0) -> None:
        """Block BEFORE the call that would breach the cap, not after.

        Checking only spend-so-far lets one more request through, so the cap is
        always overshot by a call. Pass the size of the request you are about to
        make and it is refused pre-flight instead.
        """
        if not self.cap:
            return
        projected = self.usd + (est_in / 1e6) * self.pin + (est_out / 1e6) * self.pout
        if projected > self.cap:
            raise SpendCap(
                f"Spend cap would be exceeded: ${self.usd:.2f} spent, this request "
                f"would take it to ~${projected:.2f}, cap is ${self.cap:.2f}. "
                f"Stopped BEFORE sending it, after {self.calls} calls.\n\n"
                "Everything extracted so far is saved. Raise the cap and re-run to "
                "continue from where it stopped.")

    def estimate(self, text: str) -> tuple[int, int]:
        """Rough token estimate for pre-flight checks. ~4 chars/token is the usual
        English approximation; output assumed at 10% of input (measured on real runs)."""
        tin = max(1, len(text) // 4)
        return tin, int(tin * 0.10)

    def line(self) -> str:
        cap = f" / ${self.cap:.2f} cap" if self.cap else ""
        return (f"{self.calls} calls · {self.tin:,} in / {self.tout:,} out · "
                f"${self.usd:.3f}{cap}")


def chat(cfg: dict, system: str, user: str, max_tokens: int = 2000,
         meter: "Meter | None" = None, log=None) -> str:
    """One completion. `cfg` is the dict from config.resolve()."""
    from . import consent
    ok, why = consent.gate(cfg.get("base_url", ""))
    if not ok:
        raise ProviderError(
            f"Refusing to send your notes to {cfg.get('base_url','the provider')}.\n\n{why}")
    if meter:
        meter.check(*meter.estimate(system + user))   # pre-flight, so the cap never overshoots
    provider, key = cfg["provider"], cfg["api_key"]
    base, model = cfg["base_url"].rstrip("/"), cfg["model"]

    if provider == "anthropic":
        data = _post(
            f"{base}/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {"model": model, "max_tokens": max_tokens, "system": system,
             "messages": [{"role": "user", "content": user}]},
            log=log,
        )
        u = data.get("usage") or {}
        _meter(meter, u.get("input_tokens", 0), u.get("output_tokens", 0))
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts).strip()

    if provider == "gemini":
        data = _post(
            f"{base}/models/{model}:generateContent?key={key}",
            {},
            {"systemInstruction": {"parts": [{"text": system}]},
             "contents": [{"role": "user", "parts": [{"text": user}]}],
             # thinkingBudget 0 disables Gemini's default thinking, which otherwise
             # consumes the output allowance and can return zero content.
             "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1,
                                  "responseMimeType": "application/json",
                                  "thinkingConfig": {"thinkingBudget": 0}}},
            log=log,
        )
        u = data.get("usageMetadata") or {}
        _meter(meter, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0))
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError(f"Gemini returned no candidates: {json.dumps(data)[:300]}")
        parts = cands[0].get("content", {}).get("parts", [])
        return "\n".join(p.get("text", "") for p in parts).strip()

    # OpenAI-compatible (openai, openrouter, custom/local)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if "openrouter" in base:
        # Verified against OpenRouter's reasoning-tokens docs: hidden thinking is billed
        # as output AND eats max_tokens, so a thinking model can return nothing at all.
        body["reasoning"] = {"max_tokens": 0, "exclude": True}
    data = _post(f"{base}/chat/completions", headers, body, log=log)
    u = data.get("usage") or {}
    _meter(meter, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError(f"No choices returned: {json.dumps(data)[:300]}")
    return (choices[0].get("message", {}).get("content") or "").strip()


def _meter(meter: "Meter | None", tin: int, tout: int) -> None:
    if meter:
        meter.add(int(tin or 0), int(tout or 0))


def test_key(cfg: dict) -> tuple[bool, str]:
    """Cheap round-trip so the user finds out now, not 10 minutes into a rebuild."""
    if not cfg["api_key"] and "localhost" not in cfg["base_url"]:
        return False, (f"No API key set. Paste one in Settings, or set the "
                       f"{cfg['env_var']} environment variable.")
    try:
        out = chat(cfg, "Reply with exactly: OK", "Reply with exactly: OK", max_tokens=16)
    except ProviderError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"Unexpected error: {e}"
    if "ok" in out.lower():
        return True, f"{cfg['label']} responded. Model: {cfg['model']}"
    return True, f"{cfg['label']} responded (said {out[:60]!r}). Model: {cfg['model']}"
