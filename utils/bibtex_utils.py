# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Minimal BibTeX parser for the user-provided must-cite reference list."""

import re
from typing import Dict, List

_ENTRY_START = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.MULTILINE)
_SKIP_TYPES = {"comment", "preamble", "string"}


def _matching_brace(text: str, open_idx: int) -> int:
    """Index of the brace closing the one at open_idx, or -1."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_fields(body: str) -> Dict[str, str]:
    fields = {}
    i = 0
    while i < len(body):
        m = re.compile(r"\s*,?\s*(\w[\w-]*)\s*=\s*").match(body, i)
        if not m:
            break
        name = m.group(1).lower()
        i = m.end()
        if i >= len(body):
            break
        if body[i] == "{":
            end = _matching_brace(body, i)
            if end == -1:
                break
            value = body[i + 1 : end]
            i = end + 1
        elif body[i] == '"':
            end = body.find('"', i + 1)
            if end == -1:
                break
            value = body[i + 1 : end]
            i = end + 1
        else:
            m2 = re.compile(r"[^,\s}]+").match(body, i)
            value = m2.group(0) if m2 else ""
            i = m2.end() if m2 else i + 1
        fields[name] = " ".join(value.split())
    return fields


def strip_braces(value: str) -> str:
    return value.replace("{", "").replace("}", "").strip()


def parse_bibtex_entries(text: str) -> List[Dict]:
    """Parses BibTeX text into [{"type", "key", "fields", "raw"}, ...].

    Text outside entries (e.g. Markdown around the entries) is ignored.
    """
    entries = []
    for m in _ENTRY_START.finditer(text):
        entry_type = m.group(1).lower()
        if entry_type in _SKIP_TYPES:
            continue
        open_idx = text.index("{", m.start())
        close_idx = _matching_brace(text, open_idx)
        if close_idx == -1:
            continue
        body = text[m.end() : close_idx]
        entries.append(
            {
                "type": entry_type,
                "key": m.group(2),
                "fields": _parse_fields(body),
                "raw": text[m.start() : close_idx + 1].strip(),
            }
        )
    return entries


def bibtex_authors_to_names(author_field: str) -> List[str]:
    """'Last, First and Last2, First2 and others' -> ['First Last', ...]."""
    names = []
    for part in re.split(r"\s+and\s+", author_field or ""):
        part = strip_braces(part)
        if not part or part.lower() == "others":
            continue
        if "," in part:
            last, first = [p.strip() for p in part.split(",", 1)]
            part = f"{first} {last}".strip()
        names.append(part)
    return names


_CITE_RE = re.compile(r"\\[cC]ite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")


def find_undefined_citations(tex: str, bib_text: str) -> List[str]:
    """Citation keys used in `tex` that have no entry in `bib_text` (sorted)."""
    tex = re.sub(r"(?<!\\)%.*", "", tex)
    bib_keys = {e["key"] for e in parse_bibtex_entries(bib_text)}
    used = {k.strip() for group in _CITE_RE.findall(tex) for k in group.split(",") if k.strip()}
    return sorted(used - bib_keys)
