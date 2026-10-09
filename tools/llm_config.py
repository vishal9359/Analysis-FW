"""Settings and HTTP for the tools in this folder, read from tools/config.json.

config.json holds credentials, so it is git-ignored; start from
config.example.json. The LLM block may sit under "llm" (as in the pipeline's
config.json) or at the top level:

    {"llm": {"baseUrl": "...", "defaultModel": "...", "customHeaders": {...},
             "systemPrompt": "...", "payload": {"temperature": 0.1, "max_tokens": 2048}}}

Every request body is built the way the pipeline builds it:

    {"model": defaultModel,
     "messages": [{"role": "system", ...}, {"role": "user", ...}],
     **payload}                     # every key of "payload", as written

Only the OpenAI-compatible API (/chat/completions) is supported.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "config.json")
EXAMPLE_PATH = os.path.join(HERE, "config.example.json")

# used only when config.json leaves them out; these are the pipeline's values
DEFAULT_PAYLOAD = {"temperature": 0.1, "max_tokens": 2048}
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


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
    if not isinstance(llm.get("payload", {}), dict):
        raise ConfigError("llm.payload must be a JSON object, e.g. "
                          '{"temperature": 0.1, "max_tokens": 2048}')
    return llm


def payload_params(llm: dict) -> dict:
    """The request-body parameters from config.json ("model" and "messages"
    are always set by the caller, so they are not taken from here)."""
    params = llm.get("payload")
    params = dict(DEFAULT_PAYLOAD if params is None else params)
    params.pop("model", None)
    params.pop("messages", None)
    return params


def system_prompt(llm: dict) -> str:
    return str(llm.get("systemPrompt") or DEFAULT_SYSTEM_PROMPT)


def body(llm: dict, user: str, system: str | None = None, **extra) -> dict:
    """A request body shaped like the pipeline's: system + user message, then
    every payload parameter from config.json, then any test-specific extras."""
    out = {"model": llm["defaultModel"],
           "messages": [{"role": "system", "content": system or system_prompt(llm)},
                        {"role": "user", "content": user}]}
    out.update(payload_params(llm))
    out.update(extra)
    return out


def _base(llm: dict) -> str:
    base = str(llm["baseUrl"]).strip().rstrip("/")
    return base[: -len("/chat/completions")] if base.endswith("/chat/completions") else base


def chat_url(llm: dict) -> str:
    return _base(llm) + "/chat/completions"


def models_url(llm: dict) -> str:
    return _base(llm) + "/models"


def headers(llm: dict) -> dict:
    """Request headers: Content-Type, the custom headers, and a Bearer key if
    apiKey is set - the same set the pipeline sends."""
    h = {"Content-Type": "application/json"}
    h.update({str(k): str(v) for k, v in (llm.get("customHeaders") or {}).items()})
    if llm.get("apiKey"):
        h["Authorization"] = f"Bearer {llm['apiKey']}"
    return h


def header_problems(llm: dict) -> list[str]:
    """Custom headers whose value looks unfilled - a common cause of a
    rejected request. Values are never printed, only described."""
    out = []
    for name, value in (llm.get("customHeaders") or {}).items():
        value = str(value)
        if not value.strip():
            out.append(f"{name} is empty")
        elif value.rstrip().endswith(":") or "<" in value:
            out.append(f"{name} looks unfilled (ends with ':' or holds a placeholder)")
    return out


def summary(llm: dict) -> dict:
    """Safe to print or save: header NAMES only, never their values.

    numCtx is listed only when set: it matters for a local (Ollama) model,
    which silently cuts prompts longer than numCtx."""
    out = {
        "endpoint": chat_url(llm),
        "model": llm.get("defaultModel"),
        "payload": payload_params(llm),
        "systemPrompt": system_prompt(llm),
        "timeoutSeconds": llm.get("timeoutSeconds"),
        "rateLimitSeconds": llm.get("rateLimitSeconds", 0),
        "maxContextTokens": llm.get("maxContextTokens"),
        "apiKey": "set" if llm.get("apiKey") else "not set",
        "customHeaders": sorted(str(k) for k in (llm.get("customHeaders") or {})),
    }
    if llm.get("numCtx"):
        out["numCtx"] = llm.get("numCtx")
    return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def client_name() -> str:
    try:
        import requests
        return f"requests {requests.__version__}"
    except ImportError:
        return "urllib (requests is not installed)"


def send(method, url, headers=None, json_body=None, data=None, timeout=60, verify=True):
    """One HTTP request, made the way the pipeline makes it.

    With `requests` when it is installed - its certificate bundle (and
    REQUESTS_CA_BUNDLE), proxy settings and User-Agent, and header names sent
    exactly as written. Falls back to urllib only when requests is missing.
    Returns (status or None, response text, seconds, error text or None)."""
    t0 = time.monotonic()
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        try:
            r = requests.request(method, url, headers=headers, json=json_body, data=data,
                                 timeout=timeout, verify=verify)
            return r.status_code, r.text, time.monotonic() - t0, None
        except Exception as exc:  # refused, DNS, TLS, timeout
            return None, "", time.monotonic() - t0, f"{type(exc).__name__}: {exc}"

    hdrs = dict(headers or {})
    raw = data
    if json_body is not None:
        raw = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers=hdrs, method=method)
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return (resp.status, resp.read().decode("utf-8", "replace"),
                    time.monotonic() - t0, None)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.monotonic() - t0, None
    except Exception as exc:
        return None, "", time.monotonic() - t0, f"{type(exc).__name__}: {exc}"
