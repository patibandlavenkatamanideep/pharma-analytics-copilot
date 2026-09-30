"""Reading a reply to a clarification.

"Riverside Clinic is the name of 2 different accounts -- which one did you
mean?" is only useful if "the second one" can be resolved against what was
actually shown. That needs the choices to be stored (state.py) and a reply to
be read against them, here.

Conservative by design. After a clarification the user may simply ask
something else, and "what about the top 2 accounts?" contains a 2 without
choosing anything. A reply counts as a choice only when it is SHORT and made
of choice language -- an ordinal, a number, an id as shown, or "the one in
<place>" naming exactly one option. Anything else is a new question, and the
pending clarification is superseded rather than guessed at.
"""

from __future__ import annotations

import re

_ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
    "sixth": 5, "6th": 5, "six": 5,
    "seventh": 6, "7th": 6, "seven": 6,
    "eighth": 7, "8th": 7, "eight": 7,
}

_ORDINAL_REPLY = re.compile(
    r"^(?:the\s+)?(?:(?P<word>first|second|third|fourth|fifth|sixth|seventh|eighth|"
    r"1st|2nd|3rd|4th|5th|6th|7th|8th|last)"
    r"|(?:option|number|no\.?|choice|#)\s*(?P<num>\d{1,2})"
    r"|(?P<bare>\d{1,2}))"
    r"(?:\s+(?:one|option|choice))?(?:\s*,?\s*please)?[.!]?$", re.I)

_PLACE_REPLY = re.compile(
    r"^(?:the\s+)?(?:one\s+)?(?:in|from|at)\s+(?P<place>[a-z][a-z .-]{1,30}?)[.!]?$", re.I)
_ADJECTIVE_REPLY = re.compile(r"^(?:the\s+)?(?P<place>[a-z][a-z .-]{1,30}?)\s+one[.!]?$", re.I)

_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def choice_from_reply(reply: str, choices: list[dict[str, str]]) -> int | None:
    """The index of the choice the reply selects, or None if it selects none.

    None means "treat this as a new question" -- never "pick the closest".
    """
    text = " ".join((reply or "").split()).strip()
    if not text or not choices or len(text.split()) > 6:
        return None

    # An id exactly as shown.
    for i, c in enumerate(choices):
        if text.upper() == str(c.get("id", "")).upper():
            return i

    if m := _ORDINAL_REPLY.match(text):
        if m.group("word"):
            word = m.group("word").lower()
            index = len(choices) - 1 if word == "last" else _ORDINALS[word]
        else:
            index = int(m.group("num") or m.group("bare")) - 1
        return index if 0 <= index < len(choices) else None

    for pattern in (_PLACE_REPLY, _ADJECTIVE_REPLY):
        if m := pattern.match(text):
            place = m.group("place").strip().lower()
            code = _STATE_CODES.get(place, place.upper() if len(place) == 2 else None)
            if not code:
                return None
            hits = [i for i, c in enumerate(choices)
                    if re.search(rf"\b{re.escape(code)}\b", str(c.get("detail", "")))]
            # Exactly one, or it did not disambiguate anything.
            return hits[0] if len(hits) == 1 else None
    return None
