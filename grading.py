"""Answer checking shared by /submit, adaptive routing and the review page."""
import re
from fractions import Fraction

_NUM_RE = re.compile(r"^-?\d*\.?\d+$")
_FRAC_RE = re.compile(r"^-?\d*\.?\d+/\d*\.?\d+$")


def _clean(value):
    return (str(value or "").strip().upper()
            .replace(" ", "").replace("−", "-").replace("–", "-"))


def _to_number(text):
    try:
        if _NUM_RE.match(text):
            return Fraction(text)
        if _FRAC_RE.match(text):
            num, den = text.split("/")
            den = Fraction(den)
            return None if den == 0 else Fraction(num) / den
    except (ValueError, ZeroDivisionError):
        return None
    return None


def answers_match(selected, correct):
    """True if a student's answer matches the answer key.

    * Multiple-choice: letters compared case-insensitively ("b" == "B").
    * Typed (free-response) answers: the key may list several accepted answers separated
      by "|" (e.g. "4|5"). Numbers match by value, so "3/4", ".75" and "0.75" are equal,
      and an answer with 3+ decimal places within 0.001 counts (2/3 -> .667 or .6666).
    """
    s = _clean(selected)
    if not s or correct is None:
        return False
    s_num = _to_number(s)
    decimals = len(s.split(".", 1)[1]) if "." in s else 0
    for option in str(correct).split("|"):
        c = _clean(option)
        if not c:
            continue
        if s == c:
            return True
        c_num = _to_number(c)
        if s_num is not None and c_num is not None:
            if s_num == c_num:
                return True
            if decimals >= 3 and abs(s_num - c_num) < Fraction(1, 1000):
                return True
    return False
