"""The only code that calls our LLM (docs/phase1-design.md, sections 1, 2 and 11).

Every AI call in Phase 1 is one `ask_json`: a system and a user message sent to the
company LLM gateway - or, later, an open-source model running in the office - and one
JSON object back. Settings come from tools/config.json, which is git-ignored because
it holds the gateway's custom headers. Requests are built and sent exactly as the
office-box readiness checks do it (tools/llm_config.py), so what passed there works
here. Header values are never printed, logged or saved.

Switching to another OpenAI-compatible model changes only tools/config.json.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

from tools import llm_config

MIN_TIMEOUT_S = 60.0     # a Phase 1 prompt carries the catalog: allow more than a ping


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
        self._last = 0.0

    @classmethod
    def from_config(cls, path: str | None = None) -> "Llm":
        return cls(llm_config.load(path))

    def describe(self) -> dict:
        """Safe to print or save: the endpoint and model, header names only."""
        return llm_config.summary(self.settings)

    def ask_json(self, step: str, system: str, user: str) -> dict:
        """One request; the JSON object in the reply. Raises LlmError."""
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        body = llm_config.body(self.settings, user, system=system)
        status, text, seconds, error = llm_config.send(
            "POST", self.url, headers=llm_config.headers(self.settings), json_body=body,
            timeout=self.timeout)
        self._last = time.monotonic()
        if error or status != 200:
            reason = error or f"HTTP {status}: {' '.join(text.split())[:300]}"
            self.calls.append(Call(step, round(seconds, 2), False, reason))
            raise LlmError(f"the LLM did not answer ({reason})")
        try:
            data = json.loads(text)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            self.calls.append(Call(step, round(seconds, 2), False, "not a chat completion"))
            raise LlmError(f"the LLM's answer is not a chat completion: "
                           f"{' '.join(text.split())[:300]}") from None
        obj = json_object(content or "")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        if obj is None:
            self.calls.append(Call(step, round(seconds, 2), False, "no JSON object", usage))
            raise LlmError(f"the LLM's reply has no JSON object: "
                           f"{' '.join(str(content).split())[:300]}")
        self.calls.append(Call(step, round(seconds, 2), True, None, usage))
        return obj
