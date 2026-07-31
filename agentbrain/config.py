"""
Settings. **NO SECRETS LIVE IN THIS FILE OR THE JSON IT WRITES.**

API keys go through secrets.py, which uses the OS keystore (Windows DPAPI, macOS
Keychain, Linux Secret Service) and refuses to persist anything at all if no
keystore exists. settings.json holds preferences only, so it is safe to sync,
back up, or accidentally attach to a bug report.

`api_key` is deliberately absent from DEFAULTS. If an older build left one in
settings.json, load() strips it and secrets.audit() reports it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = "AgentBrain"

# provider id -> (label, default base url, default model, env var, key url)
PROVIDERS: dict[str, dict] = {
    "openai": {
        "label": "OpenAI",
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env": "OPENAI_API_KEY",
        "keys": "https://platform.openai.com/api-keys",
        "note": "Paid. Cheapest good option is a mini/small model.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "env": "OPENROUTER_API_KEY",
        "keys": "https://openrouter.ai/keys",
        "note": "One key, hundreds of models, including some free ones.",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base": "https://api.anthropic.com/v1",
        "model": "claude-haiku-4-5-20251001",
        "env": "ANTHROPIC_API_KEY",
        "keys": "https://console.anthropic.com/settings/keys",
        "note": "Paid. Haiku is the cheap one and is plenty for this job.",
    },
    "gemini": {
        "label": "Google Gemini",
        "base": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.0-flash",
        "env": "GEMINI_API_KEY",
        "keys": "https://aistudio.google.com/apikey",
        "note": "Has a genuinely free tier — best starting point if you have no key.",
    },
    "custom": {
        "label": "Custom / local (OpenAI-compatible)",
        "base": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "env": "OPENAI_API_KEY",
        "keys": "",
        "note": "Ollama, LM Studio, vLLM, Together, Groq — anything speaking the "
                "OpenAI chat format. Local servers usually ignore the key field.",
    },
}

DEFAULTS = {
    "vault": "",            # blank -> <config dir>/vault
    "provider": "gemini",
    "base_url": "",         # blank -> provider default
    "model": "",            # blank -> provider default
    "extra_sources": [],
    "use_graphify": "auto",  # auto | never | always
    "max_note_chars": 60000,
    "max_msg_chars": 4000,
}
# Keys that must NEVER be written to settings.json. Enforced in save() and load().
FORBIDDEN = ("api_key", "apikey", "key", "token", "secret", "password")


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "settings.json"


def _strip_secrets(d: dict) -> dict:
    """Belt and braces: nothing that smells like a credential survives."""
    return {k: v for k, v in d.items() if k.lower() not in FORBIDDEN}


def load() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        try:
            cfg.update(_strip_secrets(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            pass  # corrupt settings must never stop the app starting
    return cfg


def save(cfg: dict) -> Path:
    p = config_path()
    p.write_text(json.dumps(_strip_secrets(cfg), indent=2), encoding="utf-8")
    if os.name == "posix":
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p


def vault_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load()
    v = (cfg.get("vault") or "").strip()
    d = Path(v).expanduser() if v else (config_dir() / "vault")
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(cfg: dict | None = None) -> dict:
    """Effective provider settings. The key comes from secrets.py — never from cfg."""
    from . import secrets  # local import: secrets imports config for audit()
    cfg = cfg or load()
    pid = cfg.get("provider", "gemini")
    spec = PROVIDERS.get(pid, PROVIDERS["gemini"])
    env_key = os.environ.get(spec["env"], "").strip()
    key, source = (env_key, f"environment variable {spec['env']}") if env_key else secrets.load()
    return {
        "provider": pid,
        "label": spec["label"],
        "base_url": (cfg.get("base_url") or "").strip() or spec["base"],
        "model": (cfg.get("model") or "").strip() or spec["model"],
        "api_key": key,
        "key_source": source,
        "key_from_env": bool(env_key),
        "key_masked": secrets.mask(key),
        "env_var": spec["env"],
        "keys_url": spec["keys"],
    }


def has_key(cfg: dict | None = None) -> bool:
    r = resolve(cfg)
    # local OpenAI-compatible servers legitimately need no key
    if r["provider"] == "custom" and "localhost" in r["base_url"]:
        return True
    return bool(r["api_key"])
