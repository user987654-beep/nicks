"""External free providers through OpenCode (OpenRouter, Groq, Gemini, Kilo, NVIDIA NIM, ...).

Catalog adapters discover current models; OpenCode remains the worker transport. Provider presets and
documented account contracts are in federation.py, not promotional model lists. An operator registers a
provider with a key FILE/environment reference and an account policy:

* zero-priced         provider pricing must be explicitly zero for every billed component;
* free-tier-account   operator attestation of a supported free account with no billing enabled;
* free-quota-only     current exact-model quota evidence plus an enforceable provider-side billing stop.

Keys are read from a 0600 file at run time and never leave the orchestrator process: discovery gets a
placeholder for OpenCode discovery; direct catalog discovery uses the key only in the parent. Workers get a
per-run token plus the loopback URL of the key proxy (nicks/keyproxy.py), which forwards only the approved
model upstream with the real key. Output scrubbing remains as defence in depth only.
"""
from __future__ import annotations

import json
import os
import stat
import sqlite3
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from .config import state_dir

POLICIES = ("zero-priced", "free-tier-account", "free-quota-only")


class ProviderConfigError(RuntimeError):
    pass


def _read_key(cfg: dict) -> str | None:
    if cfg.get("key_source"):
        return read_existing_key(cfg["key_source"])
    if cfg.get("key_file"):
        p = Path(os.path.expanduser(cfg["key_file"]))
        try:
            mode = p.stat().st_mode
        except OSError:
            return None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProviderConfigError(f"{p} must not be readable by group/others (chmod 600)")
        return p.read_text().strip() or None
    if cfg.get("key_env"):
        return os.environ.get(cfg["key_env"]) or None
    return None


def read_existing_key(source: dict) -> str | None:
    """Read only the named API credential from a private OpenCode store; never import OAuth sessions."""
    p = Path(source["path"]).expanduser()
    try:
        if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        if source["kind"] == "opencode-json":
            value = json.loads(p.read_text()).get(source["provider"], {})
        elif source["kind"] == "opencode-sqlite":
            con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("SELECT value FROM credential WHERE integration_id=? AND active=1 "
                                  "ORDER BY time_updated DESC LIMIT 1", (source["provider"],)).fetchone()
                value = json.loads(row[0]) if row else {}
            finally:
                con.close()
        else:
            return None
        key = value.get("key") if isinstance(value, dict) and value.get("type") == "api" else None
        return key.strip() if isinstance(key, str) and key.strip() else None
    except (OSError, ValueError, sqlite3.Error, AttributeError):
        return None


def provider_env(settings, provider: str | None = None) -> dict[str, dict[str, str]]:
    """{provider: {ENV_NAME: key}} for configured providers whose key is available (optionally just one)."""
    out = {}
    for pid, cfg in (settings.free_providers or {}).items():
        if provider and pid != provider:
            continue
        try:
            key = _read_key(cfg)
        except ProviderConfigError:
            if cfg.get("auto_discovered"):
                continue
            raise
        if key and cfg.get("env"):
            out[pid] = {name: key for name in cfg["env"]}
    return out


def all_keys(settings) -> list[str]:
    return sorted({k for env in provider_env(settings).values() for k in env.values()})


def models_dev_env(provider: str, url: str = "https://models.dev/api.json") -> list[str]:
    # models.dev rejects urllib's default User-Agent with 403 (observed live), so send a plain client name.
    req = urllib.request.Request(url, headers={"User-Agent": "council-of-nicks", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - public catalog OpenCode itself uses
            data = json.loads(r.read())
    except (OSError, ValueError) as exc:
        raise ProviderConfigError(f"cannot look up {provider}'s key variable on models.dev ({exc}); "
                                  "pass --env NAME (e.g. --env OPENROUTER_API_KEY)") from exc
    if provider not in data:
        raise ProviderConfigError(f"models.dev has no provider {provider!r}")
    return list(data[provider].get("env") or [])


def _config_path() -> Path:
    return state_dir() / "config.json"


def _load() -> dict:
    p = _config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".providers-", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(cfg, indent=2) + "\n")
        os.replace(name, p)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def config_lock():
    import fcntl
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / ".providers.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def add(provider: str, *, policy: str, key_file: str | None = None, key_env: str | None = None,
        env: list[str] | None = None, models: list[str] | None = None, anonymous: bool = False,
        endpoint: str | None = None, quota_pool: str | None = None, quota_scope: str | None = None,
        entitlements_file: str | None = None) -> dict:
    if policy not in POLICIES:
        raise ProviderConfigError(f"policy must be one of {POLICIES}")
    if not anonymous and bool(key_file) == bool(key_env):
        raise ProviderConfigError("give exactly one of --key-file or --key-env")
    from .federation import PRESETS, FREE_ACCOUNTS
    preset = PRESETS.get(provider)
    if policy == "free-tier-account" and (provider not in FREE_ACCOUNTS or
                                         (endpoint and endpoint != PRESETS[provider][0])):
        raise ProviderConfigError("no documented free-account contract for this endpoint; use verified zero pricing "
                                  "or free-quota-only with hard-stop evidence")
    if anonymous and (key_file or key_env or provider != "kilo" or policy != "zero-priced"):
        raise ProviderConfigError("anonymous access is supported only for Kilo's verified zero-priced routes")
    entry = {"policy": policy, "env": env or ([preset[1]] if preset else models_dev_env(provider))}
    if preset or endpoint:
        from urllib.parse import urlparse
        target = endpoint or preset[0]
        u = urlparse(target)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ProviderConfigError("endpoint requires https without credentials/query/fragment")
        if anonymous and target != PRESETS["kilo"][0]:
            raise ProviderConfigError("anonymous access requires the official Kilo gateway")
        entry.update(adapter="openai", endpoint=target, anonymous=anonymous,
                     quota_scope=quota_scope or (preset[2] if preset else "account"),
                     quota_pool=quota_pool or f"{provider}:default")
    if policy == "free-quota-only" and not entitlements_file:
        raise ProviderConfigError("free-quota-only requires --entitlements-file with current quota and hard-stop evidence")
    if entitlements_file:
        entry["entitlements_file"] = str(Path(entitlements_file).expanduser().resolve())
        from .federation import read_entitlements
        import time
        read_entitlements(entry, time.time())
    if not entry["env"]:
        raise ProviderConfigError(f"no API-key environment variable known for {provider}; pass --env NAME")
    if key_file:
        entry["key_file"] = str(Path(os.path.expanduser(key_file)).resolve())
        _read_key(entry)                                  # validates permissions now
    elif key_env:
        entry["key_env"] = key_env
    if models:
        entry["models"] = models
    with config_lock():
        cfg = _load()
        cfg.setdefault("free_providers", {})[provider] = entry
        cfg["auto_disabled_providers"] = [p for p in cfg.get("auto_disabled_providers", []) if p != provider]
        _save(cfg)
    return entry


def remove(provider: str) -> bool:
    with config_lock():
        cfg = _load()
        removed = cfg.get("free_providers", {}).pop(provider, None) is not None
        cfg["auto_disabled_providers"] = sorted(set(cfg.get("auto_disabled_providers", [])) | {provider})
        _save(cfg)
    return removed
