"""Interactive wizard — the no-argument ``python -m tella`` experience.

A non-technical user runs ``RUN.bat`` / ``./RUN.sh`` and answers a few
questions. Two ways to start a video:

  * type a TOPIC  → Tella writes the story, then renders it
  * drop a .txt   → Tella narrates YOUR story verbatim (cleaned for TTS),
                    splitting it into as many scenes as the story needs

The visual style for AI images is always cinematic, so there is no theme
step. Power users can still pass CLI flags (see ``tella.cli``) to override
anything, including ``--theme``.

All prompts are ASCII-only on purpose: Windows ``cmd.exe`` defaults to the
cp1252 code page and chokes on box-drawing / check-mark glyphs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from tella.ingest.genre_pace import pace_name_for
from tella.ingest.lang_detect import detect_language
from tella.ingest.script_cleaner import clean_script_text

_LANGS: list[tuple[str, str]] = [
    ("vi", "Tieng Viet"),
    ("en", "English"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh", "Chinese"),
    ("de", "German"),
    ("fr", "French"),
    ("es", "Spanish"),
]

_ASPECTS: list[tuple[str, str]] = [
    ("9:16", "Vertical short  (TikTok / Reels / YouTube Shorts)"),
    ("16:9", "Horizontal      (YouTube / landscape)"),
]

_MEDIA: list[tuple[str, str]] = [
    ("ai_image", "AI image  - cinematic art, characters stay consistent across scenes"),
    ("stock_photo", "Stock photo - real Pexels photographs, fast"),
    ("stock_video", "Stock video - real Pexels video clips, most motion"),
]

_DURATIONS: list[tuple[str, str]] = [
    ("short", "Short    - 5-8 scenes, about 60-120s"),
    ("detailed", "Detailed - 12-20 scenes, about 4-6 minutes"),
]

_GENDERS: list[tuple[str, str]] = [
    ("male", "Male voice"),
    ("female", "Female voice"),
]

# AI-image visual style. Maps to an internal theme.
#   cinematic -> realistic, filmic
#   cartoon   -> vibrant cartoon (kid-friendly) + always kid storytelling voice
_STYLES: list[tuple[str, str]] = [
    ("cinematic", "Cinematic - realistic, filmic (great for adults / real-world topics)"),
    ("cartoon", "Cartoon   - colorful illustration, kid-friendly (great for children)"),
]
_STYLE_TO_THEME = {"cinematic": "cinematic", "cartoon": "playful"}

_LANG_NAME = dict(_LANGS)


def _resolve_theme(media_source: str, style: str | None) -> str:
    """AI image uses the chosen style's theme; stock modes stay cinematic."""
    if media_source == "ai_image" and style:
        return _STYLE_TO_THEME.get(style, "cinematic")
    return "cinematic"


@dataclass
class WizardResult:
    # mode == "topic": Tella writes the story from `topic`.
    # mode == "script": Tella narrates `user_script` verbatim.
    mode: str
    target_lang: str
    aspect_ratio: str
    media_source: str
    duration_mode: str
    voice_gender: str
    theme: str = "cinematic"
    voice_pace_name: str | None = None
    topic: str = ""
    user_script: str | None = None


def _print_header() -> None:
    print()
    print("=" * 60)
    print("  Tella - turn a topic (or your own story) into a video")
    print("=" * 60)


def _looks_like_text_file(raw: str) -> str | None:
    """If ``raw`` is a path to an existing .txt/.md file, return it, else None.

    Handles the way a dragged file lands in a terminal: an absolute path,
    sometimes wrapped in single or double quotes, sometimes with a trailing
    space.
    """
    candidate = raw.strip().strip('"').strip("'").strip()
    if not candidate:
        return None
    if os.path.isfile(candidate) and candidate.lower().endswith((".txt", ".md")):
        return candidate
    return None


def _read_story_file(path: str) -> str:
    """Read a story file with a forgiving encoding chain, then clean it."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return clean_script_text(f.read())
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: read bytes and replace undecodable chars.
    with open(path, "rb") as f:
        return clean_script_text(f.read().decode("utf-8", errors="replace"))


def _ask_choice(title: str, options: list[tuple[str, str]], default_index: int = 0) -> str:
    print()
    print(title)
    for i, (_value, label) in enumerate(options, start=1):
        marker = "*" if (i - 1) == default_index else " "
        print(f"  {marker} {i}) {label}")
    while True:
        try:
            raw = input(f"Choose [{default_index + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if not raw:
            return options[default_index][0]
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= len(options):
                return options[n - 1][0]
        print(f"  (enter a number 1-{len(options)})")


def _label(options: list[tuple[str, str]], value: str) -> str:
    for v, lbl in options:
        if v == value:
            return lbl.split(" - ")[0].strip() if " - " in lbl else lbl.strip()
    return value


def run_wizard() -> WizardResult:
    """Walk the user through every choice and return their selections."""
    _print_header()

    # ── Step 1 — story input (topic text OR a dropped .txt path) ────────
    print()
    print("Step 1 - Your story")
    print("  Type a TOPIC for Tella to write about, e.g.")
    print('     the tortoise and the hare')
    print("  OR drop a .txt file here (your own finished story) and press Enter.")
    while True:
        try:
            raw = input("Topic or file: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if raw:
            break
        print("  (please type a topic, or drop a .txt file)")

    file_path = _looks_like_text_file(raw)
    if file_path:
        user_script = _read_story_file(file_path)
        if len(user_script) < 30:
            print("  (that file looks empty after cleaning — switching to topic mode)")
            file_path = None

    if file_path:
        mode = "script"
        target_lang = detect_language(user_script)
        words = len(user_script.split())
        print(f"\n  Loaded story: {os.path.basename(file_path)}")
        print(f"  Detected language: {_LANG_NAME.get(target_lang, target_lang)}")
        print(f"  ~{words} words -> Tella will split it into scenes automatically.")
        # Length follows the story; pick the heavier model/token budget for
        # anything beyond a very short piece.
        duration_mode = "detailed" if words > 220 else "short"

        aspect_ratio = _ask_choice("Step 2 - Aspect ratio", _ASPECTS, 0)
        media_source = _ask_choice("Step 3 - Where do the visuals come from?", _MEDIA, 0)
        style = None
        if media_source == "ai_image":
            style = _ask_choice("Step 4 - AI image style", _STYLES, 0)
        voice_gender = _ask_choice("Step 5 - Narrator voice", _GENDERS, 0)

        theme = _resolve_theme(media_source, style)
        voice_pace_name = pace_name_for(user_script, theme)

        print()
        print("-" * 60)
        print("  Ready to render (your story, narrated verbatim):")
        print(f"    Source    : {os.path.basename(file_path)} ({words} words)")
        print(f"    Language  : {_LANG_NAME.get(target_lang, target_lang)} (auto-detected)")
        print(f"    Aspect    : {aspect_ratio}")
        print(f"    Visuals   : {_label(_MEDIA, media_source)}"
              + (f" / {style}" if style else ""))
        print(f"    Voice     : {_label(_GENDERS, voice_gender)} ({voice_pace_name})")
        print("-" * 60)
        _confirm()
        return WizardResult(
            mode="script",
            target_lang=target_lang,
            aspect_ratio=aspect_ratio,
            media_source=media_source,
            duration_mode=duration_mode,
            voice_gender=voice_gender,
            theme=theme,
            voice_pace_name=voice_pace_name,
            user_script=user_script,
            topic=os.path.splitext(os.path.basename(file_path))[0],
        )

    # ── Topic mode ──────────────────────────────────────────────────────
    topic = raw
    target_lang = _ask_choice("Step 2 - Narration language", _LANGS, 0)
    aspect_ratio = _ask_choice("Step 3 - Aspect ratio", _ASPECTS, 0)
    media_source = _ask_choice("Step 4 - Where do the visuals come from?", _MEDIA, 0)
    style = None
    if media_source == "ai_image":
        style = _ask_choice("Step 5 - AI image style", _STYLES, 0)
    duration_mode = _ask_choice("Step 6 - How long?", _DURATIONS, 0)
    voice_gender = _ask_choice("Step 7 - Narrator voice", _GENDERS, 0)

    theme = _resolve_theme(media_source, style)
    voice_pace_name = pace_name_for(topic, theme)

    print()
    print("-" * 60)
    print("  Ready to render:")
    print(f"    Topic     : {topic}")
    print(f"    Language  : {_label(_LANGS, target_lang)}")
    print(f"    Aspect    : {aspect_ratio}")
    print(f"    Visuals   : {_label(_MEDIA, media_source)}"
          + (f" / {style}" if style else ""))
    print(f"    Length    : {_label(_DURATIONS, duration_mode)}")
    print(f"    Voice     : {_label(_GENDERS, voice_gender)} ({voice_pace_name})")
    print("-" * 60)
    _confirm()
    return WizardResult(
        mode="topic",
        target_lang=target_lang,
        aspect_ratio=aspect_ratio,
        media_source=media_source,
        duration_mode=duration_mode,
        voice_gender=voice_gender,
        theme=theme,
        voice_pace_name=voice_pace_name,
        topic=topic,
    )


def _confirm() -> None:
    try:
        go = input("Start? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt
    if go in ("n", "no"):
        raise KeyboardInterrupt


__all__ = ["WizardResult", "run_wizard"]
