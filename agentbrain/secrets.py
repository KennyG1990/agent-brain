"""
API key storage that is safe by default, with ZERO third-party dependencies.

THE RULE: a plaintext key is never written to disk. Not in the repo, not in the config
file, not in a log. If no OS keystore is available we store NOTHING and tell the user to
use an environment variable — an inconvenient app is better than a leaked key.

Backends, in order of preference:

  1. Windows  — DPAPI (CryptProtectData) via ctypes. Encrypts to the CURRENT USER ACCOUNT.
                Another user on the same machine cannot decrypt it; the blob is useless on
                another machine. Ships with Windows, needs no pip install.
  2. macOS    — Keychain via the `security` CLI. Access-controlled by the OS.
  3. Linux    — Secret Service via `secret-tool` (gnome-keyring / KWallet) when present.
  4. Nowhere  — no keystore: refuse to persist, and say so plainly.

WHAT THIS DOES NOT PROTECT AGAINST, stated honestly:
DPAPI and Keychain protect against other users, stolen drives, and files accidentally
committed or copied. They do NOT protect against malware already running as you — that
code can ask the same OS to decrypt. No local app can defend against that. If your threat
model includes it, use the environment variable and a short-lived key.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE = "AgentBrain"
ACCOUNT = "openrouter"

# Environment variables are read FIRST and never written by us.
ENV_VARS = ("AGENTBRAIN_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "GEMINI_API_KEY")


# --------------------------------------------------------------------------- #
# Windows DPAPI
# --------------------------------------------------------------------------- #
def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(encrypt: bool, data: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN: never pop a dialog from a background thread
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI {'encrypt' if encrypt else 'decrypt'} failed "
                      f"(error {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------- #
# macOS Keychain / Linux Secret Service
# --------------------------------------------------------------------------- #
def _mac_available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _linux_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def _run(cmd: list[str], stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or p.stderr or "").strip()


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def backend() -> str:
    if _dpapi_available():
        return "Windows DPAPI (encrypted to your user account)"
    if _mac_available():
        return "macOS Keychain"
    if _linux_available():
        return "Linux Secret Service (secret-tool)"
    return "none"


def can_store() -> bool:
    return backend() != "none"


def _blob_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / SERVICE
    d.mkdir(parents=True, exist_ok=True)
    return d / "key.dpapi"          # ciphertext only, never plaintext


def from_env() -> tuple[str, str]:
    """(key, which_var). Environment always wins and is never written by us."""
    for v in ENV_VARS:
        val = os.environ.get(v, "").strip()
        if val:
            return val, v
    return "", ""


def save(key: str) -> tuple[bool, str]:
    key = (key or "").strip()
    if not key:
        return delete()
    if _dpapi_available():
        try:
            blob = _dpapi(True, key.encode())
        except OSError as e:
            return False, f"DPAPI failed: {e}"
        p = _blob_path()
        p.write_bytes(base64.b64encode(blob))
        return True, f"Saved, encrypted to your Windows account ({p})."
    if _mac_available():
        rc, out = _run(["security", "add-generic-password", "-U",
                        "-s", SERVICE, "-a", ACCOUNT, "-w", key])
        return (rc == 0), ("Saved to your macOS Keychain." if rc == 0 else f"Keychain error: {out}")
    if _linux_available():
        rc, out = _run(["secret-tool", "store", "--label", SERVICE,
                        "service", SERVICE, "account", ACCOUNT], stdin=key)
        return (rc == 0), ("Saved to your keyring." if rc == 0
                           else f"secret-tool error: {out}")
    return False, (
        "No OS keystore is available on this system, so the key was NOT saved — "
        "writing it in plaintext would be worse than not saving it.\n\n"
        "Set an environment variable instead:\n"
        "    setx OPENROUTER_API_KEY sk-or-...        (Windows, then open a new shell)\n"
        "    export OPENROUTER_API_KEY=sk-or-...      (macOS/Linux)")


def load() -> tuple[str, str]:
    """(key, source_description). Environment first, then the OS keystore."""
    key, var = from_env()
    if key:
        return key, f"environment variable {var}"
    if _dpapi_available():
        p = _blob_path()
        if p.exists():
            try:
                return _dpapi(False, base64.b64decode(p.read_bytes())).decode(), \
                    "Windows DPAPI (this user account)"
            except (OSError, ValueError):
                return "", "stored key could not be decrypted (different user or machine?)"
    if _mac_available():
        rc, out = _run(["security", "find-generic-password",
                        "-s", SERVICE, "-a", ACCOUNT, "-w"])
        if rc == 0 and out:
            return out, "macOS Keychain"
    if _linux_available():
        rc, out = _run(["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT])
        if rc == 0 and out:
            return out, "Linux keyring"
    return "", "not set"


def delete() -> tuple[bool, str]:
    msgs = []
    p = _blob_path()
    if p.exists():
        try:
            p.unlink(); msgs.append("removed the encrypted local blob")
        except OSError as e:
            msgs.append(f"could not remove blob: {e}")
    if _mac_available():
        _run(["security", "delete-generic-password", "-s", SERVICE, "-a", ACCOUNT])
        msgs.append("cleared the Keychain entry")
    if _linux_available():
        _run(["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT])
        msgs.append("cleared the keyring entry")
    return True, ("; ".join(msgs) or "nothing stored")


def mask(key: str) -> str:
    """For display and logs. NEVER print a raw key."""
    k = (key or "").strip()
    if not k:
        return "(none)"
    # Anything short enough that a prefix+suffix would reveal most of it gets nothing.
    return f"{k[:6]}…{k[-4:]}  ({len(k)} chars)" if len(k) > 20 else f"(hidden, {len(k)} chars)"


def purge_report() -> str:
    """Delete the stored key AND tell the truth about what we cannot delete."""
    _, msg = delete()
    lines = [f"Keystore ({backend()}): {msg}"]
    still = [v for v in ENV_VARS if os.environ.get(v, "").strip()]
    if still:
        lines += ["", "STILL SET — environment variables are outside this app's control:"]
        lines += [f"  {v}" for v in still]
        lines += ["", "Clear them yourself:"]
        if sys.platform == "win32":
            lines += [f'  setx {v} ""' for v in still]
            lines += ["  (then close every shell and this app, and reopen)"]
        else:
            lines += [f"  unset {v}   # and remove it from your shell profile" for v in still]
    else:
        lines += ["", "No API-key environment variables are set. Nothing else to clear."]
    return "\n".join(lines)


def audit(repo_root: Path | None = None) -> list[str]:
    """Look for a key that leaked somewhere it must never be. Used by the GUI + tests."""
    problems = []
    from . import config
    cfg_p = config.config_path()
    if cfg_p.exists():
        try:
            raw = json.loads(cfg_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if str(raw.get("api_key") or "").strip():
            problems.append(f"PLAINTEXT KEY IN {cfg_p} — delete that field; "
                            "this build never writes it.")
    root = repo_root or Path(__file__).resolve().parents[1]
    gi = root / ".gitignore"
    if not gi.exists():
        problems.append(f"no .gitignore at {root}")
    for pat in ("settings.json", "key.dpapi", ".env"):
        if gi.exists() and pat not in gi.read_text(encoding="utf-8", errors="replace"):
            problems.append(f".gitignore does not cover {pat}")
    return problems


def main() -> int:
    """CLI:  python -m agentbrain.secrets [--status | --purge | --set]"""
    import argparse
    ap = argparse.ArgumentParser(description="Agent Brain key storage")
    ap.add_argument("--purge", action="store_true", help="delete the stored key")
    ap.add_argument("--set", action="store_true", help="store a key (prompts, no echo)")
    a = ap.parse_args()

    if a.purge:
        print(purge_report()); return 0
    if a.set:
        import getpass
        if not can_store():
            print(f"No OS keystore here ({backend()}). Refusing to write plaintext.\n")
            print(purge_report()); return 1
        ok, msg = save(getpass.getpass("Paste your API key (input hidden): "))
        print(msg); return 0 if ok else 1

    key, source = load()
    print(f"Backend : {backend()}")
    print(f"Key     : {mask(key)}")
    print(f"Source  : {source}")
    probs = audit()
    print("Audit   : " + ("CLEAN" if not probs else ""))
    for p in probs:
        print(f"  ! {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
