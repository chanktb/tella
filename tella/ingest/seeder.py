"""Topic ideation with embedding-based dedup against a channel's history.

The wizard's *Auto AI topic* option (only offered for scoped channels — see
:class:`tella.channels.Channel.is_scoped`) calls :func:`generate_topic`.

How it works:

1. Load up to ``history_window`` past entries from the channel's
   ``history.jsonl`` (one JSON per line: ``{"topic", "embedding", "ts"}``).
2. Ask Gemini Flash Lite to brainstorm ``batch_size`` fresh candidates from
   ``niche_guide`` + ``seed_examples`` + the last 30 used topics.
3. Embed every candidate in one batched call (gemini-embedding-001).
4. For each candidate, compute cosine vs every history embedding. Accept the
   first one whose max similarity stays under ``similarity_threshold``.
5. If every candidate fails the dedup, regenerate (up to ``max_attempts``).

The picked topic is **not** appended to history here — the CLI appends it
only after the render succeeds, so a failed render doesn't burn a topic.

Lifted from ktb-story-teller/vcm/seeder.py (same author, MIT-licensed Tella
code), adapted to Tella's shared Gemini client + multi-lang ideation prompt.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tella._gemini import get_client, is_transient_quota_error

logger = logging.getLogger("tella.ingest.seeder")

# Free-tier Gemini models. text-embedding-004 is NOT exposed on the free
# v1beta API (404 on every variant); gemini-embedding-001 works fine and
# has the same cosine semantics.
_IDEATION_MODEL = "gemini-flash-lite-latest"
_EMBED_MODEL = "gemini-embedding-001"

# Human-readable target_lang names — the ideation prompt uses these so
# Gemini reliably writes candidates in the right language.
_LANG_NAMES: dict[str, str] = {
    "vi": "Vietnamese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
}


@dataclass
class HistoryEntry:
    """One past topic with its precomputed embedding."""
    topic: str
    embedding: list[float]
    ts: int


def load_history(path: Path, window: int = 200) -> list[HistoryEntry]:
    """Read the last ``window`` entries from ``history.jsonl``.

    Missing file → empty list (first-run case). Malformed lines are
    skipped with a warning.
    """
    if not path.is_file():
        return []
    out: list[HistoryEntry] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(HistoryEntry(
                    topic=str(obj["topic"]),
                    embedding=list(obj["embedding"]),
                    ts=int(obj.get("ts", 0)),
                ))
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("skip malformed history line in %s: %s", path, exc)
    except OSError as exc:
        logger.warning("read history %s failed: %s", path, exc)
        return []
    return out[-window:]


def append_history(path: Path, topic: str, embedding: list[float]) -> None:
    """Append one entry to ``history.jsonl`` (parent dir created if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"topic": topic, "embedding": embedding, "ts": int(time.time())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b, strict=False))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


def max_similarity(
    candidate: list[float], history: Iterable[HistoryEntry]
) -> tuple[float, str]:
    """Returns ``(max_cosine, closest_topic_text)``. ``(0, "")`` if empty."""
    best = 0.0
    best_topic = ""
    for entry in history:
        s = _cosine(candidate, entry.embedding)
        if s > best:
            best = s
            best_topic = entry.topic
    return best, best_topic


def _build_ideation_prompt(
    *,
    niche_guide: str,
    seed_topics: list[str],
    recent_topics: list[str],
    batch_size: int,
    target_lang: str,
) -> str:
    """Build a multi-lang ideation prompt. Output language follows target_lang."""
    lang_name = _LANG_NAMES.get(target_lang, "English")
    seed_block = "\n".join(f"- {t}" for t in seed_topics) or "(none)"
    recent_block = "\n".join(f"- {t}" for t in recent_topics[-30:]) or "(none yet)"
    return f"""You are a viral video content strategist. The channel niche is:

{niche_guide}

Example anchor topics (tone + format reference — DO NOT copy verbatim):
{seed_block}

Topics ALREADY USED recently — DO NOT repeat or lightly rephrase any of these:
{recent_block}

Generate EXACTLY {batch_size} fresh topics that fit the niche. Each topic on
its own line, 5-15 words, framed as a curiosity-driving question or punchy
declarative. Each must differ substantially in subject — not just a word swap
of another candidate or a past topic.

Write every topic in {lang_name}.

Output: exactly {batch_size} lines, no numbering, no bullets, no commentary.
One topic per line."""


def _gemini_ideate(client, prompt: str, batch_size: int) -> list[str]:
    """Call Gemini Flash Lite → list of candidate topic strings."""
    resp = client.models.generate_content(
        model=_IDEATION_MODEL,
        contents=prompt,
    )
    text = (resp.text or "").strip()
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned: list[str] = []
    for ln in raw_lines:
        # Strip optional leading "- " / "* " / "• " bullets.
        if ln.startswith(("- ", "* ", "• ")):
            ln = ln[2:].strip()
        # Strip "1. " / "1) " numbering.
        if len(ln) > 2 and ln[0].isdigit() and ln[1] in ".)" and ln[2:3] == " ":
            ln = ln[3:].strip()
        if ln:
            cleaned.append(ln)
    return cleaned[:batch_size]


def _gemini_embed(client, texts: list[str]) -> list[list[float]]:
    """Batch-embed ``texts`` → list of vectors."""
    if not texts:
        return []
    resp = client.models.embed_content(
        model=_EMBED_MODEL,
        contents=texts,
    )
    out: list[list[float]] = []
    for emb in resp.embeddings:
        values = getattr(emb, "values", None)
        if values is None and isinstance(emb, dict):
            values = emb.get("values", [])
        out.append(list(values or []))
    return out


@dataclass
class TopicPick:
    """Result of one :func:`generate_topic` call."""
    topic: str
    embedding: list[float]
    max_history_similarity: float
    closest_history_topic: str
    attempt: int       # 1-based


def generate_topic(
    *,
    niche_guide: str,
    seed_topics: list[str],
    history: list[HistoryEntry],
    target_lang: str = "en",
    similarity_threshold: float = 0.85,
    batch_size: int = 5,
    max_attempts: int = 4,
    api_key: str | None = None,
) -> TopicPick:
    """Pick one fresh topic, deduped against ``history``.

    Args:
        niche_guide: One-paragraph description of the channel's niche.
        seed_topics: Example topics to anchor tone/format.
        history: Past topics + embeddings (from :func:`load_history`).
        target_lang: ISO-639-1 of the language Gemini should write in.
        similarity_threshold: Reject candidate if max cosine vs history
            exceeds this. 0.85 ≈ "looks like a paraphrase of a past topic".
        batch_size: Candidates requested per Gemini call.
        max_attempts: Re-request up to this many times if all candidates
            fail dedup.
        api_key: Override env-resolved key.

    Raises:
        RuntimeError: All ``max_attempts × batch_size`` candidates failed
            dedup. The channel needs fresher seeds or a wider niche guide.
    """
    client = get_client(api_key=api_key)
    recent_topics = [h.topic for h in history[-30:]]

    for attempt in range(1, max_attempts + 1):
        prompt = _build_ideation_prompt(
            niche_guide=niche_guide,
            seed_topics=seed_topics,
            recent_topics=recent_topics,
            batch_size=batch_size,
            target_lang=target_lang,
        )
        try:
            candidates = _gemini_ideate(client, prompt, batch_size)
        except Exception as exc:
            transient = is_transient_quota_error(exc)
            logger.warning(
                "generate_topic attempt %d ideate error %s%s: %s",
                attempt, type(exc).__name__,
                " (transient — rotating key)" if transient else "",
                exc,
            )
            if attempt < max_attempts and transient and api_key is None:
                client = get_client()         # picks another random key
                time.sleep(2 * attempt)
            continue
        if not candidates:
            logger.warning("Gemini returned no candidates on attempt %d", attempt)
            continue
        try:
            embeddings = _gemini_embed(client, candidates)
        except Exception as exc:
            transient = is_transient_quota_error(exc)
            logger.warning(
                "generate_topic attempt %d embed error %s%s: %s",
                attempt, type(exc).__name__,
                " (transient — rotating key)" if transient else "",
                exc,
            )
            if attempt < max_attempts and transient and api_key is None:
                client = get_client()
                time.sleep(2 * attempt)
            continue
        if len(embeddings) != len(candidates):
            logger.warning(
                "embedding count mismatch (%d topics, %d embeddings)",
                len(candidates), len(embeddings),
            )

        for topic, emb in zip(candidates, embeddings, strict=False):
            if not emb:
                continue
            sim, closest = max_similarity(emb, history)
            if sim < similarity_threshold:
                logger.info(
                    "topic picked: %r (attempt=%d, max_sim=%.3f vs %r)",
                    topic, attempt, sim, closest,
                )
                return TopicPick(
                    topic=topic,
                    embedding=emb,
                    max_history_similarity=sim,
                    closest_history_topic=closest,
                    attempt=attempt,
                )
            logger.debug(
                "rejected %r — sim=%.3f vs %r (>= %.2f)",
                topic, sim, closest, similarity_threshold,
            )

    raise RuntimeError(
        f"No fresh topic after {max_attempts} attempts x {batch_size} "
        f"candidates. Add more diverse seed_examples or widen niche_guide."
    )


__all__ = [
    "HistoryEntry",
    "TopicPick",
    "append_history",
    "generate_topic",
    "load_history",
    "max_similarity",
]
