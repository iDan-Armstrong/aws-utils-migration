"""Find columns that look like personal or controlled data before anything moves.

Checks both column names and a sample of values. A finding is a prompt for a person to
decide the marking; it never blocks a run by itself.
"""
from __future__ import annotations

import re

NAME_RULES = [
    ("ssn", re.compile(r"(^|_)(ssn|social_?sec|tin|taxpayer)", re.I), "Social Security or taxpayer number"),
    ("dob", re.compile(r"(^|_)(dob|birth_?date|date_?of_?birth)", re.I), "date of birth"),
    ("email", re.compile(r"e_?mail", re.I), "email address"),
    ("phone", re.compile(r"(phone|mobile|cell_?no|fax)", re.I), "phone number"),
    ("account", re.compile(r"(acct|account|routing|iban|card_?no|ccn)", re.I), "financial account number"),
    ("person", re.compile(r"(^|_)(owner|lessor|grantor|grantee|first_?name|last_?name|full_?name)($|_)", re.I), "person or company name"),
    ("address", re.compile(r"(mail_?addr|street|address|addr_?line)", re.I), "mailing or street address"),
    ("license", re.compile(r"(driver|dl_?no|passport)", re.I), "license or passport number"),
]
VALUE_RULES = [
    ("ssn", re.compile(r"^\d{3}-\d{2}-\d{4}$"), "values shaped like Social Security numbers"),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I), "values shaped like email addresses"),
    ("phone", re.compile(r"^\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}$"), "values shaped like phone numbers"),
    ("card", re.compile(r"^\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}$"), "values shaped like card numbers"),
]


def scan(columns: list[str], samples: dict[str, list] | None = None) -> list[str]:
    """Return human-readable findings such as "owner: person or company name (column name)"."""
    out = []
    for c in columns:
        for _, rx, label in NAME_RULES:
            if rx.search(c):
                out.append(f"{c}: {label} (column name)")
                break
        vals = [str(v) for v in (samples or {}).get(c, []) if v not in (None, "")][:200]
        if not vals:
            continue
        for _, rx, label in VALUE_RULES:
            hits = sum(1 for v in vals if rx.match(v.strip()))
            if hits and hits >= max(2, len(vals) // 5):
                out.append(f"{c}: {label} ({hits} of {len(vals)} sampled)")
                break
    return out
