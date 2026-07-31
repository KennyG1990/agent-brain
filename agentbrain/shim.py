#!/usr/bin/env python3
"""
OpenRouter shim — force thinking OFF and JSON mode ON, for any client.

WHY THIS EXISTS
The 2026-07-31 extract logged, repeatedly:

    [graphify] openai returned a hollow response (content=no nodes/edges, output_tokens=0)
    [graphify] LLM returned invalid JSON, skipping chunk (first 200 chars: '```json ...

Two causes, both fixable at the request level and neither reachable from graphify's CLI:

  1. Gemini 2.5 models think by default. Reasoning tokens are billed as output AND consume
     the max_tokens budget, so the model can spend the whole allowance thinking and return
     empty content. OpenRouter's fix (verified against their reasoning-tokens docs):
         "reasoning": {"max_tokens": 0, "exclude": true}
     For Gemini 2.5 this maps to Google's thinkingBudget. For OpenAI-style models,
     "effort": "none" is used instead — this shim sends the right one per model family.

  2. Models wrap JSON in ```json fences. Fix:
         "response_format": {"type": "json_object"}

Rather than patch graphify, sit in front of it. Point the client at this shim and every
/chat/completions request gets both fixes injected on the way out.

USAGE
    python or_shim.py                       # listens on 127.0.0.1:8787
    setx OPENAI_BASE_URL http://127.0.0.1:8787/v1
    # ...then run the rebuild as normal, in a NEW shell so setx takes effect.

Your API key is never stored here — it rides through on the Authorization header the
client already sends.

    --no-json     don't force response_format (if the client sets its own schema)
    --allow-think leave reasoning alone (A/B test: is thinking actually the problem?)
    --port N      default 8787
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://openrouter.ai/api/v1"
TIMEOUT = 600

# Model families whose reasoning is controlled by effort rather than a token budget.
EFFORT_FAMILIES = ("openai/", "x-ai/", "grok")

STATE = {"requests": 0, "thinking_stripped": 0, "json_forced": 0,
         "in": 0, "out": 0, "reasoning": 0, "errors": 0, "empty": 0,
         "peak_ratio": 0.0}
LOCK = threading.Lock()
OPTS = argparse.Namespace(force_json=True, strip_think=True, verbose=True)


def _reasoning_for(model: str) -> dict:
    m = (model or "").lower()
    if any(f in m for f in EFFORT_FAMILIES):
        return {"effort": "none", "exclude": True}
    # Gemini / Anthropic / Qwen style: an explicit budget of zero
    return {"max_tokens": 0, "exclude": True}


def mutate(body: dict) -> tuple[dict, list[str]]:
    """Inject the fixes. Returns (new_body, list of what changed)."""
    changed = []
    if OPTS.strip_think and "reasoning" not in body:
        body["reasoning"] = _reasoning_for(body.get("model", ""))
        changed.append("thinking=off")
    if OPTS.force_json and "response_format" not in body:
        body["response_format"] = {"type": "json_object"}
        changed.append("json_mode")
    return body, changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence the default noisy logger
        pass

    def _relay(self, method: str) -> None:
        path = self.path
        if path.startswith("/v1"):
            path = path[3:]
        url = UPSTREAM + path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        note = ""

        if raw and "chat/completions" in path:
            try:
                body = json.loads(raw)
                body, changed = mutate(body)
                raw = json.dumps(body).encode()
                with LOCK:
                    STATE["requests"] += 1
                    if "thinking=off" in changed:
                        STATE["thinking_stripped"] += 1
                    if "json_mode" in changed:
                        STATE["json_forced"] += 1
                note = f"{body.get('model','?')}  [{', '.join(changed) or 'unchanged'}]"
            except json.JSONDecodeError:
                pass  # not JSON: relay untouched rather than break the client

        req = urllib.request.Request(url, data=raw or None, method=method)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "accept-encoding", "connection"):
                req.add_header(k, v)
        if raw:
            req.add_header("Content-Length", str(len(raw)))

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload, status = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code
            ctype = e.headers.get("Content-Type", "application/json")
            with LOCK:
                STATE["errors"] += 1
        except Exception as e:                       # noqa: BLE001
            payload = json.dumps({"error": {"message": f"shim: {e}"}}).encode()
            status, ctype = 502, "application/json"
            with LOCK:
                STATE["errors"] += 1

        # accounting + the one thing worth shouting about: still-empty completions
        if note and status == 200:
            try:
                d = json.loads(payload)
                u = d.get("usage") or {}
                pt, ct = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
                rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                with LOCK:
                    STATE["in"] += pt; STATE["out"] += ct; STATE["reasoning"] += rt
                    if pt:
                        STATE["peak_ratio"] = max(STATE["peak_ratio"], ct / pt)
                    if not content.strip():
                        STATE["empty"] += 1
                flag = ""
                if rt:
                    flag += f"  !! STILL {rt} reasoning tokens"
                if not content.strip():
                    flag += "  !! EMPTY CONTENT"
                note += f"  in={pt} out={ct}{flag}"
            except (json.JSONDecodeError, AttributeError, IndexError):
                pass
        if note and OPTS.verbose:
            print(f"  {note}", flush=True)

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self._relay("POST")

    def do_GET(self):
        if self.path.rstrip("/") in ("/stats", "/v1/stats"):
            with LOCK:
                body = json.dumps(STATE, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._relay("GET")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-json", action="store_true", help="don't force response_format")
    ap.add_argument("--allow-think", action="store_true", help="leave reasoning untouched")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    OPTS.force_json = not a.no_json
    OPTS.strip_think = not a.allow_think
    OPTS.verbose = not a.quiet

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"OpenRouter shim on http://127.0.0.1:{a.port}/v1  ->  {UPSTREAM}")
    print(f"  thinking : {'FORCED OFF' if OPTS.strip_think else 'untouched'}")
    print(f"  json mode: {'FORCED ON' if OPTS.force_json else 'untouched'}")
    print("\nPoint the client at it, in a NEW shell:")
    print(f"  setx OPENAI_BASE_URL http://127.0.0.1:{a.port}/v1")
    print(f"\nLive stats: http://127.0.0.1:{a.port}/stats     Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\n--- shim summary ---")
    for k, v in STATE.items():
        print(f"  {k:<18} {v:,}")

    # The whole point: replace the guessed output-to-input ratio with a measured one.
    if STATE["in"] and STATE["out"]:
        ratio = STATE["out"] / STATE["in"]
        peak = STATE.get("peak_ratio", ratio)
        print(f"\n  MEASURED output/input ratio: {ratio:.1%} average, {peak:.1%} worst chunk")
        print("  model_picker.py assumes OUTPUT_RATIO = 0.10.")
        if peak > 0.13:
            print(f"  -> The worst chunk needed {peak:.0%}. Raise OUTPUT_RATIO to "
                  f"{peak:.2f} (or lower --token-budget) or dense chunks will keep truncating.")
        elif peak < 0.07:
            print("  -> Comfortably under. You could raise --token-budget for fewer, "
                  "cheaper calls.")
        else:
            print("  -> The 0.10 assumption holds for this corpus. Leave it.")

    if STATE["reasoning"]:
        print(f"\n  !! {STATE['reasoning']:,} reasoning tokens still billed despite "
              "reasoning being forced off.\n     The model or provider ignored the budget — "
              "switch to a model with no 'reasoning' parameter at all.")
    else:
        print("\n  Zero reasoning tokens billed - thinking really was disabled.")
    if STATE["empty"]:
        print(f"\n  !! {STATE['empty']} response(s) STILL had empty content. Thinking was not "
              "the (only) cause; suspect --token-budget or the model itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
