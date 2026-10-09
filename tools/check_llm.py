#!/usr/bin/env python3
"""Is the LLM API answering? A minimal check using tools/config.json.

    python tools/check_llm.py
    python tools/check_llm.py --max-tokens 8192 --timeout 60
    python tools/check_llm.py --config other.json

Sends two prompts - a tiny one, and a larger one (2-3k tokens) whose right
answer is known - and prints, for each: HTTP status and time, finish reason, token
counts and the answer. Explains an empty answer and a prompt that was cut.
Standard library only; writes nothing. For the full Phase 1 check, run
tools/check_phase1_env.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

import llm_config

ROWS = "".join(f"sample {i:03d}: ts=10:{i // 60:02d}:{i % 60:02d} read_ios={1000 + 37 * i}\n"
               for i in range(200))
PROMPTS = [  # (label, prompt, text the answer must contain)
    ("tiny", "Reply with exactly: OK", "OK"),
    ("larger (200 sample rows)",
     ROWS + "\nWhat is read_ios in the last sample? Reply with the number only.",
     str(1000 + 37 * 199)),
]


def post(url, headers, payload, timeout):
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.monotonic() - t0
    except Exception as exc:  # refused, DNS, TLS, timeout
        return None, f"{type(exc).__name__}: {exc}", time.monotonic() - t0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check that the LLM API answers.")
    ap.add_argument("--config", help="settings file (default: tools/config.json)")
    ap.add_argument("--max-tokens", type=int, default=2048)
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
    for key, value in llm_config.summary(llm).items():
        print(f"  {key:<17}: {value}")

    failed = 0
    for i, (label, prompt, expect) in enumerate(PROMPTS):
        if i and pause:
            time.sleep(pause)
        approx = len(prompt) // 4
        payload = {"model": llm["defaultModel"], "temperature": 0,
                   "max_tokens": args.max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
        status, body, secs = post(url, headers, payload, timeout)
        print(f"\n{label} (~{approx} tokens): HTTP {status}, {secs:.1f} s")
        if status != 200:
            hint = (f" - timed out after {timeout:g} s: raise timeoutSeconds or use --timeout"
                    if "timed out" in body.lower() else "")
            print(f"  FAILED{hint}\n  {body[:800]}")
            failed += 1
            continue
        try:
            data = json.loads(body)
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            print(f"  FAILED - not an OpenAI-style reply: {body[:800]}")
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
                f" - finish_reason=length: all {args.max_tokens} tokens were spent before "
                "an answer (a 'thinking' model reasons first); try --max-tokens 8192"
                if finish == "length" else ""))
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
