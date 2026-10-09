"""Settings for the tools in this folder, read from tools/config.json.

config.json holds credentials, so it is git-ignored; start from
config.example.json. The LLM block may sit under "llm" (as in the pipeline's
config.json) or at the top level:

    {"llm": {"baseUrl": "...", "defaultModel": "...", "customHeaders": {...}}}

Only the OpenAI-compatible API (/chat/completions) is supported.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "config.json")
EXAMPLE_PATH = os.path.join(HERE, "config.example.json")


class ConfigError(Exception):
    pass


def load(path: str | None = None) -> dict:
    """The whole settings file as a dict."""
    path = path or DEFAULT_PATH
    if not os.path.isfile(path):
        raise ConfigError(f"settings file not found: {path} - copy {EXAMPLE_PATH} "
                          f"to {DEFAULT_PATH} and fill it in")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return cfg


def llm_settings(cfg: dict) -> dict:
    """The LLM block, checked."""
    llm = cfg.get("llm", cfg)
    base = str(llm.get("baseUrl") or "").strip()
    if not base or "<" in base:
        raise ConfigError("llm.baseUrl is not set")
    if not llm.get("defaultModel"):
        raise ConfigError("llm.defaultModel is not set")
    if str(llm.get("provider") or "openai").lower() == "ollama":
        raise ConfigError("provider 'ollama' is not supported here; these tools use "
                          "the OpenAI-compatible API (/chat/completions)")
    return llm


def _base(llm: dict) -> str:
    base = str(llm["baseUrl"]).strip().rstrip("/")
    return base[: -len("/chat/completions")] if base.endswith("/chat/completions") else base


def chat_url(llm: dict) -> str:
    return _base(llm) + "/chat/completions"


def models_url(llm: dict) -> str:
    return _base(llm) + "/models"


def headers(llm: dict) -> dict:
    """Request headers: the custom headers, plus a Bearer key if apiKey is set."""
    h = {"Content-Type": "application/json"}
    h.update({str(k): str(v) for k, v in (llm.get("customHeaders") or {}).items()})
    if llm.get("apiKey"):
        h["Authorization"] = f"Bearer {llm['apiKey']}"
    return h


def summary(llm: dict) -> dict:
    """Safe to print or save: header NAMES only, never their values.

    numCtx is listed only when set: it matters for a local (Ollama) model,
    which silently cuts prompts longer than numCtx."""
    out = {
        "endpoint": chat_url(llm),
        "model": llm.get("defaultModel"),
        "timeoutSeconds": llm.get("timeoutSeconds"),
        "rateLimitSeconds": llm.get("rateLimitSeconds", 0),
        "maxContextTokens": llm.get("maxContextTokens"),
        "apiKey": "set" if llm.get("apiKey") else "not set",
        "customHeaders": sorted(str(k) for k in (llm.get("customHeaders") or {})),
    }
    if llm.get("numCtx"):
        out["numCtx"] = llm.get("numCtx")
    return out
