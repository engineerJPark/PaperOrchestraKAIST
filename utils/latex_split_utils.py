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

"""Splits a single-file LaTeX paper into main.tex + sec/<section>.tex.

The pipeline writes the paper as one .tex file. For hand editing it is easier
to have one file per section, so after the final PDF is produced the paper is
also written out as:

    <out_dir>/main.tex          preamble, title, \\input lines, bibliography
    <out_dir>/sec/0_abstract.tex
    <out_dir>/sec/1_introduction.tex ...
    <out_dir>/sec/A_<appendix title>.tex ...

plus the template support files, references.bib and figures/. The split
version is compiled and its text compared with the single-file PDF.

Usage outside the pipeline:
    python -m utils.latex_split_utils <paper.tex> <out_dir> [--template_dir DIR]
"""

import argparse
import os
import os.path as osp
import re
import shutil
from typing import List, Optional, Tuple

_SECTION_RE = re.compile(r"^\s*\\section\*?\s*\{")
_MARKER_RE = re.compile(r"^%{5,}\s*\S")
# Lines that end the last section of the main text or of the appendix.
_BOUNDARY_RE = re.compile(
    r"^\s*\\(appendix|bibliography|bibliographystyle|printbibliography|end\{document\})\b"
)
# Template files that are inputs for the agents, not part of the paper.
_TEMPLATE_SKIP = {"template.tex", "guidelines.md"}


def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("%")


def _section_title(line: str) -> str:
    start = line.index("{", line.index("\\section")) + 1
    depth, i = 1, start
    while i < len(line) and depth:
        depth += {"{": 1, "}": -1}.get(line[i], 0)
        i += 1
    return line[start : i - 1]


def _slug(title: str) -> str:
    title = re.sub(r"\\[a-zA-Z]+\*?", " ", title)  # drop LaTeX commands
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug[:40].rstrip("_") or "section"


def _section_end(lines: List[str], start: int, limit: int) -> int:
    """Last line (inclusive) of the section starting at `start`, before `limit`.

    Templates mark section bodies with paired comment markers
    (%%%%%%%%% INTRODUCTION %%%%%%%%%). If the section has them, it ends at the
    closing marker, so text placed after it (e.g. bibliography notes) stays in
    main.tex. Otherwise it ends before `limit`, minus trailing blank/comment lines.
    """
    for i in range(start + 1, min(start + 6, limit)):
        if _MARKER_RE.match(lines[i]):
            marker = lines[i].strip()
            closing = [j for j in range(i + 1, limit) if lines[j].strip() == marker]
            if closing:
                return closing[-1]
            break
    end = limit - 1
    while end > start and _is_blank_or_comment(lines[end]):
        end -= 1
    return end


def _find_abstract(lines: List[str], doc_end: int) -> Optional[Tuple[int, int]]:
    """Range (inclusive) of the abstract body, without its own marker lines."""
    begin = next(
        (i for i in range(doc_end) if lines[i].strip() == "\\begin{abstract}"), None
    )
    if begin is None:
        return None
    end = next(
        (i for i in range(begin + 1, doc_end) if lines[i].strip() == "\\end{abstract}"),
        None,
    )
    if end is None or end - begin < 2:
        return None
    first, last = begin + 1, end - 1
    if (
        last > first
        and _MARKER_RE.match(lines[first])
        and lines[first].strip() == lines[last].strip()
    ):
        first, last = first + 1, last - 1
    return (first, last) if last >= first else None


def split_tex_sections(tex: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Returns (main_tex, [(section_filename_without_ext, content), ...])."""
    lines = tex.split("\n")
    begin_doc = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("\\begin{document}")),
        None,
    )
    end_doc = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("\\end{document}")),
        len(lines),
    )
    if begin_doc is None:
        raise ValueError("No \\begin{document} found")

    parts = []  # (start, end_inclusive, name)
    abstract = _find_abstract(lines, end_doc)
    if abstract:
        parts.append((abstract[0], abstract[1], "0_abstract"))

    section_starts = [
        i for i in range(begin_doc + 1, end_doc) if _SECTION_RE.match(lines[i])
    ]
    appendix_line = next(
        (
            i
            for i in range(begin_doc + 1, end_doc)
            if lines[i].strip().startswith("\\appendix")
        ),
        None,
    )
    main_no, app_no = 0, 0
    for k, s in enumerate(section_starts):
        if abstract and abstract[0] <= s <= abstract[1]:
            continue
        limit = next(
            (
                i
                for i in range(s + 1, end_doc + 1)
                if i == end_doc or _SECTION_RE.match(lines[i]) or _BOUNDARY_RE.match(lines[i])
            ),
            end_doc,
        )
        e = _section_end(lines, s, limit)
        slug = _slug(_section_title(lines[s]))
        if appendix_line is not None and s > appendix_line:
            name = f"{chr(ord('A') + app_no)}_{slug}" if app_no < 26 else f"Z{app_no}_{slug}"
            app_no += 1
        else:
            main_no += 1
            name = f"{main_no}_{slug}"
        parts.append((s, e, name))

    if len(parts) <= (1 if abstract else 0):
        raise ValueError("No \\section found to split")

    main_lines, files, cursor = [], [], 0
    for s, e, name in sorted(parts):
        main_lines += lines[cursor:s]
        main_lines.append(f"\\input{{sec/{name}}}")
        files.append((name, "\n".join(lines[s : e + 1]).strip("\n") + "\n"))
        cursor = e + 1
    main_lines += lines[cursor:]
    return "\n".join(main_lines), files


def _pdf_text(pdf_path: str) -> str:
    import pymupdf  # type: ignore

    with pymupdf.open(pdf_path) as doc:
        return " ".join(" ".join(page.get_text() for page in doc).split())


def write_split_latex_project(
    tex_path: str,
    out_dir: str,
    template_dir: Optional[str] = None,
    reference_pdf: Optional[str] = None,
    compile_check: bool = True,
) -> bool:
    """Writes `tex_path` as a split project in `out_dir`. Returns True if it
    compiled (and, given `reference_pdf`, produced the same text)."""
    src_dir = osp.dirname(osp.abspath(tex_path))
    with open(tex_path, "r", encoding="utf-8") as f:
        main_tex, sections = split_tex_sections(f.read())

    if osp.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(osp.join(out_dir, "sec"))

    # Support files (class, styles, macros, logo) from the template, then the
    # paper's own bibliography and figures from the directory it was compiled in.
    if template_dir and osp.isdir(template_dir):
        for name in os.listdir(template_dir):
            if name in _TEMPLATE_SKIP:
                continue
            src = osp.join(template_dir, name)
            dst = osp.join(out_dir, name)
            if osp.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy(src, dst)
    for name in ["references.bib", "figures"]:
        src = osp.join(src_dir, name)
        if osp.isdir(src):
            shutil.copytree(src, osp.join(out_dir, name), dirs_exist_ok=True)
        elif osp.exists(src):
            shutil.copy(src, osp.join(out_dir, name))
    if not template_dir:
        for name in os.listdir(src_dir):
            if name.endswith((".cls", ".sty", ".bst")) or name in {"macro.tex", "math_commands.tex"}:
                shutil.copy(osp.join(src_dir, name), out_dir)
        if osp.isdir(osp.join(src_dir, "assets")):
            shutil.copytree(osp.join(src_dir, "assets"), osp.join(out_dir, "assets"), dirs_exist_ok=True)

    with open(osp.join(out_dir, "main.tex"), "w", encoding="utf-8") as f:
        f.write(main_tex)
    for name, content in sections:
        with open(osp.join(out_dir, "sec", f"{name}.tex"), "w", encoding="utf-8") as f:
            f.write(content)
    print(f" >> Split LaTeX project ({len(sections)} files in sec/) written to: {out_dir}")

    if not compile_check:
        return True
    from utils.pdf_utils import compile_latex

    pdf_path = osp.join(out_dir, "main.pdf")
    compile_latex(out_dir, pdf_path, "main", timeout=120)
    if not osp.exists(pdf_path):
        print(" >> ⚠️ Split project did not compile; use the single-file .tex instead.")
        return False
    if reference_pdf and osp.exists(reference_pdf):
        if _pdf_text(pdf_path) != _pdf_text(reference_pdf):
            print(" >> ⚠️ Split project compiles but its text differs from the final PDF.")
            return False
    print(" >> Split project compiled; text matches the final PDF.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tex_path")
    parser.add_argument("out_dir")
    parser.add_argument("--template_dir", default=None)
    parser.add_argument("--reference_pdf", default=None)
    parser.add_argument("--no_compile", action="store_true")
    a = parser.parse_args()
    ok = write_split_latex_project(
        a.tex_path, a.out_dir, a.template_dir, a.reference_pdf, not a.no_compile
    )
    raise SystemExit(0 if ok else 1)
