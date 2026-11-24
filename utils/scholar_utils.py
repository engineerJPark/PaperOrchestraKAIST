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

import warnings
import os
import requests
import time
from typing import List, Dict, Union, Optional
from thefuzz import fuzz  # type: ignore
import datetime
import re
import threading
import unicodedata
import xml.etree.ElementTree as ET

from dotenv import load_dotenv  # type: ignore

cur_dir = os.path.dirname(os.path.realpath(__file__))
load_dotenv(os.path.join(cur_dir, "../.env"))
S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
print(f"Semantic Scholar API key: {'set' if S2_API_KEY else 'not set (keyless, heavily rate-limited)'}")


# --- BIBTEX TEXT CLEANUP ---

# Characters seen in scholarly metadata that pdflatex (utf8 inputenc + T1) rejects.
_LATEX_CHAR_MAP = {
    "\u0229": "\u0119",  # e with cedilla: OpenAlex mis-encoding of e with ogonek (Jastrzebski)
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2212": "-",  # minus sign
    "\u00a0": " ",  # no-break space
    "\u2009": " ",  # thin space
    "\u200b": "",  # zero-width space
}
# Ranges pdflatex handles with utf8 inputenc + T1: ASCII, Latin-1, Latin Extended-A,
# plus common typographic punctuation.
_LATEX_SAFE_EXTRA = set("\u2013\u2014\u2018\u2019\u201c\u201d\u2026\u00ae\u00a9\u2122")


def latex_safe_text(text: Optional[str]) -> Optional[str]:
    """Makes metadata text (names, titles, venues) safe for pdflatex.

    Known problem characters are mapped explicitly; anything else outside the
    supported ranges is reduced to its base letter (e.g. accents are dropped).
    """
    if not text:
        return text
    out = []
    for ch in text:
        ch = _LATEX_CHAR_MAP.get(ch, ch)
        if not ch or ord(ch) <= 0x17F or ch in _LATEX_SAFE_EXTRA:
            out.append(ch)
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
        )
        out.append(base if base and all(ord(c) <= 0x17F for c in base) else "")
    return "".join(out)


def latex_escape_bib_field(text: Optional[str]) -> Optional[str]:
    """Escapes LaTeX special characters in metadata from search APIs
    (e.g. "Accident Analysis & Prevention"). Already-escaped ones are kept."""
    if not text:
        return text
    return re.sub(r"(?<!\\)([&%#_])", r"\\\1", latex_safe_text(text))


# --- SEMANTIC SCHOLAR UTILS ---
#
# Lookups go to Semantic Scholar first. If it fails (HTTP 429 rate limit, other
# HTTP errors, network errors), the same title is looked up on OpenAlex, then on
# arXiv. Fallback results are converted to the Semantic Scholar response format,
# so callers do not need to know which source answered.

S2_FIELDS = "title,authors,venue,year,abstract,citationCount,journal,publicationDate"
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY")
ARXIV_MIN_INTERVAL_SEC = 3.0  # arXiv API terms: at most one request every 3 seconds

_arxiv_lock = threading.Lock()
_arxiv_last_call = 0.0


def _is_date_valid(p_date, p_year, cutoff_str):
    if not cutoff_str:
        return True
    try:
        c_year, c_month = map(int, cutoff_str.split("-"))
        cutoff_dt = datetime.datetime(c_year, c_month, 1)
        if p_date:
            try:
                p_dt = datetime.datetime.strptime(p_date, "%Y-%m-%d")
                return p_dt < cutoff_dt
            except ValueError:
                pass
        if p_year:
            if p_year < c_year:
                return True
            if p_year == c_year and c_month > 1:
                return True
            return False
        return True
    except Exception:
        return True


def _select_best_match(
    results: List[Dict], title_query: str, year_hint: int, cutoff_date: str
) -> Optional[Dict]:
    best_match = None
    highest_ratio = 0

    for r in results:
        if not r.get("title"):
            continue
        if not _is_date_valid(r.get("publicationDate"), r.get("year"), cutoff_date):
            continue

        ratio = fuzz.ratio(title_query.lower(), r["title"].lower())
        if year_hint and r.get("year") == year_hint:
            ratio += 10

        if ratio > highest_ratio:
            highest_ratio = ratio
            best_match = r

    if highest_ratio > 70:
        return best_match
    return None


def _fetch_s2_candidates(title_query: str) -> Optional[List[Dict]]:
    """Returns S2 search results, or None if the request failed."""
    headers = {"X-API-KEY": S2_API_KEY} if S2_API_KEY else {}
    # Keyless access shares one global rate limit pool and often answers 429.
    # Retry once; if it still fails, the caller falls back to other sources.
    max_attempts = 2
    for attempt in range(max_attempts):
        rsp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            headers=headers,
            params={"query": title_query, "limit": 3, "fields": S2_FIELDS},
            timeout=5,
        )
        if rsp.status_code != 429 or attempt + 1 == max_attempts:
            break
        time.sleep(2)
    if rsp.status_code != 200:
        print(f"      ⚠️ S2 HTTP {rsp.status_code} for '{title_query}', trying fallback sources")
        return None
    return rsp.json().get("data", []) or []


def _openalex_abstract(inverted_index: Optional[Dict]) -> str:
    if not inverted_index:
        return ""
    positions = [(pos, word) for word, poss in inverted_index.items() for pos in poss]
    return " ".join(word for _, word in sorted(positions))


def _clean_openalex_venue(name: str) -> str:
    """OpenAlex often repeats the venue name ("X/Proceedings - X/Proceedings")."""
    parts = [p.strip() for p in name.split(" - ")]
    if len(parts) == 2 and parts[0] == parts[1]:
        name = parts[0]
    return name.removesuffix("/Proceedings").strip()


def _fetch_openalex_candidates(title_query: str) -> Optional[List[Dict]]:
    """Searches OpenAlex and converts results to the S2 format. None on failure."""
    params = {
        "search": title_query,
        "per-page": 3,
        "select": "display_name,publication_year,publication_date,cited_by_count,"
        "authorships,primary_location,biblio,abstract_inverted_index",
    }
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    rsp = requests.get("https://api.openalex.org/works", params=params, timeout=10)
    if rsp.status_code != 200:
        print(f"      ⚠️ OpenAlex HTTP {rsp.status_code} for '{title_query}'")
        return None

    results = []
    for w in rsp.json().get("results", []):
        source = (w.get("primary_location") or {}).get("source") or {}
        biblio = w.get("biblio") or {}
        first_page, last_page = biblio.get("first_page"), biblio.get("last_page")
        pages = None
        if first_page and last_page:
            pages = f"{first_page}--{last_page}"
        elif first_page:
            pages = first_page
        venue = _clean_openalex_venue(source.get("display_name") or "")
        # Conference papers have no journal name, so they become @inproceedings.
        is_journal = source.get("type") == "journal"
        results.append(
            {
                "title": w.get("display_name"),
                "authors": [
                    {"name": (a.get("author") or {}).get("display_name", "")}
                    for a in w.get("authorships", [])
                    if (a.get("author") or {}).get("display_name")
                ],
                "venue": venue,
                "year": w.get("publication_year"),
                "abstract": _openalex_abstract(w.get("abstract_inverted_index")),
                "citationCount": w.get("cited_by_count") or 0,
                "journal": {
                    "name": (venue or None) if is_journal else None,
                    "volume": biblio.get("volume"),
                    "pages": pages,
                },
                "publicationDate": w.get("publication_date"),
            }
        )
    return results


def _fetch_arxiv_candidates(title_query: str) -> Optional[List[Dict]]:
    """Searches arXiv by title and converts results to the S2 format. None on failure."""
    global _arxiv_last_call
    # Quotes are not allowed inside the phrase query; keep words only.
    clean_title = re.sub(r"[^\w\s-]", " ", title_query)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    if not clean_title:
        return []

    with _arxiv_lock:
        wait = ARXIV_MIN_INTERVAL_SEC - (time.time() - _arxiv_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            rsp = requests.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f'ti:"{clean_title}"', "max_results": 3},
                timeout=15,
            )
        finally:
            _arxiv_last_call = time.time()
    if rsp.status_code != 200:
        print(f"      ⚠️ arXiv HTTP {rsp.status_code} for '{title_query}'")
        return None

    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(rsp.text)
    results = []
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
        published = (entry.findtext("atom:published", "", ns) or "")[:10]
        journal_ref = entry.findtext("arxiv:journal_ref", None, ns)
        results.append(
            {
                "title": title,
                "authors": [
                    {"name": a.findtext("atom:name", "", ns)}
                    for a in entry.findall("atom:author", ns)
                ],
                "venue": "arXiv",
                "year": int(published[:4]) if published[:4].isdigit() else None,
                "abstract": " ".join((entry.findtext("atom:summary", "", ns) or "").split()),
                "citationCount": 0,
                "journal": {"name": journal_ref.strip()} if journal_ref else None,
                "publicationDate": published or None,
            }
        )
    return results


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", title or "").lower())


def _arxiv_query(search_query: str) -> Optional[str]:
    """Runs one rate-limited arXiv API query; returns the Atom XML or None."""
    global _arxiv_last_call
    for attempt in range(3):
        with _arxiv_lock:
            wait = ARXIV_MIN_INTERVAL_SEC - (time.time() - _arxiv_last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                rsp = requests.get(
                    "https://export.arxiv.org/api/query",
                    params={"search_query": search_query, "max_results": 5},
                    timeout=20,
                )
            except requests.RequestException:
                rsp = None
            finally:
                _arxiv_last_call = time.time()
        if rsp is not None and rsp.status_code == 200:
            return rsp.text
        time.sleep(5 * (attempt + 1))
    return None


def find_arxiv_id(title: str, min_ratio: int = 97) -> Optional[str]:
    """Returns the arXiv ID (no version) of the paper titled `title`, or None.

    The ID is taken from the arXiv API itself and accepted only when the arXiv
    title matches `title` almost exactly (normalized fuzz ratio >= min_ratio),
    so a similarly named paper (e.g. "Qwen2" for "Qwen2.5-VL") is rejected.
    Tries an exact title phrase first, then all title words.
    """
    title = (title or "").replace("\u2019", "'")
    phrase = re.sub(r"\s+", " ", re.sub(r"[^\w\s.'\-]", " ", title)).strip()
    words = [
        re.sub(r"[^\w]", "", w) for w in re.sub(r"[^\w\s']", " ", title).split() if "'" not in w
    ]
    words = [w for w in words if len(w) > 2]
    if not phrase:
        return None
    queries = [f'ti:"{phrase}"']
    if words:
        queries.append(" AND ".join(f"ti:{w}" for w in words[:12]))

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    target = _norm_title(title)
    failed = True
    for query in queries:
        xml = _arxiv_query(query)
        if xml is None:
            continue
        failed = False
        best_id, best_ratio = None, 0
        for entry in ET.fromstring(xml).findall("atom:entry", ns):
            entry_title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
            entry_id = (entry.findtext("atom:id", "", ns) or "").split("/abs/")[-1]
            ratio = fuzz.ratio(target, _norm_title(entry_title))
            if entry_id and ratio > best_ratio:
                best_id, best_ratio = re.sub(r"v\d+$", "", entry_id), ratio
        if best_ratio >= min_ratio:
            return best_id
    if failed:
        print(f"      ⚠️ arXiv lookup failed for '{title}'")
    return None


_SOURCES = [
    ("Semantic Scholar", _fetch_s2_candidates),
    ("OpenAlex", _fetch_openalex_candidates),
    ("arXiv", _fetch_arxiv_candidates),
]


def s2_title_search(
    title_query: str, year_hint: int = None, cutoff_date: str = None
) -> Optional[Dict]:
    """Finds the paper matching `title_query`, in the Semantic Scholar format.

    Sources are tried in order (Semantic Scholar, OpenAlex, arXiv); the next one
    is used only when the previous request failed, not when it found no match.
    """
    for source_name, fetch in _SOURCES:
        try:
            results = fetch(title_query)
        except Exception as e:
            print(f"      ⚠️ {source_name} error for '{title_query}': {e}")
            continue
        if results is None:
            continue
        match = _select_best_match(results, title_query, year_hint, cutoff_date)
        if match is not None and source_name != "Semantic Scholar":
            print(f"      ↪ matched via {source_name}: {match['title']}")
        return match
    return None
