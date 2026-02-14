#!/usr/bin/env python3
# Copyright 2026 Michael Schulte
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""microagent – a local-LLM powered command/script generator.

Talks to a local OpenAI-compatible chat-completion endpoint, processes the
response and hands the result back to a thin shell wrapper that owns the
interactive readline prompt.

Communication contract with the shell wrapper
----------------------------------------------
* **stdout** is reserved for machine-readable output that the shell wrapper
  consumes.  Human-readable chatter goes to **stderr** (always in verbose
  mode, selectively otherwise).
* When the result is a one-liner suitable for readline injection the script
  writes it to the file given via ``--prompt-file`` and prints it to stdout.
* When the result is a saved script it writes ``__SCRIPT__:<path>`` to stdout
  so the wrapper can offer to execute it.
* Exit codes:  0 = ok / one-liner,  2 = script(s) saved,  1 = error.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_HOST = "http://localhost:8280"
DEFAULT_MODEL = "default"
DEFAULT_TEMPERATURE = 0.1
DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 30
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_STARTUP_TIMEOUT = 50
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/scripts/microagent_out")

SHELL_MODES = {"bash", "sh", "zsh", "fish"}
CODE_MODES = {"python"}
ALL_MODES = SHELL_MODES | CODE_MODES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str, *, verbose: bool = True) -> None:
    """Print to stderr so stdout stays clean for the shell wrapper."""
    if verbose:
        print(msg, file=sys.stderr)


def detect_os() -> str:
    """Return a short OS description for the system prompt."""
    import platform

    parts: list[str] = [platform.system()]
    if platform.system() == "Linux":
        try:
            import distro  # type: ignore[import-untyped]

            parts.append(distro.name(pretty=True))
        except ImportError:
            release = Path("/etc/os-release")
            if release.exists():
                for line in release.read_text().splitlines():
                    if line.startswith("PRETTY_NAME="):
                        parts.append(line.split("=", 1)[1].strip('" '))
                        break
    parts.append(platform.machine())
    return " / ".join(parts)


def python_version_str() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}"


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------

def health_check(host: str, timeout: float = 0.5) -> bool:
    req = urllib.request.Request(f"{host}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def ensure_llm_running(
    host: str,
    starter: str | None,
    startup_timeout: float,
    verbose: bool,
) -> bool:
    """Return True when the LLM is reachable."""
    if health_check(host):
        return True

    if not starter:
        log("Error: LLM not reachable and no --starter provided.", verbose=True)
        return False

    log("Starting local LLM …", verbose=True)
    subprocess.Popen(
        [starter],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(5)

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if health_check(host, timeout=1.0):
            log("LLM is ready.", verbose=True)
            return True
        time.sleep(0.5)

    log(f"Error: LLM did not start within {startup_timeout}s.", verbose=True)
    return False


def build_system_prompt(mode: str, os_hint: str) -> str:
    os_part = f" on {os_hint}" if os_hint else ""

    if mode in SHELL_MODES:
        return (
            f"You are a {mode} expert{os_part}. "
            "Reply ONLY with the needed command(s), no explanation. "
            "Use variables only if necessary."
        )
    if mode == "python":
        return (
            f"You are a Python {python_version_str()} expert{os_part}. "
            "Reply ONLY with Python code. No explanation, no prose."
        )
    return f"You are a helpful coding assistant for {mode}{os_part}."


def query_llm(
    host: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout: float,
    retries: int,
    retry_delay: float,
    verbose: bool,
) -> str | None:
    """Send a chat-completion request; return the assistant content or None."""
    url = f"{host}/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }).encode()

    headers = {"Content-Type": "application/json"}

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            content: str | None = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content")
            )
            if verbose:
                log(f"[llm] raw response:\n{json.dumps(data, indent=2)}", verbose=True)
            return content
        except urllib.error.URLError as exc:
            log(f"[attempt {attempt}/{retries}] {exc}", verbose=verbose)
            if attempt < retries:
                time.sleep(retry_delay)

    log("Error: all retries exhausted.", verbose=True)
    return None


# ---------------------------------------------------------------------------
# Response processing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(
    r"```(?P<lang>\w+)?\s*\n(?P<code>.*?)```",
    re.DOTALL,
)


def extract_fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return list of (language_tag, code) from fenced code blocks."""
    return [(m.group("lang") or "", m.group("code")) for m in _FENCE_RE.finditer(text)]


def strip_fences(text: str) -> str:
    """Remove markdown fences and trim whitespace, just like the old sed chain."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        lines.append(line)

    # strip leading/trailing blank lines, trim each line
    result = "\n".join(l.rstrip() for l in lines).strip()
    return result


def is_oneliner(text: str) -> bool:
    """Heuristic: fits comfortably in a readline prompt."""
    return "\n" not in text and len(text) < 512


def extension_for_mode(mode: str) -> str:
    mapping: dict[str, str] = {
        "python": ".py",
        "bash": ".sh",
        "sh": ".sh",
        "zsh": ".zsh",
        "fish": ".fish",
    }
    return mapping.get(mode, ".txt")


def find_label(
    initial_label: str,
    content: str
) -> str:
    content = content.strip()
    try:
        tree = ast.parse(content)
        outermost_names: list[str] = []
        
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                outermost_names.append(node.name)
            elif isinstance(node, ast.ClassDef):
                outermost_names.append(node.name)
        
        # Filter out 'main' and get the last one
        filtered_names = [name for name in outermost_names if name != 'main']

        if len(filtered_names) > 0:    
            return filtered_names[-1] if filtered_names else None

    except SyntaxError as e:
        pass
    

    curtime = datetime.now(tz=timezone.utc).strftime("%H%M%S")
    return f"script_{curtime}" if initial_label == "script" else initial_label


def save_script(
    content: str,
    output_dir: Path,
    mode: str,
    label: str = "script",
    make_executable: bool = True,
) -> Path:
    """Save *content* into output_dir/YYYY-mm-dd/<label><ext>, return path."""
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    folder = output_dir / today
    folder.mkdir(parents=True, exist_ok=True)

    ext = extension_for_mode(mode)
    label = find_label(label, content)
    # find a non-colliding filename
    idx = 0
    while True:
        suffix = f"_{idx}" if idx else ""
        candidate = folder / f"{label}{suffix}{ext}"
        if not candidate.exists():
            break
        idx += 1

    candidate.write_text(content, encoding="utf-8")
    if make_executable and mode in SHELL_MODES:
        candidate.chmod(candidate.stat().st_mode | 0o755)

    return candidate


def display_code(path: Path, verbose: bool) -> None:
    """Pretty-print the saved file using bat (if available) or plain cat."""
    bat = shutil.which("bat") or shutil.which("batcat")
    if bat:
        subprocess.run([bat, "--paging=never", str(path)], check=False)
        log("\n")
    else:
        line = shutil.get_terminal_size().columns * "_"
        log(line)
        log(path.read_text(), verbose=True)
        log(f"\n{line}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="microagent",
        description="Query a local LLM for shell commands or code snippets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              microagent "list all docker containers sorted by size"
              microagent -m python "read a CSV and plot column 3"
              microagent -v -t 0.5 "find duplicate files in /data"
        """),
    )
    p.add_argument("task", nargs="+", help="Natural-language task description")

    # LLM connection
    g = p.add_argument_group("LLM connection")
    g.add_argument(
        "--host",
        default=os.environ.get("MICROAGENT_HOST", DEFAULT_HOST),
        help=f"LLM API base URL (env: MICROAGENT_HOST, default: {DEFAULT_HOST})",
    )
    g.add_argument(
        "--starter",
        default=os.environ.get("MICROAGENT_STARTER"),
        help="Script to start the LLM if it is not running (env: MICROAGENT_STARTER)",
    )
    g.add_argument(
        "--model",
        default=os.environ.get("MICROAGENT_MODEL", DEFAULT_MODEL),
        help=f"Model name (default: {DEFAULT_MODEL})",
    )

    # Generation
    g2 = p.add_argument_group("Generation")
    g2.add_argument(
        "-t", "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"Sampling temperature (default: {DEFAULT_TEMPERATURE})",
    )
    g2.add_argument(
        "-m", "--mode",
        choices=sorted(ALL_MODES),
        default="bash",
        help="Language / shell mode (default: bash)",
    )
    g2.add_argument(
        "--os",
        dest="os_hint",
        default=os.environ.get("MICROAGENT_OS", ""),
        help="OS context string for the system prompt (env: MICROAGENT_OS)",
    )
    g2.add_argument(
        "--system-prompt",
        default=None,
        help="Override the entire system prompt",
    )

    # Network / resilience
    g3 = p.add_argument_group("Network / resilience")
    g3.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    g3.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Max retries for the LLM call")
    g3.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY, help="Seconds between retries")
    g3.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT,
        help="Max seconds to wait for LLM startup",
    )

    # Output
    g4 = p.add_argument_group("Output")
    g4.add_argument(
        "-o", "--output",
        type=Path,
        default=Path(os.environ.get("MICROAGENT_OUTPUT", DEFAULT_OUTPUT_DIR)),
        help=f"Output folder for saved scripts (env: MICROAGENT_OUTPUT, default: {DEFAULT_OUTPUT_DIR})",
    )
    g4.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="File to write the one-liner into (for shell wrapper integration)",
    )
    g4.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Forward full LLM response and debug info to stderr",
    )
    g4.add_argument(
        "--raw",
        action="store_true",
        help="Print raw LLM output to stdout and exit (skip all processing)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    user_prompt: str = " ".join(args.task)
    verbose: bool = args.verbose

    # 1. Ensure LLM is available ----------------------------------------
    if not ensure_llm_running(args.host, args.starter, args.startup_timeout, verbose):
        return 1

    # 2. Build prompts --------------------------------------------------
    if args.os_hint == "" and os.environ.get("MICROAGENT_OS") is None:
        args.os_hint = detect_os()

    system_prompt: str = args.system_prompt or build_system_prompt(args.mode, args.os_hint)

    if args.mode == "python":
        user_prompt = f"In Python {python_version_str()}: {user_prompt}. Give me only the Python code use comprehensio, modern type hints and functions and call the entry function, no explanation."

    log(f"[system] {system_prompt}", verbose=verbose)
    log(f"[user]   {user_prompt}", verbose=verbose)

    # 3. Query LLM ------------------------------------------------------
    raw_content = query_llm(
        host=args.host,
        model=args.model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=args.temperature,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        verbose=verbose,
    )

    if raw_content is None:
        log("Error: no response from LLM.", verbose=True)
        return 1

    if args.raw:
        print(raw_content)
        return 0

    # 4. Process response -----------------------------------------------
    cleaned = strip_fences(raw_content)

    # Shell modes -------------------------------------------------------
    if is_oneliner(cleaned):
        # Write to prompt file for readline injection
        if args.prompt_file:
            args.prompt_file.write_text(cleaned, encoding="utf-8")
        print(cleaned)
        return 0

    # Multi-line: save as script
    p = save_script(
        cleaned,
        args.output,
        mode=args.mode,
        label="script",
        make_executable=True,
    )
    display_code(p, verbose=True)

    # For the shell wrapper: the "one-liner" to inject is executing the script
    exec_cmd = f"python3 {p}" if args.mode == "python" else str(p)
    if args.prompt_file:
        args.prompt_file.write_text(exec_cmd, encoding="utf-8")
    else:
        print(p)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
