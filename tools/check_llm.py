#!/usr/bin/env python3
"""Is the LLM API answering? A minimal check using tools/config.json.

    python tools/check_llm.py
    python tools/check_llm.py --config other.json --timeout 60

Requests are built like the pipeline's: a system and a user message, every
parameter in the "payload" block of config.json (e.g. temperature,
max_tokens), the custom headers, sent with `requests`. Two prompts - a tiny
one, and a larger one (2-3k tokens) whose right answer is known. For each it
prints the HTTP status and time, finish reason, token counts and the answer,
and explains an empty answer or a prompt that was cut. Writes nothing. For
the full Phase 1 check, run tools/check_phase1_env.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import llm_config

ROWS = "".join(f"sample {i:03d}: ts=10:{i // 60:02d}:{i % 60:02d} read_ios={1000 + 37 * i}\n"
               for i in range(200))
PROMPTS = [  # (label, user prompt, text the answer must contain)
    ("tiny", "Reply with exactly: OK", "OK"),
    ("larger (200 sample rows)",
     ROWS + "\nWhat is read_ios in the last sample? Reply with the number only.",
     str(1000 + 37 * 199)),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check that the LLM API answers.")
    ap.add_argument("--config", help="settings file (default: tools/config.json)")
    ap.add_argument("--timeout", type=float, help="seconds (default: timeoutSeconds)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        llm = llm_config.llm_settings(llm_config.load(args.config))
    except llm_config.ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    url, headers = llm_config.chat_url(llm), llm_config.headers(llm)
    timeout = args.timeout or float(llm.get("timeoutSeconds") or 120)
    pause = float(llm.get("rateLimitSeconds") or 0)
    budget = llm_config.payload_params(llm).get("max_tokens")
    for key, value in llm_config.summary(llm).items():
        print(f"  {key:<17}: {value}")
    print(f"  {'http client':<17}: {llm_config.client_name()}")
    for problem in llm_config.header_problems(llm):
        print(f"  WARNING: header {problem} - the gateway may reject the request")

    failed = 0
    for i, (label, prompt, expect) in enumerate(PROMPTS):
        if i and pause:
            time.sleep(pause)
        approx = len(prompt) // 4
        status, text, secs, error = llm_config.send(
            "POST", url, headers, json_body=llm_config.body(llm, prompt), timeout=timeout)
        print(f"\n{label} (~{approx} tokens): HTTP {status}, {secs:.1f} s")
        if status is None:
            hint = (f" - timed out after {timeout:g} s: raise timeoutSeconds or use --timeout"
                    if "timed out" in (error or "").lower() else "")
            print(f"  FAILED{hint}\n  {error}")
            failed += 1
            continue
        if status != 200:
            print(f"  FAILED\n  {text[:1500]}")
            failed += 1
            continue
        try:
            data = json.loads(text)
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            print(f"  FAILED - not an OpenAI-style reply: {text[:1500]}")
            failed += 1
            continue
        content = (msg.get("content") or "").strip()
        usage = data.get("usage") or {}
        used = usage.get("prompt_tokens")
        finish = choice.get("finish_reason")
        print(f"  finish_reason: {finish}; tokens: prompt={used} "
              f"completion={usage.get('completion_tokens')}")
        extra = [k for k in msg if k not in ("role", "content") and msg.get(k)]
        if extra:
            print(f"  other fields in the reply: {extra}")
        if isinstance(used, int) and used < approx * 3 / 4:
            print(f"  PROMPT CUT - the model saw {used} of ~{approx} tokens "
                  "(the server dropped part of it without an error)")
        if not content:
            print("  EMPTY ANSWER" + (
                f" - finish_reason=length: all {budget} tokens were spent before an answer "
                "(a 'thinking' model reasons first); raise max_tokens in config.json"
                if finish == "length" else ""))
            print(f"  full message: {json.dumps(msg)[:1200]}")
            failed += 1
            continue
        print(f"  answer: {content[:300]}")
        if expect not in content:
            print(f"  WRONG - expected {expect!r}")
            failed += 1

    print(f"\nRESULT: {len(PROMPTS) - failed} of {len(PROMPTS)} prompts answered correctly.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
