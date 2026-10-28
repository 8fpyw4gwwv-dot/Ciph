"""Data protection: what may leave the server.

Two layers, both applied before anything reaches the model:
1. Allowlist - only columns named in the list's mapping are kept.
2. Denylist - any header (raw or field name) matching a personal-data pattern is
   dropped even if the mapping names it.
Text values are also scrubbed of e-mail addresses and IBANs.
Only column names are ever recorded, never values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")
SCRUBBED = "[entfernt]"


@dataclass
class RedactionReport:
    personal_columns: list[str] = field(default_factory=list)   # dropped by the denylist
    unmapped_columns: list[str] = field(default_factory=list)   # dropped by the allowlist
    scrubbed_values: int = 0                                    # e-mails / IBANs replaced

    def as_dict(self) -> dict:
        return {
            "personal_columns_removed": self.personal_columns,
            "unmapped_columns_removed": self.unmapped_columns,
            "scrubbed_values": self.scrubbed_values,
        }

    def merge(self, other: "RedactionReport") -> None:
        for c in other.personal_columns:
            if c not in self.personal_columns:
                self.personal_columns.append(c)
        for c in other.unmapped_columns:
            if c not in self.unmapped_columns:
                self.unmapped_columns.append(c)
        self.scrubbed_values += other.scrubbed_values


class Redactor:
    def __init__(self, patterns: list[str]):
        # Patterns match at a word start: "Name" hits "Name", "Kunden-Name" and
        # "Name Kunde", but not "Artikelname"-style compounds unless listed.
        self._compiled = [re.compile(r"(?<![^\W\d_])" + p, re.IGNORECASE) for p in patterns]
        # Compounds where the word sits inside, e.g. "Kundenname", "Patientennr".
        self._compact = [re.compile(p, re.IGNORECASE) for p in patterns
                         if p in ("Kunde", "Patient", "Versicherten", "Arzt", "Vorname", "Telefon",
                                  "Adresse", "Straße", "Strasse", "Mail")]

    def is_personal(self, header: str) -> bool:
        h = str(header or "")
        if not h.strip():
            return False
        if any(rx.search(h) for rx in self._compiled):
            return True
        return any(rx.search(h) for rx in self._compact)

    def scrub(self, value: Any) -> tuple[Any, bool]:
        if not isinstance(value, str) or not value:
            return value, False
        new = _EMAIL.sub(SCRUBBED, value)
        new = _IBAN.sub(SCRUBBED, new)
        return new, new != value
