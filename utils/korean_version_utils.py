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

"""Korean version of the final paper.

Takes the split project written by `utils.latex_split_utils` (main.tex +
sec/*.tex) and writes a Korean translation next to it:

  1. One LLM call builds a terminology glossary and translates the title, so
     all sections use the same Korean terms.
  2. Each sec/*.tex file is translated in parallel. Citation keys, labels,
     references, environments and math are checked against the English file;
     a section that fails the check twice is kept in English (with a warning).
  3. main.tex gets xetexko with an installed Hangul font (found via fc-list),
     Korean names for references/tables/figures, and the translated title.
  4. The project is compiled with xelatex (Hangul does not work with pdflatex).

Needs `xelatex` and the `xetexko` package (TinyTeX: `tlmgr install xetexko`)
and a Korean font such as Noto CJK KR or Nanum. Without them the .tex files are
still written but not compiled.

Usage outside the pipeline:
    python -m utils.korean_version_utils <split_dir> <out_dir> [--model NAME]
"""

import argparse
import os
import os.path as osp
import re
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from utils.latex_split_utils import one_sentence_per_line
from utils.gemini_utils import (
    call_gemini_with_contents,
    parse_gemini_json_results,
    parse_gemini_latex_results,
)

# --------------------------------------------------------------------------
# Prompts

GLOSSARY_SYSTEM_PROMPT = """You prepare the Korean translation of an English research paper written in LaTeX.
Return only a JSON object, in a ```json block:
{"title_ko": "<Korean title>", "glossary": [{"en": "<English term>", "ko": "<Korean rendering>"}, ...]}
Rules:
- The glossary lists the 20-50 recurring technical terms and coined names of this paper
  (method names, variant/condition names, metric descriptions, key concepts).
- Korean renderings must be what Korean researchers would actually write in a paper.
  Keep model names, dataset/simulator/benchmark names, acronyms and metric abbreviations in
  English (ko equals en for those). For key concepts that are usually written in Korean, give the
  Korean term; it will be followed by the English in parentheses on first use.
- The title: natural Korean; keep names and acronyms in English. Plain text, no LaTeX commands
  unless the English title has them."""

SECTION_SYSTEM_PROMPT = """You translate one LaTeX section file of an English research paper into Korean.
Return the complete translated file in a single ```latex block and nothing else.
Rules:
- Korean academic register (~이다/~한다 체), natural Korean as if originally written by a Korean
  researcher; not word-for-word. Do not add, drop or change content, claims or numbers.
- Keep every LaTeX command, environment, \\label, \\ref/\\cref/\\Cref, \\cite*/\\citet/\\citep
  (same keys, same order), math ($...$ and display math), macros, table column specs and
  `%%%%` marker comments exactly. Leave other LaTeX comments as they are.
- Translate prose, captions, table cell text, \\section/\\subsection/\\paragraph titles and the
  contents of \\textbf/\\emph. Keep text inside math (\\text{...}) in English.
- Use the glossary for recurring terms. On the first use of a key concept in this file you may add
  the English in parentheses, e.g. 추론 충실성(reasoning faithfulness).
- Keep model names, dataset/simulator/benchmark names, acronyms and metric abbreviations in English.
- \\citet{key} renders English author names; phrase the Korean so it reads naturally
  (e.g. "\\citet{key}는 ...을 제안하였다").
- Do not restructure tables or lists."""

ABSTRACT_NOTE = (
    "\nThis file is the abstract. It is placed inside an abstract defined in the preamble: "
    "it must not contain any \\begin{...}/\\end{...} environment."
)

# --------------------------------------------------------------------------
# Structure check

_STRUCT_RE = re.compile(
    r"\\(cite[a-zA-Z]*|label|ref|eqref|autoref|cref|Cref|begin|end|includegraphics|input)\*?"
    r"(?:\[[^\]]*\])*\{([^}]*)\}"
)


def _structure(tex: str) -> Counter:
    body = "\n".join(l for l in tex.split("\n") if not l.lstrip().startswith("%"))
    c = Counter(f"\\{cmd}{{{arg.strip()}}}" for cmd, arg in _STRUCT_RE.findall(body))
    c["$ count"] = body.replace("\\$", "").count("$")
    c["& count"] = body.replace("\\&", "").count("&")
    return c


def _structure_diff(en: str, ko: str) -> List[str]:
    a, b = _structure(en), _structure(ko)
    problems = []
    for key in sorted(set(a) | set(b)):
        if a[key] != b[key]:
            problems.append(f"{key}: English has {a[key]}, translation has {b[key]}")
    return problems


# --------------------------------------------------------------------------
# Hangul fonts

_SERIF_PREFS = ["Noto Serif CJK KR", "Noto Serif KR", "NanumMyeongjo", "UnBatang", "Baekmuk Batang"]
_SANS_PREFS = ["Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "UnDotum", "Baekmuk Dotum"]
_MONO_PREFS = ["Noto Sans Mono CJK KR", "NanumGothicCoding", "D2Coding"]


def _list_korean_fonts() -> Dict[str, List[Tuple[str, str, int]]]:
    """family -> [(style, file, index)] for installed fonts covering Korean."""
    try:
        out = subprocess.run(
            ["fc-list", "-f", "%{family}\t%{style}\t%{file}\t%{index}\n", ":lang=ko"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return {}
    fonts: Dict[str, List[Tuple[str, str, int]]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        families, styles, path, index = parts
        style = styles.split(",")[0].strip()
        for family in families.split(","):
            fonts.setdefault(family.strip(), []).append(
                (style, path, int(index) if index.isdigit() else 0)
            )
    return fonts


def _fontspec_command(cmd: str, entries: List[Tuple[str, str, int]]) -> Optional[str]:
    def pick(names):
        for n in names:
            for style, path, idx in entries:
                if style.lower() == n:
                    return path, idx
        return None

    regular = pick(["regular", "book", "normal", "medium"]) or (entries[0][1], entries[0][2])
    bold = pick(["bold"]) or regular
    if osp.dirname(bold[0]) != osp.dirname(regular[0]):
        bold = regular
    stem, ext = osp.splitext(osp.basename(regular[0]))
    bold_stem = osp.splitext(osp.basename(bold[0]))[0]
    # paper templates may load text fonts (e.g. XCharter) whose default features
    # (Extension=.otf, *-Roman, ...) would leak into these fonts, so every shape is explicit.
    opts = [
        f"Path={osp.dirname(regular[0])}/",
        f"Extension={ext}",
        f"UprightFont={stem}", f"BoldFont={bold_stem}",
        f"ItalicFont={stem}", f"BoldItalicFont={bold_stem}",
        f"SlantedFont={stem}", f"BoldSlantedFont={bold_stem}",
    ]
    if ext.lower() in (".ttc", ".otc"):
        opts.append(f"FontIndex={regular[1]}")
        if bold[1] != regular[1]:
            opts.append(f"BoldFeatures={{FontIndex={bold[1]}}}")
    return f"\\{cmd}{{{stem}}}[{', '.join(opts)}]"


def hangul_font_preamble() -> Optional[str]:
    """xetexko font setup lines, or None if no Korean font is installed."""
    fonts = _list_korean_fonts()

    def first(prefs):
        for name in prefs:
            if name in fonts:
                return fonts[name]
        return None

    serif = first(_SERIF_PREFS) or first(_SANS_PREFS)
    if serif is None:
        if not fonts:
            return None
        serif = next(iter(fonts.values()))
    sans = first(_SANS_PREFS) or serif
    mono = first(_MONO_PREFS) or sans
    return "\n".join(
        [
            _fontspec_command("setmainhangulfont", serif),
            _fontspec_command("setsanshangulfont", sans),
            _fontspec_command("setmonohangulfont", mono),
        ]
    )


_KO_NAMES = r"""% ---- Korean names for references, tables, figures ----
\AtBeginDocument{%
  \renewcommand{\refname}{참고문헌}\renewcommand{\bibname}{참고문헌}%
  \renewcommand{\appendixname}{부록}\renewcommand{\tablename}{표}%
  \renewcommand{\figurename}{그림}}
\makeatletter
\@ifpackageloaded{cleveref}{%
  \crefformat{section}{#2#1#3절}\Crefformat{section}{#2#1#3절}%
  \crefformat{subsection}{#2#1#3절}\Crefformat{subsection}{#2#1#3절}%
  \crefformat{subsubsection}{#2#1#3절}\Crefformat{subsubsection}{#2#1#3절}%
  \crefformat{appendix}{부록~#2#1#3}\Crefformat{appendix}{부록~#2#1#3}%
  \crefformat{subappendix}{부록~#2#1#3}\Crefformat{subappendix}{부록~#2#1#3}%
  \crefformat{table}{표~#2#1#3}\Crefformat{table}{표~#2#1#3}%
  \crefformat{figure}{그림~#2#1#3}\Crefformat{figure}{그림~#2#1#3}%
  \crefformat{equation}{식~#2(#1)#3}\Crefformat{equation}{식~#2(#1)#3}%
  \crefmultiformat{equation}{식~#2(#1)#3}{ 및~#2(#1)#3}{, #2(#1)#3}{, #2(#1)#3}%
  \Crefmultiformat{equation}{식~#2(#1)#3}{ 및~#2(#1)#3}{, #2(#1)#3}{, #2(#1)#3}%
}{}
\makeatother
"""


def _replace_braced(tex: str, command: str, new_value: str) -> str:
    """Replaces the argument of the first `\\command{...}` (balanced braces)."""
    start = tex.find(f"\\{command}{{")
    if start == -1:
        return tex
    open_idx = start + len(command) + 1
    depth = 0
    for i in range(open_idx, len(tex)):
        depth += {"{": 1, "}": -1}.get(tex[i], 0)
        if depth == 0:
            return tex[: open_idx + 1] + new_value + tex[i:]
    return tex


def build_korean_main(main_tex: str, title_ko: Optional[str], font_preamble: str) -> str:
    lines = main_tex.split("\n")
    doc_idx = next(i for i, l in enumerate(lines) if l.lstrip().startswith("\\documentclass"))
    setup = [
        "",
        "% ---- Korean version: compile with xelatex (xelatex main; bibtex main; xelatex main; xelatex main) ----",
        "\\usepackage{xetexko}",
        font_preamble,
    ]
    lines[doc_idx + 1 : doc_idx + 1] = setup
    tex = "\n".join(lines)
    # The real \begin{document} line (templates mention it in comments too).
    begin = re.search(r"^[ \t]*\\begin\{document\}", tex, re.MULTILINE).start()
    tex = tex[:begin] + _KO_NAMES + "\n" + tex[begin:]
    if title_ko:
        tex = _replace_braced(tex, "title", title_ko)
    return tex.replace("Correspondence to:", "교신저자:")


# --------------------------------------------------------------------------
# Translation


def _paper_text(split_dir: str, section_files: List[str]) -> str:
    with open(osp.join(split_dir, "main.tex"), encoding="utf-8") as f:
        title = re.search(r"\\title\{(.*)\}", f.read())
    parts = [f"TITLE: {title.group(1) if title else ''}"]
    for name in section_files:
        with open(osp.join(split_dir, "sec", name), encoding="utf-8") as f:
            parts.append(f"===== {name} =====\n{f.read()}")
    return "\n\n".join(parts)


def _make_glossary(paper_text: str, model_name: str) -> Tuple[Optional[str], str]:
    rsp = call_gemini_with_contents(
        contents=paper_text,
        model_name=model_name,
        result_parsing_func=parse_gemini_json_results,
        generation_configs={"system_instruction": GLOSSARY_SYSTEM_PROMPT},
    )
    data = rsp.get("parsed_response") or {}
    entries = data.get("glossary") or []
    glossary = "\n".join(
        f"- {e.get('en')} -> {e.get('ko')}" for e in entries if e.get("en") and e.get("ko")
    )
    return data.get("title_ko"), glossary


def _translate_section(name: str, english: str, glossary: str, model_name: str) -> Tuple[str, List[str]]:
    """Returns (tex, problems). On failure the English text is returned."""
    system = SECTION_SYSTEM_PROMPT + (ABSTRACT_NOTE if name.startswith("0_abstract") else "")
    feedback = ""
    problems: List[str] = []
    for attempt in range(2):
        prompt = (
            f"GLOSSARY (English -> Korean):\n{glossary or '(none)'}\n\n"
            f"FILE: sec/{name}\n```latex\n{english}\n```"
            + feedback
        )
        rsp = call_gemini_with_contents(
            contents=prompt,
            model_name=model_name,
            result_parsing_func=parse_gemini_latex_results,
            generation_configs={"system_instruction": system},
        )
        korean = (rsp.get("parsed_response") or "").strip("\n") + "\n"
        problems = _structure_diff(english, korean)
        if not problems:
            return korean, []
        print(f"      ⚠️ sec/{name}: translation changed the LaTeX structure (attempt {attempt + 1})")
        feedback = (
            "\n\nYour previous translation changed the LaTeX structure. Keep these exactly as in "
            "the English file:\n" + "\n".join(f"- {p}" for p in problems[:30])
        )
    return english, problems


def write_korean_version(
    split_dir: str,
    out_dir: str,
    model_name: str,
    pdf_output_path: Optional[str] = None,
    max_workers: int = 4,
) -> bool:
    """Translates the split project in `split_dir` into `out_dir` and compiles it.
    Returns True if a Korean PDF was produced."""
    sec_dir = osp.join(split_dir, "sec")
    section_files = sorted(f for f in os.listdir(sec_dir) if f.endswith(".tex"))
    if not section_files:
        raise ValueError(f"No section files in {sec_dir}")

    if osp.exists(out_dir):
        shutil.rmtree(out_dir)
    shutil.copytree(
        split_dir, out_dir,
        ignore=shutil.ignore_patterns("main.aux", "main.log", "main.out", "main.blg", "main.bbl", "main.pdf"),
    )

    print(f" >> Korean version: building glossary with {model_name}...")
    title_ko, glossary = _make_glossary(_paper_text(split_dir, section_files), model_name)

    def run(name):
        with open(osp.join(sec_dir, name), encoding="utf-8") as f:
            english = f.read()
        return name, _translate_section(name, english, glossary, model_name)

    print(f" >> Korean version: translating {len(section_files)} section files...")
    kept_english = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, (tex, problems) in pool.map(run, section_files):
            with open(osp.join(out_dir, "sec", name), "w", encoding="utf-8") as f:
                f.write(one_sentence_per_line(tex))
            if problems:
                kept_english.append(name)
                print(f"      ⚠️ sec/{name} kept in English: " + "; ".join(problems[:5]))
    with open(osp.join(out_dir, "glossary.md"), "w", encoding="utf-8") as f:
        f.write(f"# Korean glossary\n\nTitle: {title_ko}\n\n{glossary}\n")

    font_preamble = hangul_font_preamble()
    with open(osp.join(split_dir, "main.tex"), encoding="utf-8") as f:
        main_en = f.read()
    with open(osp.join(out_dir, "main.tex"), "w", encoding="utf-8") as f:
        f.write(build_korean_main(main_en, title_ko, font_preamble or "% (no Korean font found: set \\setmainhangulfont here)"))
    print(f" >> Korean LaTeX project written to: {out_dir}")

    if font_preamble is None:
        print(" >> ⚠️ No Korean font found (fc-list :lang=ko). Install e.g. fonts-noto-cjk, then compile with xelatex.")
        return False
    if shutil.which("xelatex") is None:
        print(" >> ⚠️ xelatex not found; the Korean version was not compiled.")
        return False
    if subprocess.run(["kpsewhich", "xetexko.sty"], capture_output=True, text=True).stdout.strip() == "":
        print(" >> ⚠️ xetexko is not installed (TinyTeX: tlmgr install xetexko); not compiled.")
        return False

    from utils.pdf_utils import compile_latex

    pdf_path = osp.join(out_dir, "main.pdf")
    compile_latex(out_dir, pdf_path, "main", timeout=300, engine="xelatex")
    if not osp.exists(pdf_path):
        print(" >> ⚠️ Korean version did not compile; see main.log in", out_dir)
        return False
    if pdf_output_path:
        shutil.copy(pdf_path, pdf_output_path)
    note = f" ({len(kept_english)} section(s) kept in English)" if kept_english else ""
    print(f" >> Korean PDF saved to: {pdf_output_path or pdf_path}{note}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("split_dir", help="Directory with main.tex and sec/ (output of latex_split_utils)")
    parser.add_argument("out_dir")
    parser.add_argument("--model", default=os.getenv("PO_CLAUDE_MODEL", "claude-opus-5-5"))
    parser.add_argument("--pdf", default=None, help="Also copy the Korean PDF here")
    a = parser.parse_args()
    if a.model.startswith("claude"):
        os.environ.setdefault("PAPER_ORCHESTRA_BACKEND", "claude")
    raise SystemExit(0 if write_korean_version(a.split_dir, a.out_dir, a.model, a.pdf) else 1)
