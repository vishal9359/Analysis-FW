"""The only code that calls our LLM (docs/phase1-design.md, sections 1, 2 and 11).

Every AI call in Phase 1 is one `ask_json`: a system and a user message sent to the
company LLM gateway - or, later, an open-source model running in the office - and one
JSON object back. Settings come from tools/config.json, which is git-ignored because
it holds the gateway's custom headers. Requests are built and sent exactly as the
office-box readiness checks do it (tools/llm_config.py), so what passed there works
here. Header values are never printed, logged or saved.

With log_to(path), every call is also written to a text file as it happens: the whole
request - address, header names, model and settings, the system and user messages - and
the whole response - status, why the model stopped, tokens, its reply, and the JSON
object the code takes from it. The runner keeps one such file beside each record.

Switching to another OpenAI-compatible model changes only tools/config.json.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from tools import llm_config

MIN_TIMEOUT_S = 60.0     # a Phase 1 prompt carries the catalog: allow more than a ping
RULE = "=" * 88


class LlmError(Exception):
    """The LLM could not be reached, or did not answer with a JSON object."""


@dataclass
class Call:
    """One LLM call, as kept in a request's record - never the headers."""
    step: str
    seconds: float
    ok: bool
    error: str | None = None
    usage: dict | None = None
    finish_reason: str | None = None   # "length": the reply was cut at max_tokens

    def as_dict(self) -> dict:
        return asdict(self)


def json_object(text: str) -> dict | None:
    """The first JSON object in a model's reply (a ```json fence is allowed)."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


class Llm:
    """Our LLM, as configured in tools/config.json."""

    def __init__(self, settings: dict) -> None:
        self.settings = llm_config.llm_settings(settings)
        self.url = llm_config.chat_url(self.settings)
        self.timeout = max(float(self.settings.get("timeoutSeconds") or 0), MIN_TIMEOUT_S)
        self.pause = float(self.settings.get("rateLimitSeconds") or 0)
        self.calls: list[Call] = []
        self.log_path: Path | None = None
        self._log_first = 0                  # calls before the log was started
        self._last = 0.0

    @classmethod
    def from_config(cls, path: str | None = None) -> "Llm":
        return cls(llm_config.load(path))

    def describe(self) -> dict:
        """Safe to print or save: the endpoint and model, header names only."""
        return llm_config.summary(self.settings)

    def log_to(self, path, title: str = "") -> None:
        """From now on, write every call's request and response to this file."""
        self.log_path = Path(path)
        self._log_first = len(self.calls)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(title.rstrip() + "\n\n" if title else "", encoding="utf-8")

    def ask_json(self, step: str, system: str, user: str) -> dict:
        """One request; the JSON object in the reply. Raises LlmError."""
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = llm_config.body(self.settings, user, system=system)
        headers = llm_config.headers(self.settings)
        started = datetime.now()
        status, text, seconds, error = llm_config.send(
            "POST", self.url, headers=headers, json_body=body, timeout=self.timeout)
        self._last = time.monotonic()
        data, content, completion = None, None, False
        if not error and status == 200:
            try:
                data = json.loads(text)
                content = data["choices"][0]["message"]["content"]
                completion = True
            except (ValueError, KeyError, IndexError, TypeError):
                data = None
        obj = json_object(content or "") if completion else None
        usage = data.get("usage") if completion and isinstance(data.get("usage"), dict) else None
        finish = data["choices"][0].get("finish_reason") if completion else None
        self._log(step, started, seconds, body, headers, status, text, error, content,
                  completion, obj, usage, finish)
        if error or status != 200:
            reason = error or f"HTTP {status}: {' '.join(text.split())[:300]}"
            self.calls.append(Call(step, round(seconds, 2), False, reason))
            raise LlmError(f"the LLM did not answer ({reason})")
        if not completion:
            self.calls.append(Call(step, round(seconds, 2), False, "not a chat completion"))
            raise LlmError(f"the LLM's answer is not a chat completion: "
                           f"{' '.join(text.split())[:300]}")
        if obj is None:
            self.calls.append(Call(step, round(seconds, 2), False, "no JSON object", usage, finish))
            raise LlmError(f"the LLM's reply has no JSON object: "
                           f"{' '.join(str(content).split())[:300]}")
        self.calls.append(Call(step, round(seconds, 2), True, None, usage, finish))
        return obj

    def _log(self, step, started, seconds, body, headers, status, text, error, content,
             completion, obj, usage, finish) -> None:
        """One call, request and response in full, appended to the log file - header
        names only, never their values (they hold the gateway's credentials)."""
        if self.log_path is None:
            return
        n = len(self.calls) - self._log_first + 1
        settings = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}"
                             for k, v in body.items() if k != "messages")
        out = [RULE, f"LLM call {n} - step {step} - {started:%Y-%m-%d %H:%M:%S} - "
                     f"{seconds:.2f} s", RULE, "",
               "REQUEST",
               f"  POST {self.url}",
               f"  headers: {', '.join(sorted(headers))} (values are not logged)",
               f"  {settings}"]
        for m in body.get("messages") or []:
            message = str(m.get("content", ""))
            out += ["", f"---- {m.get('role')} message ({len(message):,} characters) ----",
                    message]
        out += ["", "RESPONSE"]
        if error:
            out.append(f"  no answer: {error}")
        else:
            tokens = (f" - tokens: prompt {usage.get('prompt_tokens', '?')}, completion "
                      f"{usage.get('completion_tokens', '?')}" if usage else "")
            out.append(f"  HTTP {status}" + (f" - finish_reason: {finish}" if finish else "")
                       + tokens)
            if completion:
                out += ["", "---- the model's reply ----", str(content)]
            else:
                out += ["", "---- the response body ----", text[:20000]]
            out += ["", "---- the JSON object the code uses ----",
                    json.dumps(obj, indent=2, ensure_ascii=False) if obj is not None
                    else "(none: no JSON object in the reply)"]
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n\n")
