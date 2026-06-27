"""Tella CLI entry — full pipeline from topic to MP4 in one command.

Usage::

    python -m tella \
        --topic "the story of cinderella" \
        --lang en \
        --theme parable \
        --media ai_image \
        --duration short \
        --aspect 9:16 \
        --out ./out

Steps the CLI walks (each one logs progress):

  1. Translate the topic into ``target_lang`` (skip if source = target)
  2. Plan scene-by-scene with Gemini + apply character lock
  3. Fetch one media asset per scene (CF FLUX / Pexels Photo / Pexels Video)
  4. Synthesize Edge TTS narration for each scene
  5. Compose scene timing
  6. Render scene MP4s + concatenate → final video.mp4

The CLI is async-orchestrated so steps 3 + 4 run concurrently — typical
total wall time is dominated by the longest of {AI image gen, TTS} which
runs ~5-30 s for a short-mode 8-scene video.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    _REPO_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from tella._voice_pace import PRESETS, default_pace_for_theme, resolve_pace
from tella.composer.compose import compose_timing
from tella.ingest.topic_translator import SUPPORTED_LANGS, translate_topic
from tella.media.fetch import fetch_assets
from tella.planner.story_planner import plan_story, plan_story_from_script
from tella.render.pipeline import render
from tella.tts.synth_all import synthesize_all

logger = logging.getLogger("tella.cli")


def _slugify(text: str, max_len: int = 40) -> str:
    import re
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip("_").lower()).strip("_")
    return (slug or "tella")[:max_len]


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


async def run_pipeline(
    *,
    topic: str,
    target_lang: str,
    theme: str,
    media_source: str,
    duration_mode: str,
    aspect_ratio: str,
    voice_pace_name: str | None,
    voice_rate_custom: str | None,
    voice_gender: str | None,
    out_root: Path,
    job_id: str | None = None,
    google_tts_api_key: str = "",
    google_tts_voice: str = "",
    user_script: str | None = None,
) -> Path:
    """Execute the full Tella pipeline. Returns the path to the final MP4.

    Two input modes (CEO 2026-06-17):
      * ``user_script=None`` — TOPIC MODE (default): translate ``topic`` →
        Gemini writes story → scenes.
      * ``user_script=<str>`` — PASTE-SCRIPT MODE: skip translation, ask
        Gemini to PARSE the user's narration into scenes preserving
        wording verbatim. ``topic`` is used only for the job slug + title
        fallback (pass a short label or empty).
    """

    use_script = bool((user_script or "").strip())

    # ── 0. Setup output folder ─────────────────────────────────────────
    if not job_id:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug_seed = topic if topic else (user_script or "script")[:40]
        job_id = f"{ts}_{_slugify(slug_seed)}"
    job_dir = out_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    logger.info("job: %s (mode=%s)", job_dir, "script" if use_script else "topic")

    # ── 1. Translate topic (skipped in script mode — user's text is canonical) ──
    if use_script:
        logger.info("step 1/6 — skip topic translation (paste-script mode)")
        topic_in_target = (topic or "").strip()
    else:
        logger.info("step 1/6 — translate topic")
        tr = await translate_topic(topic, target_lang)
        topic_in_target = tr.translated_topic
        logger.info(
            "  source=%s, target=%s, needs_translation=%s",
            tr.source_language_detected, tr.target_language, tr.needs_translation,
        )
        logger.info("  → %r", topic_in_target)

    # ── 2. Plan story (topic mode) OR parse script ────────────────────
    logger.info("step 2/6 — %s (gemini)",
                "parse user script" if use_script else "plan story")
    pace = resolve_pace(
        theme=theme,
        override=voice_pace_name,
        custom_edge_rate=voice_rate_custom,
    )
    if use_script:
        plan = await plan_story_from_script(
            user_script=user_script.strip(),
            target_lang=target_lang,
            aspect_ratio=aspect_ratio,
            media_source=media_source,
            duration_mode=duration_mode,
            theme=theme,
            voice_pace=pace,
            voice_gender=voice_gender,
        )
    else:
        plan = await plan_story(
            topic=topic_in_target,
            target_lang=target_lang,
            aspect_ratio=aspect_ratio,
            media_source=media_source,
            duration_mode=duration_mode,
            theme=theme,
            voice_pace=pace,
            voice_gender=voice_gender,
        )
    plan_json = job_dir / "plan.json"
    plan_json.write_text(
        json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("  %d scenes, voice=%s @ %s", len(plan.scenes), plan.voice_name, plan.voice_edge_rate)

    # ── 3 + 4. Media + TTS in parallel ─────────────────────────────────
    logger.info("step 3/6 — fetch %d assets (%s)", len(plan.scenes), plan.media_source)
    logger.info("step 4/6 — synthesize edge TTS in parallel")
    await asyncio.gather(
        fetch_assets(plan, job_dir),
        synthesize_all(
            plan,
            job_dir,
            google_tts_api_key=google_tts_api_key,
            google_tts_voice=google_tts_voice,
        ),
    )

    # ── 5. Compose timing ──────────────────────────────────────────────
    logger.info("step 5/6 — compose timing")
    compose_timing(plan)

    # Re-write plan with timing populated for debugging.
    plan_json.write_text(
        json.dumps(plan.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 6. Render MP4 ──────────────────────────────────────────────────
    logger.info("step 6/6 — render (ffmpeg)")
    final = await render(plan, job_dir)
    logger.info("DONE — %s (%.2fs total)", final, plan.total_duration)
    return final


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tella",
        description="Tella — Người kể chuyện · creative storytelling video tool",
    )
    p.add_argument("--topic", required=True, help="Story topic (any language)")
    p.add_argument(
        "--lang", required=True, choices=list(SUPPORTED_LANGS),
        help="Target language (ISO-639-1)",
    )
    p.add_argument(
        "--theme", default="cinematic",
        choices=["parable", "cinematic", "playful", "mindfulness"],
    )
    p.add_argument(
        "--media", default="ai_image", dest="media_source",
        choices=["ai_image", "stock_photo", "stock_video"],
    )
    p.add_argument(
        "--duration", default="short", dest="duration_mode",
        choices=["short", "detailed"],
    )
    p.add_argument("--aspect", default="9:16", choices=["9:16", "16:9"])
    p.add_argument(
        "--pace", default=None, choices=list(PRESETS),
        dest="voice_pace_name",
        help="Voice pace preset (default = theme default)",
    )
    p.add_argument(
        "--voice-rate-custom", default=None, dest="voice_rate_custom",
        help='Custom Edge rate, e.g. "+3%%" or "-7%%" (overrides --pace)',
    )
    p.add_argument(
        "--gender", default=None, choices=["male", "female"],
        dest="voice_gender",
    )
    p.add_argument(
        "--out", default=None, dest="out_root",
        help="Output root dir (default ./out or $TELLA_OUTPUT_DIR)",
    )
    p.add_argument("--job-id", default=None, help="Override job folder name")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv

    # No flags at all → friendly interactive wizard (the RUN.bat experience).
    # Any flag present → classic argparse CLI (power users + automation).
    if not raw_argv:
        _setup_logging(verbose=False)
        if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GEMINI_API_KEYS"):
            print(
                "ERROR: GEMINI_API_KEY missing. Copy .env.example to .env and fill it in.",
                file=sys.stderr,
            )
            return 1
        from tella.wizard import run_wizard

        try:
            choice = run_wizard()
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            return 130

        out_root = Path(os.environ.get("TELLA_OUTPUT_DIR") or "./out")
        out_root.mkdir(parents=True, exist_ok=True)
        try:
            final = asyncio.run(
                run_pipeline(
                    topic=choice.topic,
                    target_lang=choice.target_lang,
                    theme=choice.theme,  # cinematic, or playful when cartoon style picked
                    media_source=choice.media_source,
                    duration_mode=choice.duration_mode,
                    aspect_ratio=choice.aspect_ratio,
                    voice_pace_name=choice.voice_pace_name,  # adapted to topic genre
                    voice_rate_custom=None,
                    voice_gender=choice.voice_gender,
                    out_root=out_root,
                    job_id=None,
                    user_script=choice.user_script,
                )
            )
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except Exception as exc:
            logger.exception("pipeline failed: %s", exc)
            return 1
        print(f"\n[OK] Final video: {final}")
        return 0

    parser = build_arg_parser()
    args = parser.parse_args(raw_argv)
    _setup_logging(args.verbose)

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GEMINI_API_KEYS"):
        print(
            "ERROR: GEMINI_API_KEY missing. Set it in .env (see .env.example).",
            file=sys.stderr,
        )
        return 1

    out_root = Path(args.out_root or os.environ.get("TELLA_OUTPUT_DIR") or "./out")
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        final = asyncio.run(
            run_pipeline(
                topic=args.topic,
                target_lang=args.lang,
                theme=args.theme,
                media_source=args.media_source,
                duration_mode=args.duration_mode,
                aspect_ratio=args.aspect,
                voice_pace_name=args.voice_pace_name,
                voice_rate_custom=args.voice_rate_custom,
                voice_gender=args.voice_gender,
                out_root=out_root,
                job_id=args.job_id,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("pipeline failed: %s", exc)
        return 1

    # ASCII-only print so Windows cmd cp1252 doesn't choke.
    print(f"\n[OK] Final video: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
