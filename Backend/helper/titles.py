"""
Title helpers: caption cleaning and env-driven collection matching.

This module must stay free of imports from the rest of the Backend package
(except the logger) so that config.py can import it without circular imports.
"""
import re
from dataclasses import dataclass, field
from typing import List, Pattern

from Backend.logger import LOGGER

# --------------------------------------------------------------------------
# Title cleaning  (ONLY @usernames and links are removed, nothing else)
# --------------------------------------------------------------------------

# http(s)://..., ftp://..., www....  and  t.me/... / telegram.me/... style links
_URL_RE = re.compile(
    r"(?:(?:https?|ftp)://|www\.|(?:t|telegram)\.me/)\S+",
    re.IGNORECASE,
)
# @username (not an e-mail address, so the char before '@' must not be alnum)
_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9])@\w+")
# brackets left empty after removal:  [ ]  ( )  { }
_EMPTY_BRACKETS_RE = re.compile(r"\[\s*\]|\(\s*\)|\{\s*\}")


def clean_title(text: str) -> str:
    """
    Remove @usernames and links from a caption. Everything else is kept
    exactly as it is, including line breaks.
    """
    if not text:
        return ""
    text = _URL_RE.sub("", text)
    text = _USERNAME_RE.sub("", text)
    text = _EMPTY_BRACKETS_RE.sub("", text)

    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip(" \t-|_")
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    """lowercase and turn every run of non-word chars/underscores into one space."""
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


@dataclass
class Collection:
    name: str
    phrases: List[str] = field(default_factory=list)      # normalized phrases
    regexes: List[Pattern] = field(default_factory=list)  # compiled re: rules

    def matches(self, title: str, norm_title: str) -> bool:
        padded = f" {norm_title} "
        for phrase in self.phrases:
            # whole phrase must be present as full word(s)
            if f" {phrase} " in padded:
                return True
        for rx in self.regexes:
            if rx.search(title):
                return True
        return False


def parse_collections(raw: str) -> List[Collection]:
    """
    Parse the COLLECTIONS env value.

        COLLECTIONS='movie: Jawan 2024 | new latest: 2026 | tv: re:S\\d+E\\d+'

    * rules are separated by  |
    * "name : keyword1, keyword2"  -> name before the first ':', keywords after
    * a keyword is a phrase (case-insensitive, full-phrase match; dots, dashes
      and underscores count as spaces) or a regex when it starts with  re:
    * keywords are OR-ed together
    """
    collections: List[Collection] = []
    if not raw or not raw.strip():
        return collections

    for rule in raw.split("|"):
        rule = rule.strip()
        if not rule:
            continue
        if ":" not in rule:
            LOGGER.warning(f"COLLECTIONS: skipping rule without ':' -> {rule!r}")
            continue

        name, _, keywords = rule.partition(":")
        name = name.strip()
        if not name:
            LOGGER.warning(f"COLLECTIONS: skipping rule with empty name -> {rule!r}")
            continue

        col = Collection(name=name)
        for kw in keywords.split(","):
            kw = kw.strip()
            if not kw:
                continue
            if kw.lower().startswith("re:"):
                pattern = kw[3:].strip()
                try:
                    col.regexes.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    LOGGER.warning(f"COLLECTIONS: bad regex {pattern!r} in '{name}': {e}")
            else:
                phrase = _norm(kw)
                if phrase:
                    col.phrases.append(phrase)

        if not col.phrases and not col.regexes:
            LOGGER.warning(f"COLLECTIONS: '{name}' has no usable keywords, skipped")
            continue

        # same collection name written twice -> merge the rules
        existing = next((c for c in collections if c.name.lower() == name.lower()), None)
        if existing:
            existing.phrases.extend(col.phrases)
            existing.regexes.extend(col.regexes)
        else:
            collections.append(col)

    return collections


def match_collections(title: str, collections: List[Collection], default: str) -> List[str]:
    """
    Return the names of ALL collections whose rules match the title.
    If none match, return [default].
    """
    norm_title = _norm(title or "")
    found = [c.name for c in collections if c.matches(title or "", norm_title)]
    return found or [default]
