"""Saved channels — a small name + avatar the wizard can pick from.

A "channel" is just a brand label (and optional avatar image) shown on the
video. Define one by dropping a folder under ``channels/`` at the repo root:

    channels/
      my-brand/
        channel.json     -> {"name": "My Brand", ...}
        avatar.png        -> optional square image (logo / face)

The wizard lists these so you don't retype the name every run. You can also
just type a fresh name in the wizard without saving a channel at all.

channel.json minimal shape::

    {"name": "My Brand"}

channel.json full shape (a "scoped" channel — unlocks AI auto-ideate)::

    {
      "name": "My Brand",
      "niche_guide": "one-paragraph description of what this channel is about",
      "seed_examples": ["example topic 1", "example topic 2", ...],
      "defaults": {
        "lang": "vi",                 # vi/en/ja/ko/zh/de/fr/es
        "media": "ai_image",          # ai_image / stock_photo / stock_video
        "style": "cinematic",         # cinematic / cartoon (only used for ai_image)
        "voice_gender": "male",       # male / female
        "duration": "short",          # short / detailed
        "aspect": "9:16"              # 9:16 / 16:9
      }
    }

When a scoped channel is picked the wizard hides per-step prompts for fields
covered by ``defaults`` and offers an **Auto AI topic** option that uses
:mod:`tella.ingest.seeder` (Gemini ideation + embedding dedup vs the
channel's ``history.jsonl``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tella.channels")

# Repo-root ``channels/`` (this file lives at tella/channels.py).
_CHANNELS_DIR = Path(__file__).resolve().parent.parent / "channels"

_AVATAR_NAMES = ("avatar.png", "avatar.jpg", "avatar.jpeg", "avatar.webp")

# Valid enum values for each defaults field. Anything outside this set is
# dropped at load time with a warning rather than crashing the wizard.
_VALID = {
    "lang": {"vi", "en", "ja", "ko", "zh", "de", "fr", "es"},
    "media": {"ai_image", "stock_photo", "stock_video"},
    "style": {"cinematic", "cartoon"},
    "voice_gender": {"male", "female"},
    "duration": {"short", "detailed"},
    "aspect": {"9:16", "16:9"},
}


@dataclass(frozen=True)
class Channel:
    slug: str
    name: str
    avatar_path: str | None  # absolute path, or None

    # Scope fields — present only when channel.json supplies them.
    niche_guide: str = ""
    seed_examples: tuple[str, ...] = ()
    defaults: dict[str, str] = field(default_factory=dict)

    # Path to the per-channel history.jsonl (absolute, may not exist yet).
    history_path: str = ""

    @property
    def is_scoped(self) -> bool:
        """True when the channel supplies a niche_guide + at least 1 seed.

        A scoped channel unlocks AI auto-ideate; otherwise the wizard
        only offers manual topic / dropped-script input for that channel.
        """
        return bool(self.niche_guide.strip()) and bool(self.seed_examples)


def _find_avatar(folder: Path) -> str | None:
    for n in _AVATAR_NAMES:
        p = folder / n
        if p.is_file():
            return str(p)
    return None


def _coerce_defaults(raw: object) -> dict[str, str]:
    """Filter a ``defaults`` block down to known keys with valid enum values."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, allowed in _VALID.items():
        val = raw.get(key)
        if isinstance(val, str) and val in allowed:
            out[key] = val
    return out


def _coerce_seeds(raw: object) -> tuple[str, ...]:
    """Filter ``seed_examples`` to a tuple of non-empty strings."""
    if not isinstance(raw, list):
        return ()
    return tuple(s.strip() for s in raw if isinstance(s, str) and s.strip())


def list_channels() -> list[Channel]:
    """Return saved channels (sorted by name), skipping malformed folders."""
    if not _CHANNELS_DIR.is_dir():
        return []
    out: list[Channel] = []
    for folder in sorted(_CHANNELS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        cfg = folder / "channel.json"
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            name = (data.get("name") or "").strip()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("skipping channel %s: %s", folder.name, exc)
            continue
        if not name:
            continue
        out.append(Channel(
            slug=folder.name,
            name=name,
            avatar_path=_find_avatar(folder),
            niche_guide=str(data.get("niche_guide") or "").strip(),
            seed_examples=_coerce_seeds(data.get("seed_examples")),
            defaults=_coerce_defaults(data.get("defaults")),
            history_path=str(folder / "history.jsonl"),
        ))
    out.sort(key=lambda c: c.name.lower())
    return out


__all__ = ["Channel", "list_channels"]
