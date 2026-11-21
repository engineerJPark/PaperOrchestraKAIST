# KAIST AI Technical Report Writing Guidelines

Below are the instructions to help you with the LaTeX writing process for a **KAIST AI technical report**.
This is a technical report (not a conference submission): it is **not anonymous** and has **no hard page limit**, but it should still be concise and submission-quality.

## General Setup
* **Document Class:** `\documentclass[10pt,a4paper,logo]{paper}` (custom class `paper.cls`, a tech-report style derived from the Google DeepMind / UC Berkeley RAIL template).
* **Layout:** Single column, 10-point XCharter font, A4 paper. The KAIST logo (`assets/logo.png`) is placed in the first-page header automatically.
* **Bibliography style:** `plainnat` with `natbib` (`authoryear, round`).
* **Do NOT modify** the `\documentclass` line, its options, `paper.cls`, `macro.tex`, `math_commands.tex`, or anything in the preamble other than the title/author block and the abstract.
* **Do NOT load** additional packages that change the layout or fonts (e.g., `geometry`, `times`, `fontenc`, `hyperref`, `xcolor`, `titlesec` are already loaded by the class). Already available: `amsmath`, `amssymb`, `amsthm`, `graphicx`, `booktabs`, `multirow`, `makecell`, `arydshln`, `subcaption`, `wrapfig`, `adjustbox`, `tabularx`, `colortbl`, `enumitem`, `cleveref`, `tcolorbox`, `nicefrac`, `bbm`, `dsfont`, `pifont`.

## Title and Authors
* Fill in a concise, informative `\title{...}`.
* **Keep the author block exactly as given in `template.tex`** (`Author A`, `Author B`, `Author C`, `KAIST AI`, `email@domain`). The real authors will replace these placeholders manually. Do not invent author names or e-mails.
* Leave `\reportnumber{}` empty.
* The class always prints the line "*Equal Contribution, †Corresponding Authors" under the authors; do not try to remove it.

## Abstract (Crucial — class-specific)
* The abstract **must stay in the preamble, before `\begin{document}`**, exactly where it is in `template.tex`. `\maketitle` prints it automatically.
* The abstract must **NOT contain any `\begin{...}` / `\end{...}` environments** (no itemize, no equation environments). Inline math `$...$` is fine.
* One paragraph, roughly 150–250 words. Avoid citations in the abstract.
* Optionally, keywords can be added in the preamble with `\keywords{keyword1, keyword2, ...}`.

## Report Structure
Keep the following top-level sections, in this order, with their existing `\label`s. You may add `\subsection`s / `\subsubsection`s inside them.
1. `\section{Introduction}` — `\label{sec:introduction}`
2. `\section{Related Work}` — `\label{sec:related_work}`
3. `\section{Method}` — `\label{sec:method}`
4. `\section{Experimental Setup}` — `\label{sec:experimental_setup}` (datasets, baselines, metrics, implementation details)
5. `\section{Experimental Results}` — `\label{sec:experimental_result}` (main results, ablations, analysis)
6. `\section{Conclusion}` — `\label{sec:conclusion}` (summary, limitations, future work)
7. References (`\bibliography{references}`)
8. Appendix (optional, see below)

## Report Length
* **Main Text** (everything up to the start of the References): target **8–12 pages**. There is no strict upper limit, but do not pad the text; prefer density of information.
* **References and Appendix:** Unlimited, do not count toward the main text length.

## Writing Style
* Write in **English only**. Do **not** include Korean (Hangul) or other CJK characters anywhere in the LaTeX source — the report is compiled with `pdflatex` without CJK support and compilation will fail.
* Since the report is not anonymous, first-person plural ("we") is fine, and own prior work may be cited normally.
* Write the method name directly in the text (do not use the `\methodname` macro).

## Citations and References
* Use `natbib` commands:
    * **Narrative citation:** `\citet{key}` → "Author et al. (2024)".
    * **Parenthetical citation:** `\citep{key}` → "(Author et al., 2024)".
* Only use BibTeX keys that exist in the provided reference library.
* Keep the bibliography commands exactly as:
    ```latex
    \bibliographystyle{plainnat}
    \bibliography{references}
    ```

## Cross-References
* `cleveref` is loaded. Prefer `\Cref{...}` / `\cref{...}` (e.g., `\Cref{fig:overview}` → "Figure 1", `\Cref{eq:loss}` → "Eq. (1)").
* Use label prefixes: `sec:`, `fig:`, `tab:`, `eq:`.

## Figures and Tables
* **Figures Directory:** All figures reside in the `figures/` directory. Always include the `figures/` prefix and the exact file extension in `\includegraphics` (e.g., `figures/fig_overview.png`).
* The layout is **single column**: use `figure` / `table` (not `figure*` / `table*`), and size figures with `width=\linewidth` (or a fraction of it).
* **Placement:** Figure captions go **below** the figure; table captions go **above** the table. Prefer `[t]` placement.
* Use `booktabs` rules (`\toprule`, `\midrule`, `\bottomrule`) in tables; no vertical lines. `\cmark` / `\xmark` are available for check/cross marks.

### Example Code for Figures
```latex
\begin{figure}[t]
    \centering
    \includegraphics[width=\linewidth]{figures/fig_overview.png}
    \caption{Overview of the proposed method.}
    \label{fig:overview}
\end{figure}
```

### Example Code for Tables
```latex
\begin{table}[t]
    \centering
    \caption{Main results. Best results are in \textbf{bold}.}
    \label{tab:main_results}
    \begin{tabular}{lcc}
        \toprule
        Method & Metric A $\uparrow$ & Metric B $\downarrow$ \\
        \midrule
        Baseline & 70.1 & 0.35 \\
        Ours     & \textbf{75.3} & \textbf{0.28} \\
        \bottomrule
    \end{tabular}
\end{table}
```

### Prompt Boxes
For showing LLM prompts or text examples, a `promptbox` environment (gray `tcolorbox`, monospace) is predefined:
```latex
\begin{promptbox}
You are a helpful assistant ...
\end{promptbox}
```

## Math Notation
* `math_commands.tex` (Deep Learning book notation) and `macro.tex` are loaded. Variables: italics ($a$, $\va$, $\mA$); sets: blackboard bold; `\E`, `\R`, `\KL`, `\Var`, `\argmax`, `\argmin`, `\Tr` are available.
* **Do NOT redefine existing macros** (no `\newcommand` for names that may already exist; it will break compilation). Avoid defining new macros altogether.
* **Pitfalls:** `\vs` is the bold vector $\bm{s}$ (write "vs." literally in text), and `\eps` is `\epsilon`. `\eg`, `\ie`, `\etal`, `\cf`, `\etc` are defined and already include a trailing space.

## Appendix
* Optional. Add it only if the experimental log contains material that does not fit in the main text (extra ablations, hyperparameters, prompts, qualitative examples).
* Place it after the bibliography:
    ```latex
    \clearpage
    \appendix
    \section{Additional Results}
    \label{sec:appendix_results}
    ```

## Important Rules
* **Compilation:** The report is compiled with `pdflatex` + `bibtex`. Make sure every `\begin{...}` has a matching `\end{...}` and all special characters (`%`, `&`, `_`, `#`) are escaped in text.
* **Formatting:** Do not change margins, font sizes, line spacing, or section formatting.
* **Author Information:** Always keep the author placeholder block from `template.tex` unchanged.
