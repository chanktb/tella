"""Shared Gemini client + JSON-parse helpers for Tella.

Centralized so the ingest, planner, and any future Gemini-touching modules
don't each duplicate the credential-resolution / loose-JSON logic.

Key resolution priority (matches story-teller's pattern so a deployment
can use the same env var across both tools):

  1. Explicit ``api_key`` argument
  2. ``GEMINI_API_KEYS`` (plural, comma-separated, random pick per call)
  3. ``GEMINI_API_KEY`` (single)
  4. ``GOOGLE_API_KEY`` (Google's older naming)

Raises :class:`ValueError` rather than ``SystemExit`` so callers can
catch + degrade (e.g. CLI prints a friendly setup hint).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re

from google import genai

logger = logging.getLogger("tella._gemini")

# Defaults — overridable via env or call args.
DEFAULT_MODEL_TRANSLATE = "gemini-flash-lite-latest"
DEFAULT_MODEL_EXPAND = "gemini-flash-latest"
DEFAULT_MODEL_PLAN_SHORT = "gemini-flash-lite-latest"
DEFAULT_MODEL_PLAN_DETAILED = "gemini-flash-latest"

_KEY_HINT = (
    "Set GEMINI_API_KEY (or GEMINI_API_KEYS for rotation, or GOOGLE_API_KEY). "
    "Free key at https://aistudio.google.com/apikey"
)


def get_client(api_key: str | None = None) -> genai.Client:
    """Return a configured ``genai.Client``. See module docstring for key resolution."""
    if api_key:
        return genai.Client(api_key=api_key)

    keys_csv = (os.environ.get("GEMINI_API_KEYS") or "").strip()
    if keys_csv:
        keys = [k.strip() for k in keys_csv.split(",") if k.strip()]
        if keys:
            return genai.Client(api_key=random.choice(keys))

    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise ValueError(f"No Gemini key on environment. {_KEY_HINT}")
    return genai.Client(api_key=key)


def is_transient_quota_error(exc: BaseException) -> bool:
    """True when ``exc`` is a Gemini 429/503/quota error worth a key rotation.

    Used by retry loops to decide whether to rebuild the client (rotating to
    a different key from ``GEMINI_API_KEYS``) vs reusing the existing one.
    Pattern-matches on substrings because google-genai exceptions don't
    expose status codes uniformly across versions.
    """
    s = str(exc).upper()
    return any(token in s for token in (
        "RESOURCE_EXHAUSTED",
        "RATE_LIMIT",
        "RATE LIMIT",
        "QUOTA",
        "UNAVAILABLE",
        " 429",
        " 503",
    ))


def parse_json_loose(raw: str) -> dict | list:
    """Parse Gemini text into JSON, tolerating fences + trailing noise.

    Handles:
      - ``` / ```json fences
      - Trailing prose after the JSON block
      - Leading whitespace
      - Returns dict OR list (Gemini sometimes returns a top-level array)

    Raises:
        json.JSONDecodeError: when no balanced JSON can be located.
    """
    raw = (raw or "").strip()
    if not raw:
        raise json.JSONDecodeError("empty response", "", 0)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Walk to find the first balanced { or [ block.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = raw.find(open_ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == open_ch:
                    depth += 1
                elif c == close_ch:
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break

    raise json.JSONDecodeError("no balanced JSON block found", raw, len(raw))


__all__ = [
    "DEFAULT_MODEL_TRANSLATE",
    "DEFAULT_MODEL_EXPAND",
    "DEFAULT_MODEL_PLAN_SHORT",
    "DEFAULT_MODEL_PLAN_DETAILED",
    "get_client",
    "is_transient_quota_error",
    "parse_json_loose",
]
