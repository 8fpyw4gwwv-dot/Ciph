"""Per-list column mappings (mappings/Zxxxx.yaml).

A mapping is the allowlist for its list: only the fields it names can leave the
server. Headers are matched by exact (normalized) aliases, never by guessing;
validate_mapping shows near misses so a human can add the right alias.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .german import normalize_header, normalize_pzn, parse_date, parse_expiry, parse_int, parse_number

FIELD_TYPES = {"text", "pzn", "number", "int", "percent", "date", "expiry"}


class MappingError(ValueError):
    pass


@dataclass
class FieldSpec:
    name: str
    aliases: list[str]
    type: str = "text"
    required: bool = False
    description: str = ""

    @property
    def normalized_aliases(self) -> set[str]:
        return {normalize_header(a) for a in self.aliases}

    def convert(self, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        t = self.type
        if t == "pzn":
            return normalize_pzn(value)
        if t in ("number", "percent"):
            return parse_number(value)
        if t == "int":
            return parse_int(value)
        if t == "date":
            return parse_date(value)
        if t == "expiry":
            return parse_expiry(value)
        s = str(value).strip()
        if isinstance(value, float) and value.is_integer():
            s = str(int(value))
        return s


@dataclass
class ListMapping:
    z_code: str
    title: str
    description: str = ""
    category: str = ""
    needs_period: bool = True
    key: str | None = None
    validated: bool = False
    validated_with: str | None = None
    gui_notes: str = ""
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    path: Path | None = None

    def summary(self) -> dict:
        return {
            "z_code": self.z_code,
            "title": self.title,
            "description": self.description,
            "needs_period": self.needs_period,
            "mapping_validated": self.validated,
            "fields": list(self.fields),
        }


def load_mapping_file(path: Path) -> ListMapping:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    z = str(data.get("z_code", "")).upper()
    if not z:
        raise MappingError(f"{path.name}: z_code fehlt.")
    fields: dict[str, FieldSpec] = {}
    for name, spec in (data.get("columns") or {}).items():
        spec = spec or {}
        aliases = [str(a) for a in spec.get("aliases") or []]
        if not aliases:
            raise MappingError(f"{path.name}: Feld {name} hat keine aliases.")
        ftype = spec.get("type", "text")
        if ftype not in FIELD_TYPES:
            raise MappingError(f"{path.name}: Feld {name} hat unbekannten Typ {ftype!r}.")
        fields[name] = FieldSpec(name=name, aliases=aliases, type=ftype,
                                 required=bool(spec.get("required", False)),
                                 description=str(spec.get("description", "")))
    key = data.get("key")
    if key and key not in fields:
        raise MappingError(f"{path.name}: key {key!r} ist kein Feld.")
    return ListMapping(
        z_code=z, title=str(data.get("title", z)), description=str(data.get("description", "")),
        category=str(data.get("category", "")), needs_period=bool(data.get("needs_period", True)),
        key=key, validated=bool(data.get("validated", False)),
        validated_with=data.get("validated_with"), gui_notes=str(data.get("gui_notes") or ""),
        fields=fields, path=path,
    )


def load_mappings(directory: Path) -> dict[str, ListMapping]:
    out: dict[str, ListMapping] = {}
    if not directory.exists():
        return out
    for p in sorted(directory.glob("Z*.yaml")):
        m = load_mapping_file(p)
        out[m.z_code] = m
    return out


@dataclass
class HeaderMatch:
    columns: dict[str, int]                 # field name -> column index
    headers: list[str]                      # raw header texts
    unmatched: list[str]                    # headers not in the mapping
    missing_required: list[str]
    missing_optional: list[str]
    suggestions: dict[str, list[str]]       # unmatched header -> similar aliases (field: alias)
    duplicates: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.columns)


def match_headers(headers: list[Any], mapping: ListMapping) -> HeaderMatch:
    texts = ["" if h is None else str(h).strip() for h in headers]
    norm = [normalize_header(t) for t in texts]
    columns: dict[str, int] = {}
    duplicates: list[str] = []
    used: set[int] = set()
    for fname, spec in mapping.fields.items():
        aliases = [normalize_header(a) for a in spec.aliases]
        for alias in aliases:  # alias order = priority
            hits = [i for i, n in enumerate(norm) if n and n == alias and i not in used]
            if hits:
                columns[fname] = hits[0]
                used.add(hits[0])
                if len(hits) > 1:
                    duplicates.append(texts[hits[0]])
                break
    unmatched = [texts[i] for i in range(len(texts)) if texts[i] and i not in used]
    all_aliases = {normalize_header(a): f"{f}: {a}" for f, s in mapping.fields.items() for a in s.aliases}
    suggestions = {}
    for u in unmatched:
        close = difflib.get_close_matches(normalize_header(u), list(all_aliases), n=2, cutoff=0.75)
        if close:
            suggestions[u] = [all_aliases[c] for c in close]
    missing_required = [f for f, s in mapping.fields.items() if s.required and f not in columns]
    missing_optional = [f for f, s in mapping.fields.items() if not s.required and f not in columns]
    return HeaderMatch(columns, texts, unmatched, missing_required, missing_optional, suggestions,
                       duplicates)


def proposal_yaml(mapping: ListMapping, match: HeaderMatch, source_file: str) -> str:
    """A mapping file reflecting what was found in a real export, for a human to approve."""
    cols: dict[str, Any] = {}
    for fname, spec in mapping.fields.items():
        aliases = list(spec.aliases)
        if fname in match.columns:
            found = match.headers[match.columns[fname]]
            if found not in aliases:
                aliases.insert(0, found)
        else:
            for header, sugg in match.suggestions.items():
                if any(s.startswith(f"{fname}: ") for s in sugg) and header not in aliases:
                    aliases.insert(0, header)
        entry: dict[str, Any] = {"aliases": aliases, "type": spec.type}
        if spec.required:
            entry["required"] = True
        if spec.description:
            entry["description"] = spec.description
        cols[fname] = entry
    doc = {
        "z_code": mapping.z_code,
        "title": mapping.title,
        "description": mapping.description,
        "category": mapping.category,
        "needs_period": mapping.needs_period,
        "key": mapping.key,
        "gui_notes": mapping.gui_notes,
        "validated": not match.missing_required,
        "validated_with": source_file,
        "columns": cols,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)
