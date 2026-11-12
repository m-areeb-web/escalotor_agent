"""Cleaning incoming email text before the agent reads it.

Replies usually contain the whole earlier conversation underneath ("On Tue ... wrote:",
Outlook's "From: ... Sent: ..." block, or lines starting with ">"). If we kept that, the agent
could pick up an old ticket number or treat old text as the new update. We keep only the
new part of the message.
"""

from __future__ import annotations

import re

# Each pattern marks the START of quoted history; everything from there on is dropped.
_QUOTE_STARTS = [
    # Gmail / Apple Mail: "On Tue, 11 Nov 2026 at 15:02, Name <a@b.com> wrote:" (may wrap onto 2 lines)
    re.compile(r"^[ \t]*On\b[^\n]{0,250}(?:\n[^\n]{0,250})?\bwrote:[ \t]*$", re.MULTILINE | re.IGNORECASE),
    # Outlook: "From: Name <a@b.com>" followed within a few lines by "Sent:" or "Date:"
    re.compile(r"^[ \t]*\*?From:\*?[^\n]*\n(?:[^\n]*\n){0,3}?[ \t]*\*?(?:Sent|Date):", re.MULTILINE | re.IGNORECASE),
    # "-----Original Message-----"
    re.compile(r"^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}", re.MULTILINE | re.IGNORECASE),
    # Outlook plain-text separator line
    re.compile(r"^[ \t]*_{20,}[ \t]*$", re.MULTILINE),
    # Standard signature separator "-- "
    re.compile(r"^-- ?$", re.MULTILINE),
]


def clean_body(text: str) -> str:
    """Return only the new part of an email body (no quoted history, no '>' lines)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = len(text)
    for pattern in _QUOTE_STARTS:
        m = pattern.search(text)
        if m and m.start() < cut:
            cut = m.start()
    new = "\n".join(line for line in text[:cut].split("\n") if not line.lstrip().startswith(">"))
    new = re.sub(r"\n{3,}", "\n\n", new).strip()
    # If cleaning removed everything (e.g. a bare forward), fall back to the original text.
    return new or text.strip()
