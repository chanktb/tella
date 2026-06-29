"""Interactive wizard — the no-argument ``python -m tella`` experience.

Flow (decided by the channel pick on step 1):

* SCOPED channel (channel.json has ``niche_guide`` + ``seed_examples``)
    Step 1 — pick the channel
    Step 2 — topic source: Auto AI ideate (deduped vs the channel's
             ``history.jsonl``) / Type a topic / Drop a .txt
    Channel ``defaults`` (lang, media, style, voice, duration, aspect) are
    applied silently — the wizard skips those questions and the user sees
    them in the confirmation panel.

* AD-HOC (no channel, or a channel with name only)
    Step 1 — pick channel (or none / type new)
    Step 2 — topic / dropped .txt
    Steps 3-8 — full per-step questions (lang, aspect, media, style,
    duration, voice gender)

All prompts are ASCII-only — Windows ``cmd.exe`` cp1252 chokes on glyphs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from tella.channels import Channel, list_channels
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
    ("ai_image", "AI image  - generated art, characters stay consistent (pick style next)"),
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
    channel_name: str = ""
    channel_avatar: str = ""
    channel_slug: str = ""        # set when a saved channel is picked
    topic: str = ""
    user_script: str | None = None

    # Auto-ideate signals — populated only when the topic came from the
    # seeder, so the CLI can append to history.jsonl after a successful render.
    topic_embedding: list[float] | None = None


def _print_header() -> None:
    print()
    print("=" * 60)
    print("  Tella - turn a topic (or your own story) into a video")
    print("=" * 60)


def _looks_like_text_file(raw: str) -> str | None:
    """If ``raw`` is a path to an existing .txt/.md file, return it, else None."""
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


def _ask_optional_text(prompt: str) -> str:
    """Prompt for optional free text. Empty input returns ""."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def _ask_required_text(prompt: str) -> str:
    """Prompt for required free text — keep asking until non-empty."""
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if raw:
            return raw
        print("  (please enter a non-empty value)")


def _ask_channel_step1() -> Channel | tuple[str, str] | None:
    """Step 1 — pick a saved channel, type a new name, or go ad-hoc.

    Returns:
        * :class:`Channel` — a saved channel was picked
        * ``(new_name, "")`` tuple — user typed a brand-new name (ad-hoc but
          with a label on the video)
        * ``None`` — no channel / clean video / fully ad-hoc
    """
    saved = list_channels()
    options: list[tuple[str, str]] = []
    for c in saved:
        tag = "  [scoped: AI auto-topic]" if c.is_scoped else ""
        avatar = "  +avatar" if c.avatar_path else ""
        options.append((c.slug, f"{c.name}{tag}{avatar}"))
    options.append(("__new__", "Type a new channel name (just a label on the video)"))
    options.append(("__none__", "No channel  (clean video, no name)"))

    pick = _ask_choice("Step 1 - Pick a channel", options, default_index=0)
    if pick == "__none__":
        return None
    if pick == "__new__":
        name = _ask_optional_text("  Channel name (Enter to skip): ")
        return (name, "")
    for c in saved:
        if c.slug == pick:
            return c
    return None


def _label(options: list[tuple[str, str]], value: str) -> str:
    for v, lbl in options:
        if v == value:
            return lbl.split(" - ")[0].strip() if " - " in lbl else lbl.strip()
    return value


def _confirm() -> None:
    try:
        go = input("Start? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt
    if go in ("n", "no"):
        raise KeyboardInterrupt


def _ideate_with_channel(channel: Channel, target_lang: str) -> tuple[str, list[float]]:
    """Run the Gemini ideation + dedup loop. Returns (topic, embedding).

    Lets the user Accept / Regenerate / fall back to manual entry. Raises
    KeyboardInterrupt if the user cancels mid-loop.
    """
    from pathlib import Path

    from tella.ingest.seeder import (
        generate_topic,
        load_history,
    )

    history_path = Path(channel.history_path)
    history = load_history(history_path)
    print()
    print(f"  Channel scope: {channel.niche_guide[:120]}"
          + ("..." if len(channel.niche_guide) > 120 else ""))
    print(f"  History size : {len(history)} past topics")

    while True:
        print("  Generating a fresh topic with Gemini ...")
        try:
            pick = generate_topic(
                niche_guide=channel.niche_guide,
                seed_topics=list(channel.seed_examples),
                history=history,
                target_lang=target_lang,
            )
        except Exception as exc:
            print(f"  (ideation failed: {exc})")
            print("  Falling back to manual topic entry.")
            topic = _ask_required_text("  Topic: ")
            return topic, []

        print()
        print(f"  >> {pick.topic}")
        if pick.max_history_similarity > 0:
            print(f"     (dedup ok — closest past topic similarity {pick.max_history_similarity:.2f}: "
                  f"{pick.closest_history_topic!r})")
        else:
            print("     (no past topics yet — first one for this channel)")

        verdict = _ask_choice(
            "  Use this topic?",
            [
                ("accept", "Accept and render"),
                ("regen", "Regenerate a different one"),
                ("manual", "Forget AI — type my own topic"),
            ],
            default_index=0,
        )
        if verdict == "accept":
            return pick.topic, pick.embedding
        if verdict == "manual":
            topic = _ask_required_text("  Topic: ")
            return topic, []
        # else regen → loop


def _index_of(options: list[tuple[str, str]], value: str | None, fallback: int = 0) -> int:
    """Return the index of the first option whose value equals ``value``.

    Used to pre-select a channel's stored default as the Enter-to-accept choice
    for a per-step question.
    """
    if value is None:
        return fallback
    for i, (v, _) in enumerate(options):
        if v == value:
            return i
    return fallback


def _flow_scoped_channel(channel: Channel) -> WizardResult:
    """Wizard branch when the user picked a scoped channel.

    Each per-step question is still asked, but channel.defaults pre-select the
    Enter-to-accept option. The user can override any step by typing a number.
    """
    d = channel.defaults

    target_lang = _ask_choice(
        "Step 2 - Narration language",
        _LANGS, _index_of(_LANGS, d.get("lang"), 0),
    )

    source = _ask_choice(
        "Step 3 - Where does the topic come from?",
        [
            ("auto", "Auto AI ideate (Gemini, deduped vs this channel's history)"),
            ("manual", "Type a topic"),
            ("script", "Drop a .txt file (Tella will narrate it verbatim)"),
        ],
        default_index=0,
    )

    mode = "topic"
    topic = ""
    user_script: str | None = None
    topic_embedding: list[float] | None = None
    word_count = 0

    if source == "auto":
        topic, emb = _ideate_with_channel(channel, target_lang)
        if emb:
            topic_embedding = emb
    elif source == "manual":
        topic = _ask_required_text("  Topic: ")
    else:  # script
        print()
        print("  Drop a .txt file path here (or type one) and press Enter.")
        while True:
            raw = _ask_optional_text("  File: ")
            file_path = _looks_like_text_file(raw)
            if file_path:
                user_script = _read_story_file(file_path)
                if len(user_script) < 30:
                    print("  (file is empty after cleaning — try again)")
                    continue
                topic = os.path.splitext(os.path.basename(file_path))[0]
                mode = "script"
                word_count = len(user_script.split())
                print(f"  Loaded {os.path.basename(file_path)} (~{word_count} words).")
                break
            print("  (not a .txt/.md path — drop the file again or type a full path)")

    aspect_ratio = _ask_choice(
        "Step 4 - Aspect ratio",
        _ASPECTS, _index_of(_ASPECTS, d.get("aspect"), 0),
    )
    media_source = _ask_choice(
        "Step 5 - Where do the visuals come from?",
        _MEDIA, _index_of(_MEDIA, d.get("media"), 0),
    )
    style = None
    if media_source == "ai_image":
        style = _ask_choice(
            "Step 6 - AI image style",
            _STYLES, _index_of(_STYLES, d.get("style"), 0),
        )

    if mode == "script":
        # Script mode auto-scales duration from word count (matches ad-hoc flow).
        duration_mode = "detailed" if word_count > 220 else "short"
    else:
        duration_mode = _ask_choice(
            "Step 7 - How long?",
            _DURATIONS, _index_of(_DURATIONS, d.get("duration"), 0),
        )
    voice_gender = _ask_choice(
        "Step 8 - Narrator voice",
        _GENDERS, _index_of(_GENDERS, d.get("voice_gender"), 0),
    )

    theme = _resolve_theme(media_source, style)
    voice_pace_name = pace_name_for(user_script or topic, theme)

    print()
    print("-" * 60)
    print("  Ready to render:")
    print(f"    Channel   : {channel.name}  [scoped]"
          + ("  + avatar" if channel.avatar_path else ""))
    print(f"    Mode      : {mode}")
    print(f"    Topic     : {topic}")
    print(f"    Language  : {_LANG_NAME.get(target_lang, target_lang)}")
    print(f"    Aspect    : {aspect_ratio}")
    print(f"    Visuals   : {_label(_MEDIA, media_source)}"
          + (f" / {style}" if style else ""))
    print(f"    Length    : {_label(_DURATIONS, duration_mode)}")
    print(f"    Voice     : {_label(_GENDERS, voice_gender)} ({voice_pace_name})")
    print("-" * 60)
    _confirm()
    return WizardResult(
        mode=mode,
        target_lang=target_lang,
        aspect_ratio=aspect_ratio,
        media_source=media_source,
        duration_mode=duration_mode,
        voice_gender=voice_gender,
        theme=theme,
        voice_pace_name=voice_pace_name,
        channel_name=channel.name,
        channel_avatar=channel.avatar_path or "",
        channel_slug=channel.slug,
        topic=topic,
        user_script=user_script,
        topic_embedding=topic_embedding,
    )


def _flow_adhoc(channel_name: str, channel_avatar: str) -> WizardResult:
    """Original 7-step ad-hoc flow — used when no scoped channel is picked.

    ``channel_name`` may be empty (truly no channel) or a free-typed label.
    """
    print()
    print("Step 2 - Your story")
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
            print("  (that file looks empty after cleaning - switching to topic mode)")
            file_path = None

    if file_path:
        mode = "script"
        target_lang = detect_language(user_script)
        words = len(user_script.split())
        print(f"\n  Loaded story: {os.path.basename(file_path)}")
        print(f"  Detected language: {_LANG_NAME.get(target_lang, target_lang)}")
        print(f"  ~{words} words -> Tella will split it into scenes automatically.")
        duration_mode = "detailed" if words > 220 else "short"

        aspect_ratio = _ask_choice("Step 3 - Aspect ratio", _ASPECTS, 0)
        media_source = _ask_choice("Step 4 - Where do the visuals come from?", _MEDIA, 0)
        style = None
        if media_source == "ai_image":
            style = _ask_choice("Step 5 - AI image style", _STYLES, 0)
        voice_gender = _ask_choice("Step 6 - Narrator voice", _GENDERS, 0)

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
        print(f"    Channel   : {channel_name or '(none)'}"
              + ("  + avatar" if channel_avatar else ""))
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
            channel_name=channel_name,
            channel_avatar=channel_avatar,
            user_script=user_script,
            topic=os.path.splitext(os.path.basename(file_path))[0],
        )

    # Topic mode
    topic = raw
    target_lang = _ask_choice("Step 3 - Narration language", _LANGS, 0)
    aspect_ratio = _ask_choice("Step 4 - Aspect ratio", _ASPECTS, 0)
    media_source = _ask_choice("Step 5 - Where do the visuals come from?", _MEDIA, 0)
    style = None
    if media_source == "ai_image":
        style = _ask_choice("Step 6 - AI image style", _STYLES, 0)
    duration_mode = _ask_choice("Step 7 - How long?", _DURATIONS, 0)
    voice_gender = _ask_choice("Step 8 - Narrator voice", _GENDERS, 0)

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
    print(f"    Channel   : {channel_name or '(none)'}"
          + ("  + avatar" if channel_avatar else ""))
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
        channel_name=channel_name,
        channel_avatar=channel_avatar,
        topic=topic,
    )


def run_wizard() -> WizardResult:
    """Walk the user through every choice and return their selections."""
    _print_header()
    pick = _ask_channel_step1()

    if isinstance(pick, Channel) and pick.is_scoped:
        return _flow_scoped_channel(pick)

    # Saved-but-not-scoped channel → ad-hoc with the channel's name + avatar.
    if isinstance(pick, Channel):
        return _flow_adhoc(pick.name, pick.avatar_path or "")

    # User typed a new name → ad-hoc with that label.
    if isinstance(pick, tuple):
        name, avatar = pick
        return _flow_adhoc(name, avatar)

    # None — fully ad-hoc, no channel.
    return _flow_adhoc("", "")


__all__ = ["WizardResult", "run_wizard"]
