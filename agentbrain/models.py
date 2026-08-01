#!/usr/bin/env python3
"""
OpenRouter model picker — fetch, rank, and EXPLAIN models for graph extraction.

NO GUI IMPORTS. brain_gui.py is a shell over this, so the ranking can be tested headlessly.

WHY A CUSTOM RANKING INSTEAD OF "CHEAPEST FIRST"
Measured on 2026-07-31 during a real extract with google/gemini-2.5-flash-lite:

    [graphify] openai returned a hollow response
               (content=no nodes/edges, output_tokens=0)

That is not a weak model. Gemini 2.5 models THINK by default, and reasoning tokens are
billed as output AND consume the max_tokens budget. Cap the budget and the model can
spend all of it thinking and emit nothing. The endpoint data shows it plainly:
flash-lite prices "internal_reasoning" and lists "reasoning" in supported_parameters.

So the single most predictive attribute for THIS job is not price or benchmark score:
    does the model burn output budget on hidden reasoning?
A pure completion model spends 100% of its budget on the JSON you asked for.

Second most predictive: structured_outputs / response_format support, which stops the
other failure mode in that same log — models wrapping JSON in ```json fences that the
parser then rejects.

Usage:
    python model_picker.py                     # ranked table
    python model_picker.py --notes 756         # cost for a specific corpus size
    python model_picker.py --json
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

MODELS_URL = "https://openrouter.ai/api/v1/models"
TIMEOUT = 30
# measured on this vault: ~21.3K input tokens per note
TOKENS_PER_NOTE = 21_300
# MEASURED 2026-07-31 on the full 757-note rebuild:
#   2,850,983 in -> 1,288,170 out  =  45.2%
# The earlier 10% figure came from a run that only touched 44 notes and was badly
# unrepresentative. Graph extraction emits a LOT relative to its input -- roughly half.
# This is the single most important constant here: it drives every budget suggestion.
OUTPUT_RATIO = 0.45


def fetch(api_key: str = "") -> list[dict]:
    """The model list is PUBLIC. The key is optional and only sent if you have one."""
    req = urllib.request.Request(MODELS_URL, method="GET")
    req.add_header("User-Agent", "AgentBrain/1.0")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"OpenRouter returned HTTP {e.code}. "
            f"{'Check the API key.' if e.code in (401, 403) else ''}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach OpenRouter: {e.reason}") from e
    return data.get("data", [])


def _f(d: dict, k: str) -> float:
    try:
        return float(d.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def analyse(m: dict, notes: int) -> dict | None:
    """Score one model for graph extraction. None = unusable for this job."""
    pricing = m.get("pricing") or {}
    p_in, p_out = _f(pricing, "prompt") * 1e6, _f(pricing, "completion") * 1e6
    ctx = m.get("context_length") or 0
    top = m.get("top_provider") or {}
    max_out = top.get("max_completion_tokens") or 0
    params = set(m.get("supported_parameters") or [])

    if ctx < 60_000:
        return None  # graphify batches many notes per chunk
    is_free = p_in == 0 and p_out == 0

    tok_in = notes * TOKENS_PER_NOTE
    cost = (tok_in / 1e6) * p_in + (tok_in * OUTPUT_RATIO / 1e6) * p_out

    structured = "structured_outputs" in params or "response_format" in params
    thinks = "reasoning" in params or _f(pricing, "internal_reasoning") > 0

    score, why = 0.0, []
    if not thinks:
        score += 40
        why.append("no hidden reasoning — the whole output budget goes to your JSON")
    else:
        why.append(
            "THINKS BY DEFAULT — reasoning tokens eat the output budget; this is "
            "what produced the empty responses in the 2026-07-31 run"
        )
    if structured:
        score += 25
        why.append("supports structured outputs, so no ```json fences to fail parsing")
    else:
        # A hard penalty, not a missing bonus. Cheapness must never float a model that
        # cannot reliably emit parseable JSON — that is half the failures in the log.
        score -= 25
        why.append("no structured-output support — expect fence/format failures")
    if ctx >= 200_000:
        score += 10
        why.append(f"{ctx // 1000}k context fits big chunks")
    elif ctx >= 100_000:
        score += 6
        why.append(f"{ctx // 1000}k context is adequate")
    else:
        why.append(f"{ctx // 1000}k context is tight; expect more chunk splitting")
    if max_out >= 32_768:
        score += 8
        why.append(f"{max_out:,} max output leaves room for large graphs")
    elif max_out and max_out < 16_000:
        score -= 8
        why.append(f"only {max_out:,} max output — truncation risk")

    if is_free:
        score += 5
        why.append("free tier (expect rate limits)")
    else:
        score -= min(cost * 1.2, 30)  # price matters, but never outweighs correctness

    return {
        "id": m.get("id", "?"),
        "name": m.get("name", m.get("id", "?")),
        "in": p_in,
        "out": p_out,
        "ctx": ctx,
        "max_out": max_out,
        "cost": cost,
        "free": is_free,
        "thinks": thinks,
        "structured": structured,
        "score": round(score, 1),
        "why": why,
        "description": (m.get("description") or "").strip(),
    }


# --------------------------------------------------------------------------- #
# token-budget suggestion
# --------------------------------------------------------------------------- #
# graphify's --token-budget is the INPUT tokens batched into one semantic chunk.
# The binding constraint is the OUTPUT side: the JSON describing that chunk has to
# fit inside the model's max_completion_tokens. So the budget must scale with the
# model's output ceiling, not with its context window.
#
# CALIBRATION, AND ITS LIMITS — measured 2026-07-30 on this vault:
#   666,538 input -> 68,628 output  =>  ~10% output-to-input ratio.
# That is an AVERAGE over a corpus that is mostly small notes. The chunks that
# actually truncated were the dense ones, where the ratio is higher. So we aim for
# expected output at only ~25% of the ceiling, leaving 4x headroom for dense chunks.
# This is a HEURISTIC, not a derivation. or_shim.py reports the observed ratio per
# request; once you have real numbers for your corpus, correct OUTPUT_RATIO here.
OUTPUT_HEADROOM = 0.50  # target: expected output uses half the ceiling
THINKING_RESERVE = 0.30  # if the model thinks, assume 70% of the ceiling is burned
BUDGET_FLOOR, BUDGET_CEIL = 8_000, 120_000
GRAPHIFY_DEFAULT_BUDGET = 60_000


def suggest_token_budget(a: dict, thinking_disabled: bool = False) -> dict:
    """Suggested --token-budget for this model, with the reasoning shown."""
    max_out = a.get("max_out") or 0
    why = []
    if not max_out:
        return {
            "budget": GRAPHIFY_DEFAULT_BUDGET,
            "confident": False,
            "why": [
                "this model does not publish max_completion_tokens, so there is "
                "nothing to compute from — using graphify's default of 60,000"
            ],
        }

    usable = max_out
    if a["thinks"] and not thinking_disabled:
        usable = int(max_out * THINKING_RESERVE)
        why.append(
            f"thinks by default, so only ~{THINKING_RESERVE:.0%} of the "
            f"{max_out:,} ceiling is assumed available for actual JSON"
        )
    elif a["thinks"] and thinking_disabled:
        why.append(
            f"thinking forced off via the shim, so the full {max_out:,} output ceiling is usable"
        )
    else:
        why.append(f"pure completion model: the full {max_out:,} output ceiling is usable")

    budget = int(usable * OUTPUT_HEADROOM / OUTPUT_RATIO)
    why.append(
        f"targeting expected output at {OUTPUT_HEADROOM:.0%} of that "
        f"(4x headroom for dense chunks) at the measured {OUTPUT_RATIO:.0%} ratio"
    )

    ctx_cap = int(a["ctx"] * 0.6)
    if budget > ctx_cap:
        budget = ctx_cap
        why.append(f"capped to 60% of the {a['ctx']:,} context window")
    budget = max(BUDGET_FLOOR, min(budget, BUDGET_CEIL))
    if budget in (BUDGET_FLOOR, BUDGET_CEIL):
        why.append(
            f"clamped to the {budget:,} "
            f"{'floor (fewer, larger calls beat thousands of tiny ones)' if budget == BUDGET_FLOOR else 'ceiling'}"
        )
    if budget < GRAPHIFY_DEFAULT_BUDGET:
        why.append(
            "LOWER than graphify's 60,000 default — this is the change that "
            "reduces truncation for this model"
        )
    return {"budget": budget, "confident": True, "why": why}


def verdict(a: dict) -> tuple[str, str]:
    """(one-line badge, colour hint) — the headline for the GUI."""
    if not a["thinks"] and a["structured"]:
        return (
            "RECOMMENDED — pure completion model with structured outputs. Best fit for this job.",
            "ok",
        )
    if a["thinks"] and a["structured"]:
        return (
            "RISKY — thinks by default. Hidden reasoning can consume the whole output "
            "budget and return nothing. Cheap, but this is the known failure mode.",
            "warn",
        )
    if not a["structured"]:
        return (
            "NOT ADVISED — no structured-output support; the extractor will hit "
            "JSON parse failures.",
            "bad",
        )
    return ("Usable.", "")


def matches(a: dict, q: str) -> bool:
    """Filter used by the GUI table. Plain substring, plus a few field: filters."""
    if not q:
        return True
    for term in q.lower().split():
        if term.startswith("thinks:"):
            if a["thinks"] != (term.split(":", 1)[1] in ("yes", "y", "true", "1")):
                return False
        elif term.startswith("struct:"):
            if a["structured"] != (term.split(":", 1)[1] in ("yes", "y", "true", "1")):
                return False
        elif term.startswith("under:"):
            try:
                if a["cost"] > float(term.split(":", 1)[1]):
                    return False
            except ValueError:
                return False
        elif term == "free":
            if not a["free"]:
                return False
        elif term not in a["id"].lower() and term not in a["name"].lower():
            return False
    return True


def rank(models: list[dict], notes: int, limit: int = 40) -> list[dict]:
    out = [a for a in (analyse(m, notes) for m in models) if a]
    out.sort(key=lambda a: -a["score"])
    return out[:limit]


def explain(a: dict, notes: int) -> str:
    """Full text for the GUI description pane."""
    badge, _ = verdict(a)
    price = "FREE" if a["free"] else f"${a['in']:.3f} in / ${a['out']:.3f} out per 1M tokens"
    lines = [
        a["name"],
        a["id"],
        "",
        badge,
        "",
        f"Price:        {price}",
        f"This job:     ~${a['cost']:.2f} for {notes} notes"
        + ("  (free tier)" if a["free"] else ""),
        f"Context:      {a['ctx']:,}",
        f"Max output:   {a['max_out']:,}" if a["max_out"] else "Max output:   not published",
        f"Thinks:       {'YES - risky here' if a['thinks'] else 'no - good'}",
        f"Structured:   {'yes' if a['structured'] else 'NO - risky here'}",
        "",
        "Why this rating:",
    ]
    lines += [f"  - {w}" for w in a["why"]]
    if a["description"]:
        lines += ["", "OpenRouter's description:", ""]
        d = a["description"]
        while d:
            lines.append("  " + d[:96])
            d = d[96:]
    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=756)
    ap.add_argument("--key", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    ranked = rank(fetch(a.key), a.notes, a.top)
    if a.json:
        print(json.dumps(ranked, indent=2))
        return 0
    print(f"{'model':<44}{'$/Min':>8}{'$/Mout':>8}{'ctx':>9}{'think':>7}{'struct':>7}{'JOB $':>8}")
    for r in ranked:
        print(
            f"{r['id']:<44}{r['in']:>8.3f}{r['out']:>8.3f}{r['ctx']:>9}"
            f"{'YES' if r['thinks'] else 'no':>7}{'yes' if r['structured'] else 'NO':>7}"
            f"{r['cost']:>8.2f}"
        )
    print()
    print(explain(ranked[0], a.notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
