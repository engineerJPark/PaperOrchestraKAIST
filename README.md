# PaperOrchestra for KAIST Tech Reports

This repository is a modified version of [PaperOrchestra](https://github.com/google-research/paper-orchestra), adapted to write papers in the **KAIST AI tech report** format. PaperOrchestra is a multi-agent pipeline that turns pre-writing materials into a LaTeX manuscript. You give it an idea document, an experimental log, and optionally figures and a list of required references. Separate agents then handle the outline, the literature review, section writing, review-driven refinement, and optional plotting.

This fork is not an official Google product and is not affiliated with the original authors.

## Changes from the original PaperOrchestra

- **KAIST tech report template** (`templates/kaist-techreport`): the KAIST AI report class (`paper.cls`), macros, and the logo. It also includes writing guidelines tuned for the class: the abstract goes in the preamble, citations use natbib author-year, the main text is 8–12 pages, and figures are single-column.
- **Claude backend** (`--backend claude`): every LLM call runs through the Claude Code CLI (`claude -p`), so a logged-in Claude subscription is enough and no API key is needed.
- **Must-cite references** (`raw_materials/reference.md`): BibTeX entries that the paper must cite. They are added to the bibliography as written and keep their citation keys, and the outline and literature agents are told to cite every one.
- **Robust literature verification**: if Semantic Scholar fails (for example HTTP 429 without an API key), lookups fall back to OpenAlex and then arXiv. Metadata is cleaned so that it compiles with pdflatex: unsupported Unicode is replaced and `&`, `%`, `#`, `_` are escaped.
- **Split LaTeX output** (`latex/`): the final paper is also written as `main.tex` plus one file per section under `sec/`, with one sentence per line for easier editing and diffs.
- **Korean version** (`latex_ko/`, `final_paper_ko.pdf`): a Korean translation of the final paper. It uses a shared terminology glossary, checks that the LaTeX structure survived translation, and compiles with xelatex.

## Installation

1. **Python environment**
   ```bash
   conda create -n paper_orchestra python=3.11
   conda activate paper_orchestra
   pip install -r requirements.txt
   ```

2. **LaTeX**: `pdflatex` and `bibtex` must be on `PATH`. Without root access, [TinyTeX](https://yihui.org/tinytex/) works. Install it under `~/.TinyTeX` and add `~/.TinyTeX/bin/x86_64-linux` to `PATH`, which `paper_writing_cli.sh` does automatically. Missing LaTeX packages can be added with `tlmgr install <package>`.
   For the Korean version you also need `xelatex`, which TinyTeX includes. Install the `xetexko` package with `tlmgr install xetexko`, and make sure a Korean font is installed, such as Noto CJK KR (`fonts-noto-cjk`) or Nanum. If either is missing, the Korean `.tex` files are still written but not compiled.

3. **LLM backend**, one of the following:
   - **Claude**: install [Claude Code](https://claude.com/claude-code) and run `claude` once to log in.
   - **Gemini/OpenAI**: put the API keys in `.env` (see below).

4. **`.env`** in the repository root. Every key is optional unless your chosen backend needs it.
   ```env
   SEMANTIC_SCHOLAR_API_KEY=   # free; avoids frequent HTTP 429
   OPENALEX_API_KEY=           # free; raises the OpenAlex fallback budget
   GEMINI_API_KEY=             # --backend gemini only (or VERTEX_AI_PROJECT + VERTEX_AI_LOCATION)
   OPENAI_API_KEY=             # OpenAI models only
   ```

## Raw materials

Put the following files in a single directory and pass it with `--raw_materials_dir`:

| File | Content |
|---|---|
| `idea_sparse.md` | Research idea: problem, method, and planned experiments. Change the name with `--idea_filename`. |
| `experimental_log.md` | Experimental setup and results. If no experiments have been run, say so explicitly, so that no numbers are invented. |
| `reference.md` | Optional. BibTeX entries that must be cited. Change the name with `--references_filename`. |
| `figures/` | Optional. Figures to include. |

## Usage

```bash
python paper_writing_cli.py --backend claude \
  --raw_materials_dir /path/to/raw_materials/ \
  --latex_template_dir templates/kaist-techreport \
  --output_dir /path/to/output/
```

You can also use `./paper_writing_cli.sh`; edit the variables at the top of the script first.

### Arguments

- `--raw_materials_dir` (required): directory with the raw materials described above.
- `--latex_template_dir` (required): LaTeX template, e.g. `templates/kaist-techreport`. `templates/cvpr2025` and `templates/iclr2025` from the original repository still work.
- `--output_dir`: output directory. If omitted, a timestamped folder is created.
- `--backend`: `gemini` (default; uses the API keys in `.env`) or `claude`.
- `--references_filename`: BibTeX file in the raw materials with papers that must be cited. Default: `reference.md`. Optional.
- `--korean_version`: also write the Korean translation. Default: `true`. Pass `false` to skip it.
- `--use_plotting`: generate figures from code with the plotting agent. Default: `false`.
- `--writer_model_name`, `--reflection_model_name`, `--plotting_model_name`: model overrides. The defaults depend on the backend.
- `--research_cutoff`: latest month (`YYYY-MM`) of papers the literature review may cite. Default: the current month.

### Output

| Path | Content |
|---|---|
| `final_paper.pdf` | Final paper |
| `latex/` | Final paper as an editable project: `main.tex`, `sec/0_abstract.tex`, `sec/1_introduction.tex`, …, and appendix sections as `sec/A_*.tex`. Build with `pdflatex main && bibtex main && pdflatex main && pdflatex main`. |
| `final_paper_ko.pdf`, `latex_ko/` | Korean version. Build with `xelatex` instead of `pdflatex`. `latex_ko/glossary.md` lists the term translations used. |
| `content_refinement_workdir/` | Review and refinement rounds, peer reviews, and the single-file final source `final_refined_paper.tex` |
| `latex_writeup/` | Working copy of the template and the first draft before refinement; this is not the final paper |
| `literature_agent_output/`, `outline.json` | Intermediate outputs of the literature and outline agents |

Both split projects, `latex/` and `latex_ko/`, are compiled during the run. The English project is also compared with `final_paper.pdf` to confirm the text is identical. If a step fails, the run still finishes with a warning, and the single-file source remains available.

The Korean version is a machine translation. Review it before using it. A section whose LaTeX structure (citations, labels, references, math) did not survive translation is left in English, and a warning is printed.

### Claude backend

- Default model: `claude-opus-5-5` for the writer, reflection, and plotting agents. Calls that the original code hardcodes to Gemini Flash (literature search, title extraction) use `claude-sonnet-5-5`. Override these with the model arguments or with the `PO_CLAUDE_MODEL` and `PO_CLAUDE_FAST_MODEL` environment variables.
- Each call runs in its own `claude -p` process with `--safe-mode`, no tools, and its own system prompt. Literature search uses Claude's WebSearch tool in place of Google Search grounding.
- Claude has no image generation model. With `--use_plotting true`, conceptual diagrams are drawn as matplotlib code instead; statistical plots work as before.
- `temperature` cannot be set through the CLI and is ignored.
- Optional environment variables:
  - `PO_CLAUDE_EFFORT`: the `--effort` level.
  - `PO_CLAUDE_TIMEOUT`: seconds per call. Default: 1800.
  - `PO_CLAUDE_MAX_OUTPUT_TOKENS`: default 64000.
  - `PO_CLAUDE_BIN`: path to `claude`.
- A full paper takes many calls with large contexts, so it uses a noticeable share of your subscription's usage limits. With the default settings, the Korean version adds about 10 more calls.

### Literature verification sources

The literature agent verifies every candidate paper (title, authors, abstract, year) before citing it. Lookups go to Semantic Scholar first. If that request fails, the same title is looked up on OpenAlex and then on arXiv. Without a key, OpenAlex allows about 100 searches per day per IP, shared by everything on that IP; `OPENALEX_API_KEY` raises the limit. arXiv is queried at most once every 3 seconds, as its API terms require.

### Interactive demo

The Streamlit demo from the original repository is in `frontend/`; see [frontend/README.md](frontend/README.md).

## Project structure

- `methods/`: pipeline and agents (outline, literature review, section writing, content refinement, plotting).
- `utils/`: LLM backends (`gemini_utils.py`, `claude_cli_utils.py`), literature lookup (`scholar_utils.py`), LaTeX compilation (`pdf_utils.py`), section splitting (`latex_split_utils.py`), and the Korean version (`korean_version_utils.py`).
- `templates/`: LaTeX templates. To add a template, create a folder with `template.tex`, `guidelines.md`, and the class and style files.
- `autoraters/`: automatic reviewers used during refinement and evaluation.

## Credits and license

This project is based on PaperOrchestra by Google Cloud AI Research ([repository](https://github.com/google-research/paper-orchestra), [paper](https://arxiv.org/abs/2604.05018)). If you use the pipeline in research, please cite the original work:

```bibtex
@article{song2026paperorchestra,
  title={PaperOrchestra: A Multi-Agent Framework for Automated AI Research Paper Writing},
  author={Song, Yiwen and Song, Yale and Pfister, Tomas and Yoon, Jinsung},
  journal={arXiv preprint arXiv:2604.05018},
  year={2026}
}
```

The original code and this modified version are licensed under the Apache License 2.0 (see `LICENSE`). Files changed from the original are listed in the git history of this repository.
