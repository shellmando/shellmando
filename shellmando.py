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

"""shellmando – a local-LLM powered command/script generator.

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
import platform
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

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


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
DEFAULT_OUTPUT_DIR = os.path.expanduser("~/scripts/shellmando_out")
SHELLMANDO_DIR=os.environ.get("SHELLMANDO_DIR", os.path.curdir)

SHELL_MODES = {"bash", "sh", "zsh", "fish"}
CODE_MODES = {"python"}
NO_MODES = {"none"}
ALL_MODES = SHELL_MODES | CODE_MODES | NO_MODES

_CONFIG_SEARCH_PATHS: list[Path] = [
    Path.home() / ".config" / "shellmando" / "config.toml",
    Path(__file__).resolve().parent / "shellmando.toml",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _find_config(explicit: str | None = None) -> Path | None:
    """Locate the first existing config file.

    Search order:
      1. *explicit* path (from ``--config`` or ``SHELLMANDO_CONFIG``)
      2. ``~/.config/shellmando/config.toml``
      3. ``<script_dir>/shellmando.toml``
    """
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _deep_get(d: dict, *keys, default=None):
    """Safely traverse nested dicts: ``_deep_get(d, 'a', 'b')`` → ``d['a']['b']``."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def load_config(path: Path | None) -> dict:
    """Load and return the TOML config, or an empty dict on failure."""
    if path is None or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        print(f"Warning: failed to load config {path}: {exc}", file=sys.stderr)
        return {}


def expand_prompt_template(template: str, variables: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders in *template*.

    Unknown placeholders are left as-is so partial templates are safe.
    """
    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    return template.format_map(SafeDict(variables))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str, *, verbose: bool = True) -> None:
    """Print to stderr so stdout stays clean for the shell wrapper."""
    if verbose:
        print(msg, file=sys.stderr)


def detect_os() -> str:
    """Return a short OS description for the system prompt."""
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


def _detect_clipboard_cmd() -> list[str] | None:
    """Find a clipboard copy command available on this system."""
    if platform.system() == "Darwin":
        if shutil.which("pbcopy"):
            return ["pbcopy"]
    elif os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("wl-copy"):
            return ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the system clipboard.  Return True on success."""
    cmd = _detect_clipboard_cmd()
    if cmd is None:
        return False
    try:
        proc = subprocess.run(
            cmd, input=text.encode(), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except (OSError, subprocess.CalledProcessError):
        return False


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


def _prompt_variables(mode: str, os_hint: str) -> dict[str, str]:
    """Build the dict of variables available for prompt templates."""
    return {
        "mode": mode,
        "os": os_hint,
        "python_version": python_version_str(),
    }


def build_system_prompt(mode: str, os_hint: str, snippet: bool, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    tvars = _prompt_variables(mode, os_hint)

    # Try config-defined template first
    if mode in SHELL_MODES:
        tpl = _deep_get(cfg, "prompts", "shell", "system", default=None)
        if tpl:
            return expand_prompt_template(tpl, tvars)
        os_part = f" on {os_hint}" if os_hint else ""
        return (
            f"You are a {mode} expert{os_part}. "
            "Reply ONLY with the needed command(s), no explanation. "
            "Use variables only if necessary."
        )

    if mode == "python":
        tpl = _deep_get(cfg, "prompts", "python", "system", default=None)
        res = ""
        if tpl:
            res = expand_prompt_template(tpl, tvars)
        return (res +
            "Use a simple snippet with no functions if possible. " if snippet else "Use functions and call the entry function. "
        )

    return ""


def build_user_prompt(user_prompt: str, mode: str, os_hint: str, cfg: dict | None = None) -> str:
    """Apply config-driven prefix/suffix to the user prompt."""
    cfg = cfg or {}
    tvars = _prompt_variables(mode, os_hint)

    if mode in SHELL_MODES:
        prefix = _deep_get(cfg, "prompts", "shell", "user_prefix", default="")
        suffix = _deep_get(cfg, "prompts", "shell", "user_suffix", default="")
    elif mode == "python":
        prefix = _deep_get(cfg, "prompts", "python", "user_prefix", default="")
        suffix = _deep_get(cfg, "prompts", "python", "user_suffix", default="")
    else:
        prefix, suffix = "", ""

    if prefix:
        prefix = expand_prompt_template(prefix, tvars)
    if suffix:
        suffix = expand_prompt_template(suffix, tvars)

    return f"{prefix}{user_prompt}{suffix}"


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
    line = shutil.get_terminal_size().columns * "_"
    if bat:
        subprocess.run([bat, "--paging=never", "--style=-numbers", str(path)], check=False)
        log("\n")
    else:
        line = shutil.get_terminal_size().columns * "_"
        log(line)
        log(path.read_text(), verbose=True)
        log(f"\n{line}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _pre_parse_config(argv: list[str] | None) -> tuple[Path | None, dict]:
    """Extract ``--config`` from *argv* before argparse runs, load the file."""
    args = argv if argv is not None else sys.argv[1:]
    config_path_str: str | None = None
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            config_path_str = args[i + 1]
            break
        if arg.startswith("--config="):
            config_path_str = arg.split("=", 1)[1]
            break

    if config_path_str is None:
        config_path_str = os.environ.get("SHELLMANDO_CONFIG")

    cfg_path = _find_config(config_path_str)
    cfg = load_config(cfg_path)
    return cfg_path, cfg


def build_parser(cfg: dict | None = None) -> argparse.ArgumentParser:
    if cfg is None:
        cfg = {}

    # Helper: resolve a setting with precedence env > config > hardcoded default
    def _resolve(env_key: str | None, *cfg_keys: str, default):
        if env_key:
            env_val = os.environ.get(env_key)
            if env_val is not None:
                return env_val
        cfg_val = _deep_get(cfg, *cfg_keys, default=None)
        if cfg_val is not None:
            return cfg_val
        return default

    p = argparse.ArgumentParser(
        prog="shellmando",
        description="Query a local LLM for shell commands or code snippets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              shellmando "list all docker containers sorted by size"
              shellmando -m python "read a CSV and plot column 3"
              shellmando -v -t 0.5 "find duplicate files in /data"
        """),
    )
    p.add_argument("task", nargs="+", help="Natural-language task description")

    # Config file
    p.add_argument(
        "--config",
        default=None,
        help="Path to TOML config file (env: SHELLMANDO_CONFIG)",
    )

    # LLM connection
    g = p.add_argument_group("LLM connection")
    g.add_argument(
        "--host",
        default=_resolve("SHELLMANDO_HOST", "llm", "host", default=DEFAULT_HOST),
        help=f"LLM API base URL (env: SHELLMANDO_HOST, default: {DEFAULT_HOST})",
    )
    g.add_argument(
        "--starter",
        default=_resolve("SHELLMANDO_LLM_STARTER", "llm", "starter", default=None),
        help="Script to start the LLM if it is not running (env: SHELLMANDO_LLM_STARTER)",
    )
    g.add_argument(
        "--model",
        default=_resolve("SHELLMANDO_MODEL", "llm", "model", default=DEFAULT_MODEL),
        help=f"Model name (default: {DEFAULT_MODEL})",
    )

    # Generation
    g2 = p.add_argument_group("Generation")
    g2.add_argument(
        "-t", "--temperature",
        type=float,
        default=float(_resolve(None, "generation", "temperature", default=DEFAULT_TEMPERATURE)),
        help=f"Sampling temperature (default: {DEFAULT_TEMPERATURE})",
    )
    g2.add_argument(
        "-m", "--mode",
        choices=sorted(ALL_MODES),
        default=_resolve(None, "generation", "mode", default="bash"),
        help="Language / shell mode (default: bash)",
    )
    g2.add_argument(
        "--os",
        dest="os_hint",
        default=_resolve("SHELLMANDO_OS", "generation", "os", default=""),
        help="OS context string for the system prompt (env: SHELLMANDO_OS)",
    )
    g2.add_argument(
        "--system-prompt",
        default=None,
        help="Override the entire system prompt",
    )

    # Network / resilience
    g3 = p.add_argument_group("Network / resilience")
    g3.add_argument(
        "--timeout", type=float,
        default=float(_resolve(None, "network", "timeout", default=DEFAULT_TIMEOUT)),
        help="HTTP timeout in seconds",
    )
    g3.add_argument(
        "--retries", type=int,
        default=int(_resolve(None, "network", "retries", default=DEFAULT_RETRIES)),
        help="Max retries for the LLM call",
    )
    g3.add_argument(
        "--retry-delay", type=float,
        default=float(_resolve(None, "network", "retry_delay", default=DEFAULT_RETRY_DELAY)),
        help="Seconds between retries",
    )
    g3.add_argument(
        "--startup-timeout", type=float,
        default=float(_resolve(None, "network", "startup_timeout", default=DEFAULT_STARTUP_TIMEOUT)),
        help="Max seconds to wait for LLM startup",
    )

    # Output
    g4 = p.add_argument_group("Output")
    g4.add_argument(
        "-o", "--output",
        type=Path,
        default=Path(os.path.expanduser(
            _resolve("SHELLMANDO_OUTPUT", "output", "dir", default=DEFAULT_OUTPUT_DIR)
        )),
        help=f"Output folder for saved scripts (env: SHELLMANDO_OUTPUT, default: {DEFAULT_OUTPUT_DIR})",
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
        "-j", "--justanswer",
        action="store_true",
        help="Just answer the question: print the LLM reply and exit "
             "(no prompt adaptation, no file generated)",
    )
    g4.add_argument(
        "--raw",
        action="store_true",
        help="Print raw LLM output to stdout and exit (skip all processing)",
    )
    g4.add_argument(
        "-s", "--snippet",
        action="store_true",
        help="Generate snippet only: display, copy to clipboard, no file saved",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    cfg_path, cfg = _pre_parse_config(argv)
    parser = build_parser(cfg)
    args = parser.parse_args(argv)

    user_prompt: str = " ".join(args.task)
    verbose: bool = args.verbose

    if cfg_path and verbose:
        log(f"[config] loaded {cfg_path}", verbose=True)

    # Expand ~ in starter path from config
    if args.starter:
        args.starter = os.path.expanduser(args.starter)

    # 1. Ensure LLM is available ----------------------------------------
    if not ensure_llm_running(args.host, args.starter, args.startup_timeout, verbose):
        return 1

    # 2. Build prompts --------------------------------------------------
    if args.os_hint == "" and os.environ.get("SHELLMANDO_OS") is None:
        args.os_hint = detect_os()

    if args.justanswer:
        system_prompt = args.system_prompt or "You are a helpful assistant."
    else:
        args.system_prompt or build_system_prompt(args.mode, args.os_hint, args.snippet, cfg)

    if not args.justanswer and args.mode == "python":
        user_prompt = f"In Python {python_version_str()}: {user_prompt}. Give me only the Python code use comprehensio, modern type hints and functions and call the entry function, no explanation."

    # Apply config-driven user prompt prefix/suffix (Python mode uses
    # config templates instead of hardcoded wrapping when available)
    has_python_prompt_cfg = (
        _deep_get(cfg, "prompts", "python", "user_prefix", default=None) is not None
        or _deep_get(cfg, "prompts", "python", "user_suffix", default=None) is not None
    )
    if args.mode == "python" and has_python_prompt_cfg:
        user_prompt = build_user_prompt(user_prompt, args.mode, args.os_hint, cfg)
    elif args.mode == "python":
        # Fallback to original hardcoded behaviour
        user_prompt = f"In Python {python_version_str()}: {user_prompt}"
    elif args.mode in SHELL_MODES:
        user_prompt = build_user_prompt(user_prompt, args.mode, args.os_hint, cfg)

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

    if args.justanswer:
        print(strip_fences(raw_content))
        return 0

    # 4. Process response -----------------------------------------------
    cleaned = strip_fences(raw_content)

    # Snippet mode: display + clipboard, nothing else -------------------
    if args.snippet:
        line='_' * shutil.get_terminal_size().columns
        log(line)
        log(cleaned, verbose=True)
        log(line)
        if copy_to_clipboard(cleaned):
            log("  >> copied to clipboard", verbose=True)
        else:
            log("  >> clipboard not available – use the output above", verbose=True)
            if platform.system() == "Linux":
                log(" >> suggest installing xclip: sudo apt install xclip")
        return 0

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
    try:
        p = p.relative_to(SHELLMANDO_DIR)
    except:
        pass
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
