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

"""Claude backend that runs every LLM call through the Claude Code CLI (`claude -p`).

This lets the pipeline run on a Claude subscription without any API key. It is
selected with `--backend claude` (which sets PAPER_ORCHESTRA_BACKEND=claude) or by
passing a model name that starts with "claude".

Each call starts an isolated, tool-less Claude Code process:
  * `--safe-mode`            : no CLAUDE.md, hooks, MCP servers, skills or plugins
  * `--system-prompt-file`   : replaces the Claude Code system prompt
  * `--tools ""`             : no tools, unless web search is requested
  * stream-json input        : text, images and PDFs are sent as content blocks

Environment variables (all optional):
  PAPER_ORCHESTRA_BACKEND      "claude" routes every Gemini call to Claude.
  PO_CLAUDE_MODEL              Model for regular calls (default: claude-opus-5-5).
  PO_CLAUDE_FAST_MODEL         Model for calls hardcoded to Gemini Flash
                               (default: claude-sonnet-5-5).
  PO_CLAUDE_EFFORT             Passed to `--effort` (low|medium|high|xhigh|max).
  PO_CLAUDE_TIMEOUT            Seconds per call (default: 1800).
  PO_CLAUDE_MAX_OUTPUT_TOKENS  Max output tokens per call (default: 64000).
  PO_CLAUDE_BIN                Path to the claude executable (default: claude).
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_MAIN_MODEL = "claude-opus-5-5"
DEFAULT_FAST_MODEL = "claude-sonnet-5-5"
CLAUDE_MODEL_ALIASES = {"opus", "sonnet", "haiku", "fable", "default"}

DEFAULT_SYSTEM_PROMPT = (
    "You are an expert AI research assistant. Follow the user's instructions "
    "exactly and respond only in the requested output format."
)

# Anthropic image limits: 5MB per image (base64) and 8000px per side.
_MAX_IMAGE_BYTES = 3_700_000
_MAX_IMAGE_SIDE = 7_900
_RESIZE_TARGET_SIDE = 2_000


def get_backend() -> str:
    return os.getenv("PAPER_ORCHESTRA_BACKEND", "gemini").strip().lower()


def is_claude_model(model_name: Optional[str]) -> bool:
    if not model_name:
        return False
    name = model_name.strip().lower()
    return name.startswith("claude") or name in CLAUDE_MODEL_ALIASES


def use_claude_backend(model_name: Optional[str] = None) -> bool:
    return get_backend() == "claude" or is_claude_model(model_name)


def resolve_claude_model(model_name: Optional[str]) -> str:
    """Maps the requested model to a Claude model.

    Claude model names are passed through. Gemini names that leak in from
    hardcoded defaults are mapped: "flash"/"lite" models to the fast model,
    everything else to the main model.
    """
    if is_claude_model(model_name):
        return model_name.strip()
    name = (model_name or "").lower()
    if "flash" in name or "lite" in name:
        return os.getenv("PO_CLAUDE_FAST_MODEL", DEFAULT_FAST_MODEL)
    return os.getenv("PO_CLAUDE_MODEL", DEFAULT_MAIN_MODEL)


def _sniff_image_media_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _shrink_image_if_needed(data: bytes, media_type: str) -> Tuple[bytes, str]:
    """Downscales images that exceed the API size limits."""
    try:
        from PIL import Image
    except ImportError:
        return data, media_type

    try:
        img = Image.open(io.BytesIO(data))
        too_large = len(data) > _MAX_IMAGE_BYTES or max(img.size) > _MAX_IMAGE_SIDE
        if not too_large:
            return data, media_type
        img.thumbnail((_RESIZE_TARGET_SIDE, _RESIZE_TARGET_SIDE))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"Warning: could not check/resize image for Claude: {e}")
        return data, media_type


def _binary_block(data: bytes, mime_type: Optional[str]) -> Dict[str, Any]:
    mime_type = (mime_type or "").lower()
    if mime_type == "application/pdf" or data.startswith(b"%PDF"):
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.b64encode(data).decode("utf-8"),
            },
        }

    # Callers sometimes label JPEGs as PNG; the API rejects mismatches.
    media_type = _sniff_image_media_type(data) or mime_type or "image/png"
    if not media_type.startswith("image/"):
        raise ValueError(f"Unsupported content type for Claude backend: {mime_type}")
    data, media_type = _shrink_image_if_needed(data, media_type)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("utf-8"),
        },
    }


def _part_to_blocks(part: Any) -> List[Dict[str, Any]]:
    """Converts a str / google.genai Part / Content / dict into Claude content blocks."""
    if part is None:
        return []
    if isinstance(part, str):
        return [{"type": "text", "text": part}] if part else []
    if isinstance(part, (list, tuple)):
        blocks = []
        for p in part:
            blocks.extend(_part_to_blocks(p))
        return blocks

    # google.genai types.Content
    parts = getattr(part, "parts", None)
    if parts is not None and not hasattr(part, "inline_data"):
        return _part_to_blocks(list(parts))

    # google.genai types.Part
    text = getattr(part, "text", None)
    if text:
        return [{"type": "text", "text": text}]
    inline_data = getattr(part, "inline_data", None)
    if inline_data is not None and getattr(inline_data, "data", None):
        return [_binary_block(inline_data.data, getattr(inline_data, "mime_type", None))]

    if isinstance(part, dict) and "type" in part:
        return [part]

    raise ValueError(f"Unsupported content part for Claude backend: {type(part)}")


def _has_google_search_tool(generation_configs: Dict[str, Any]) -> bool:
    for tool in generation_configs.get("tools") or []:
        if getattr(tool, "google_search", None) is not None:
            return True
        if isinstance(tool, dict) and "google_search" in tool:
            return True
    return False


def _system_instruction_to_text(system_instruction: Any) -> Optional[str]:
    if system_instruction is None:
        return None
    if isinstance(system_instruction, str):
        return system_instruction
    texts = [b["text"] for b in _part_to_blocks(system_instruction) if b.get("type") == "text"]
    return "\n\n".join(texts) if texts else None


def _run_claude_cli(
    content_blocks: List[Dict[str, Any]],
    model: str,
    system_prompt: str,
    enable_web_search: bool,
) -> str:
    claude_bin = os.getenv("PO_CLAUDE_BIN", "claude")
    if shutil.which(claude_bin) is None:
        raise RuntimeError(
            f"Claude Code CLI '{claude_bin}' not found. Install Claude Code and log in, "
            "or set PO_CLAUDE_BIN to its path."
        )

    timeout = int(os.getenv("PO_CLAUDE_TIMEOUT", "1800"))
    env = dict(os.environ)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.getenv("PO_CLAUDE_MAX_OUTPUT_TOKENS", "64000")

    # Run in an empty scratch directory so no project files or settings are picked up.
    with tempfile.TemporaryDirectory(prefix="po_claude_") as work_dir:
        system_prompt_path = os.path.join(work_dir, "system_prompt.txt")
        with open(system_prompt_path, "w", encoding="utf-8") as f:
            f.write(system_prompt)

        cmd = [
            claude_bin,
            "-p",
            "--safe-mode",
            "--no-session-persistence",
            "--model",
            model,
            "--system-prompt-file",
            system_prompt_path,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if enable_web_search:
            cmd += ["--tools", "WebSearch", "--allowedTools", "WebSearch"]
        else:
            cmd += ["--tools", ""]
        effort = os.getenv("PO_CLAUDE_EFFORT")
        if effort:
            cmd += ["--effort", effort]

        message = {
            "type": "user",
            "message": {"role": "user", "content": content_blocks},
        }
        proc = subprocess.run(
            cmd,
            input=json.dumps(message) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=work_dir,
            env=env,
        )

    result_event = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result_event = event

    if result_event is None:
        raise RuntimeError(
            f"claude -p returned no result (exit code {proc.returncode}). "
            f"stderr: {proc.stderr.strip()[-2000:]}"
        )
    if result_event.get("is_error") or result_event.get("subtype") != "success":
        raise RuntimeError(
            f"claude -p failed ({result_event.get('subtype')}, "
            f"api_error_status={result_event.get('api_error_status')}): "
            f"{str(result_event.get('result'))[:2000]}"
        )
    if result_event.get("stop_reason") == "max_tokens":
        raise RuntimeError(
            "claude -p output was truncated (stop_reason=max_tokens). "
            "Increase PO_CLAUDE_MAX_OUTPUT_TOKENS."
        )

    return result_event.get("result") or ""


def call_claude_with_contents(
    contents: Any,
    model_name: str,
    max_retries: int = 5,
    base_interval_sec: int = 5,
    result_parsing_func: Callable = None,
    generation_configs: Optional[dict] = None,
    check_parsed_response_not_none: bool = True,
) -> Dict[str, Any]:
    """Drop-in replacement for `call_gemini_with_contents` using `claude -p`.

    Supported generation_configs keys: `system_instruction` and `tools`
    (a Google Search tool enables Claude's WebSearch). Sampling options such as
    `temperature` cannot be set through the CLI and are ignored.
    """
    generation_configs = generation_configs or {}
    model = resolve_claude_model(model_name)
    system_prompt = (
        _system_instruction_to_text(generation_configs.get("system_instruction"))
        or DEFAULT_SYSTEM_PROMPT
    )
    enable_web_search = _has_google_search_tool(generation_configs)
    content_blocks = _part_to_blocks(contents)
    if not content_blocks:
        raise ValueError("Empty contents passed to the Claude backend.")

    raw_response = None
    parsed_response = None
    for attempt in range(max_retries):
        try:
            raw_response = _run_claude_cli(
                content_blocks=content_blocks,
                model=model,
                system_prompt=system_prompt,
                enable_web_search=enable_web_search,
            )
            if not raw_response:
                raise ValueError("Empty text response from claude -p")

            parsed_response = (
                result_parsing_func(raw_response) if result_parsing_func else raw_response
            )
            if check_parsed_response_not_none:
                assert (
                    parsed_response is not None
                ), f"Parsed response is None, Here is the raw response:\n{raw_response}"

            return {"raw_response": raw_response, "parsed_response": parsed_response}

        except Exception as e:
            print(
                f"Warning: Claude ({model}) call failed on attempt {attempt + 1} of "
                f"{max_retries}. Error: {e}"
            )
            if "prompt is too long" in str(e).lower():
                print("Detected input token count exceed. Not retrying.")
                raise e
            if "safeguards flagged" in str(e).lower():
                # Deterministic for the same input: retrying only burns usage.
                print(
                    "Claude's safeguards rejected this prompt. Not retrying. "
                    "Rephrase the prompt or use another model for this step."
                )
                raise e
            if attempt + 1 == max_retries:
                print(f"Error: All {max_retries} retry attempts failed.")
                raise e
            delay = base_interval_sec * (2 ** (attempt - 1)) if attempt > 0 else 0
            print(f"Retrying in {delay} seconds...")
            if delay > 0:
                time.sleep(delay)

    return {"raw_response": raw_response, "parsed_response": parsed_response}
