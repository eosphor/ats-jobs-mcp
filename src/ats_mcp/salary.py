"""Salary parsing: turn a raw salary string into normalized fields.

Conservative by design: return ``None`` for any component we cannot confidently
determine. An ambiguous string yields fewer populated fields, never a wrong one.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "₣": "CHF",
    "R$": "BRL",
}
_CURRENCY_CODES = re.compile(
    r"\b(USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|SEK|NOK|DKK|PLN|CZK|HUF|INR|BRL|MXN|SGD|HKD|ZAR)\b",
    re.I,
)
# Multilingual pay periods: EN / FR / DE / ES / NL / IT. Bare abbreviations are
# only matched in "per X" / "/X" context to avoid false positives (e.g. "an
# hour" must be "hour", not the French year "an").
_PERIODS = [
    (re.compile(
        r"\b(?:years?|annual(?:ly)?|annum|annuel(?:le)?s?|j[aä]hrlich(?:e|es|er)?|"
        r"anual(?:es)?|a[ñn]os?|jaar(?:lijks)?|annuale|annui|anno)\b|"
        r"(?:per|par|pro|/)\s*(?:annum|year|an)\b|\byr\b",
        re.I,
    ), "year"),
    (re.compile(
        r"\b(?:months?|monthly|mois|mensuel(?:le)?s?|monatlich(?:e|es|er)?|"
        r"mensual(?:es)?|maand(?:elijks)?|mes(?:e|i)?|mensil[ei])\b|"
        r"(?:per|par|pro|/)\s*(?:month|mois|mes|mese|maand)\b|(?:^|\s)/mo\b",
        re.I,
    ), "month"),
    (re.compile(
        r"\b(?:hours?|hourly|heures?|horaire|st[uü]ndlich(?:e|es|er)?|"
        r"horas?|uur|ore)\b|"
        r"(?:per|par|pro|/)\s*(?:hour|heure|hora|uur|stunde)\b|(?:^|\s)/hr\b",
        re.I,
    ), "hour"),
    (re.compile(
        r"\b(?:days?|daily|jours?|quotidien(?:ne)?s?|t[aä]glich(?:e|es|er)?|"
        r"d[ií]as?|diari[oa]s?|dag(?:elijks)?|giorn[oi])\b|"
        r"(?:per|par|pro|/)\s*(?:day|jour|tag|giorno|dia|diem)\b",
        re.I,
    ), "day"),
]
_NUMBER = re.compile(r"(?<![A-Za-z])(\d[\d,]*(?:\.\d+)?)\s*([kKmM]?)(?![A-Za-z])")


def _amount(match: re.Match) -> float:
    num = float(match.group(1).replace(",", ""))
    suffix = match.group(2).lower()
    if suffix == "k":
        num *= 1000
    elif suffix == "m":
        num *= 1_000_000
    return num


def parse_salary(raw: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """Parse a raw salary string into (min, max, currency, period).

    Returns ``None`` for any component we cannot confidently determine.
    """
    if not raw or not raw.strip():
        return (None, None, None, None)
    s = raw.strip()

    currency: Optional[str] = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in s:
            currency = code
            break
    if currency is None:
        m = _CURRENCY_CODES.search(s)
        if m:
            currency = m.group(1).upper()

    period: Optional[str] = None
    for pat, name in _PERIODS:
        if pat.search(s):
            period = name
            break

    amounts = [_amount(m) for m in _NUMBER.finditer(s)]
    if not amounts:
        return (None, None, currency, period)

    has_range = bool(re.search(r"[-–—]|\bto\b|\bthrough\b", s, re.I))
    if len(amounts) >= 2 and has_range:
        return (min(amounts[0], amounts[1]), max(amounts[0], amounts[1]), currency, period)
    return (amounts[0], None, currency, period)
