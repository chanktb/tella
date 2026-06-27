"""Pick a narration pace from the topic's genre.

Different stories want different reading speeds: a children's tale reads
slowly and warmly; a science explainer reads at a clear, steady clip. The
wizard does not ask the user for pace — we infer it from the topic (or, in
paste-script mode, the story text) with a light keyword heuristic over
Vietnamese + English, returning one of the ``slow / medium / fast`` presets
defined in :mod:`tella._voice_pace`.

Cartoon style (theme == "playful") always means kid content, so it pins to
the slow, warm storytelling pace regardless of keywords.
"""
from __future__ import annotations

import re

# Children's stories / fables → slow, warm storytelling. Vietnamese entries
# keep diacritics so they don't false-match unrelated words (e.g. "cao" =
# tall vs "cáo" = fox).
_KIDS = (
    # story framing
    "truyện", "câu chuyện", "kể chuyện", "cổ tích", "ngụ ngôn", "thiếu nhi",
    "em bé", "trẻ em", "công chúa", "hoàng tử", "cô bé", "cậu bé",
    "cau chuyen", "co tich", "thieu nhi",
    # animal protagonists (fable signal)
    "thỏ", "rùa", "mèo", "cáo", "gấu", "sói", "cừu", "khỉ",
    # english
    "bedtime", "fairy", "fable", "kids", "children", "once upon", "fox",
    "rabbit", "bunny", "tortoise", "hare", "dragon", "prince", "princess",
)

# Educational / science / factual → clear, steady medium pace.
_SCIENCE = (
    "khoa học", "khoa hoc", "vũ trụ", "vu tru", "vật lý", "vat ly", "hóa học",
    "hoa hoc", "sinh học", "sinh hoc", "tại sao", "tai sao", "vì sao", "vi sao",
    "như thế nào", "nhu the nao", "lịch sử", "lich su", "công nghệ", "cong nghe",
    "science", "physics", "chemistry", "biology", "history", "technology",
    "how ", "why ", "explain", "guide", "tutorial",
)

# Meditative / spiritual → slow, unhurried.
_CALM = (
    "thiền", "thien", "phật", "phat", "tâm linh", "tam linh", "chánh niệm",
    "chanh niem", "mindful", "meditation", "dharma", "buddh",
)


def _count_hits(text: str, needles: tuple[str, ...]) -> int:
    return sum(text.count(n) for n in needles)


def pace_name_for(text: str, theme: str = "cinematic") -> str:
    """Return a pace preset name (``slow`` / ``medium`` / ``fast``).

    ``theme == "playful"`` (the cartoon style) always returns ``slow``.
    """
    if theme == "playful":
        return "slow"

    sample = re.sub(r"\s+", " ", (text or "").lower())[:1500]
    kids = _count_hits(sample, _KIDS)
    calm = _count_hits(sample, _CALM)
    science = _count_hits(sample, _SCIENCE)

    if calm and calm >= kids and calm >= science:
        return "slow"
    if kids and kids >= science:
        return "slow"
    if science:
        return "medium"
    return "medium"


__all__ = ["pace_name_for"]
