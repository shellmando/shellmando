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
import contextlib
import curses
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
import difflib
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
DEFAULT_TIMEOUT = 180
DEFAULT_STARTUP_TIMEOUT = 120
DEFAULT_RETRIES = 30
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
DEFAULT_DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
DEFAULT_OUTPUT_DIR = os.path.join(DEFAULT_DATA_HOME, "shellmando")
SHELLMANDO_DIR = os.environ.get("SHELLMANDO_DIR", os.path.curdir)

SHELL_MODES = {"bash", "sh", "zsh", "fish"}
CODE_MODES = {"python"}
NO_MODES = {"none"}
ASSISTANT_MODES = {"assistant"}
ALL_MODES = SHELL_MODES | CODE_MODES | NO_MODES | ASSISTANT_MODES

CLARIFY_SYSTEM_PROMPT = (
    "You are a senior engineer. "
    "Break down the user's task into sub-tasks and check if the given task and information "
    "is clear or if information is missing, "
    " _especially_ for input-data, their structures, error handling and the presented outputs. "
    "If no information is missing reply ONLY: CLEAR. "
    "Otherwise with sub-tasks as topics and 2-7 options to satisfy the missing information "
    "reply ONLY with lines in this exact format:\n"
    "A: <topic> [<option1> || <option2> || ...]\n"
)


CLARIFY_SYSTEM_PROMPT = CLARIFY_SYSTEM_PROMPT

_CONFIG_SEARCH_PATHS: list[Path] = [
    Path(DEFAULT_CONFIG_HOME) / "shellmando" / "config.toml",
    Path(__file__).resolve().parent / "shellmando.toml",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _find_config(explicit: str | None = None) -> Path | None:
    """Locate the first existing config file.

    Search order:
      1. *explicit* path (from ``--config`` or ``SHELLMANDO_CONFIG``)
      2. ``$XDG_CONFIG_HOME/shellmando/config.toml``  (default: ``~/.config``)
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


def _toml_value(v) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, float):
        # Avoid scientific notation for typical config values
        return f"{v:g}"
    return str(v)


def _serialize_toml(cfg: dict) -> str:
    """Serialize a two-level nested dict to a TOML string."""
    lines = ["# shellmando configuration", "# Generated by --defaults", ""]
    for section, values in cfg.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


# Mapping: argparse dest -> (toml_section, toml_key, type_fn)
_ARG_TO_CONFIG: dict[str, tuple[str, str, type]] = {
    "host":            ("llm",        "host",            str),
    "model":           ("llm",        "model",           str),
    "starter":         ("llm",        "starter",         str),
    "mode":            ("generation", "mode",            str),
    "temperature":     ("generation", "temperature",     float),
    "os_hint":         ("generation", "os",              str),
    "timeout":         ("network",    "timeout",         float),
    "startup_timeout": ("network",    "startup_timeout", float),
    "retries":         ("network",    "retries",         int),
    "retry_delay":     ("network",    "retry_delay",     float),
    "output":          ("output",     "dir",             str),
}


def _write_defaults(
    cfg_path: Path | None,
    existing_cfg: dict,
    argv: list[str],
) -> int:
    """Parse *argv* for config-settable args, merge into *existing_cfg*, write file.

    Returns 0 on success, 1 on error.
    """
    # Build a minimal parser with all defaults=None so we can detect which
    # args were explicitly supplied by the user.
    detector = argparse.ArgumentParser(add_help=False)
    detector.add_argument("task", nargs="*")
    detector.add_argument("--config", default=None)
    detector.add_argument("--host", default=None)
    detector.add_argument("--starter", default=None)
    detector.add_argument("--model", default=None)
    detector.add_argument("-t", "--temperature", type=float, default=None)
    detector.add_argument("-m", "--mode", default=None)
    detector.add_argument("--os", dest="os_hint", default=None)
    detector.add_argument("--timeout", type=float, default=None)
    detector.add_argument("--retries", type=int, default=None)
    detector.add_argument("--retry-delay", type=float, default=None)
    detector.add_argument("--startup-timeout", type=float, default=None)
    detector.add_argument("-o", "--output", type=str, default=None)
    parsed, _ = detector.parse_known_args(argv)

    # Collect only the args that were explicitly set (non-None)
    updates: dict[str, dict] = {}
    for dest, (section, key, type_fn) in _ARG_TO_CONFIG.items():
        val = getattr(parsed, dest, None)
        if val is None:
            continue
        # Resolve mode prefix so we store the full name
        if dest == "mode":
            val = resolve_mode_interactive(val)
        updates.setdefault(section, {})[key] = type_fn(val)

    if not updates:
        print("No settable parameters provided; nothing written.", file=sys.stderr)
        return 0

    # Deep-merge updates into existing config
    merged: dict = {}
    for section in ["llm", "generation", "network", "output"]:
        merged[section] = dict(existing_cfg.get(section, {}))
    for section, kvs in updates.items():
        merged.setdefault(section, {}).update(kvs)

    # Determine target path
    target = cfg_path
    if target is None:
        target = _CONFIG_SEARCH_PATHS[0]

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_serialize_toml(merged), encoding="utf-8")
    except OSError as exc:
        print(f"Error: could not write config to {target}: {exc}", file=sys.stderr)
        return 1

    print(f"Defaults written to {target}:", file=sys.stderr)
    for section, kvs in updates.items():
        for key, val in kvs.items():
            print(f"  [{section}] {key} = {_toml_value(val)}", file=sys.stderr)
    return 0


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


@contextlib.contextmanager
def _alternate_screen():
    """Stream LLM output in the terminal's alternate screen buffer.

    On entry the terminal switches to the alternate screen (smcup); on exit it
    returns to the main screen (rmcup), which discards everything written
    during streaming — including content that scrolled off the top — without
    any line-counting heuristics.  Falls back to a no-op when stderr is not a
    tty or the terminal lacks alternate-screen support.
    """
    if not sys.stderr.isatty():
        yield
        return
    smcup = rmcup = None
    try:
        curses.setupterm()
        smcup = curses.tigetstr("smcup")
        rmcup = curses.tigetstr("rmcup")
    except Exception:
        pass
    if smcup:
        sys.stderr.buffer.write(smcup)
        sys.stderr.flush()
    try:
        yield
    finally:
        if rmcup:
            sys.stderr.buffer.write(rmcup)
            sys.stderr.flush()


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
            cmd,
            input=text.encode(),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except (OSError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------


def health_check(host: str, timeout: float = 0.5) -> tuple[bool, bool]:
    try:
        req = urllib.request.Request(f"{host}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True, True  # running, llama_server
    except Exception:
        req = urllib.request.Request(f"{host}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return True, False  # running, ollama
        except Exception:
            return False, False


def _has_systemd() -> bool:
    """Return True if a systemd user session is available."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "status"],
            capture_output=True,
            timeout=5,
        )
        # Exit code 0 = running units exist; 3 = no units loaded but systemd is present
        return result.returncode in (0, 3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_shell_profile() -> Path:
    """Return the path of the user's interactive shell profile."""
    shell = os.path.basename(os.environ.get("SHELL", "bash"))
    if shell == "zsh":
        zdotdir = os.environ.get("ZDOTDIR", str(Path.home()))
        return Path(zdotdir) / ".zshrc"
    if shell == "bash":
        bashrc = Path.home() / ".bashrc"
        if bashrc.exists():
            return bashrc
        return Path.home() / ".bash_profile"
    return Path.home() / ".profile"


_AUTOSTART_MARKER = "# shellmando-autostart"
_SYSTEMD_SERVICE_NAME = "shellmando-llm.service"


def _setup_profile_autostart(enable: bool, start_llm: str) -> int:
    """Add or remove the autostart line from the user's shell profile."""
    profile = _detect_shell_profile()
    autostart_line = f"{start_llm} {_AUTOSTART_MARKER}"

    if enable:
        content = profile.read_text(encoding="utf-8") if profile.exists() else ""
        if _AUTOSTART_MARKER in content:
            print(f"Autostart already enabled in {profile}", file=sys.stderr)
            return 0
        with profile.open("a", encoding="utf-8") as f:
            f.write(f"\n{autostart_line}\n")
        print(f"Autostart enabled: added to {profile}", file=sys.stderr)
    else:
        if not profile.exists():
            print("No shell profile found; nothing to remove.", file=sys.stderr)
            return 0
        content = profile.read_text(encoding="utf-8")
        if _AUTOSTART_MARKER not in content:
            print("Autostart not enabled in shell profile; nothing to remove.", file=sys.stderr)
            return 0
        lines = [ln for ln in content.splitlines(keepends=True) if _AUTOSTART_MARKER not in ln]
        profile.write_text("".join(lines), encoding="utf-8")
        print(f"Autostart disabled: removed from {profile}", file=sys.stderr)
    return 0


def _setup_systemd_autostart(enable: bool, start_llm: str) -> int:
    """Create/enable or disable/remove a systemd user service for LLM autostart."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_file = service_dir / _SYSTEMD_SERVICE_NAME

    if enable:
        service_dir.mkdir(parents=True, exist_ok=True)
                
        extra_path=[]
        for app in ["llama-server", "ollama"]:
            if app_path := shutil.which(app):
                extra_path.append(str(Path(os.path.expanduser(app_path)).parent))
        extra_path.append("$PATH")
        service_script = service_dir / "start_llm.sh"
        service_script_content = textwrap.dedent(f"""\
            #!/bin/bash
            PATH={":".join(extra_path)} {start_llm}
        """)
        service_script.write_text(service_script_content, encoding="utf-8")
        os.chmod(service_script, 0o755)

        service_content = textwrap.dedent(f"""\
            [Unit]
            Description=Start local LLM for shellmando
            After=network.target

            [Service]
            Type=oneshot
            ExecStart={service_script}
            RemainAfterExit=yes

            [Install]
            WantedBy=default.target
        """)
        service_file.write_text(service_content, encoding="utf-8")
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", _SYSTEMD_SERVICE_NAME], check=True, capture_output=True)
            subprocess.run(["systemctl", "--user", "start", _SYSTEMD_SERVICE_NAME], check=False, capture_output=True)
        except subprocess.CalledProcessError as exc:
            print(f"Error: systemctl failed: {exc}", file=sys.stderr)
            return 1
        print("Autostart enabled via systemd user service.", file=sys.stderr)
        print(f"  Service file: {service_file}", file=sys.stderr)
        print("  To check status: systemctl --user status shellmando-llm", file=sys.stderr)
    else:
        if not service_file.exists():
            print("Systemd service not found; nothing to remove.", file=sys.stderr)
            return 0
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _SYSTEMD_SERVICE_NAME],
            check=False, capture_output=True,
        )
        service_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        print("Autostart disabled: systemd service removed.", file=sys.stderr)
    return 0


def handle_autostart(enable: bool, start_llm: str) -> int:
    """Enable or disable LLM autostart on login."""
    if _has_systemd():
        return _setup_systemd_autostart(enable, start_llm)
    return _setup_profile_autostart(enable, start_llm)


def ensure_llm_running(
    host: str,
    starter: str | None,
    startup_timeout: float,
    verbose: bool,
    use_starter: bool
) -> tuple[bool, bool]:
    """Return True when the LLM is reachable."""
    running, llama_server = health_check(host)
    if running:
        return running, llama_server

    if not starter:
        log("Error: LLM not reachable and no --starter provided.", verbose=True)
        return False, False

    cmd = os.path.expanduser(starter)
    log(f"Starting local LLM using {cmd}", verbose=True)
    subprocess.Popen(starter)
    if not use_starter:
        time.sleep(1)

        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            running, llama_server = health_check(host, timeout=1.0)
            if running:
                log("LLM is ready.", verbose=verbose)
                return True, llama_server
            time.sleep(0.5)

        log(f"Error: LLM did not start within {startup_timeout}s.", verbose=True)
    return False, False


def build_system_prompt(mode: str, os_hint: str, snippet: bool, file_output: bool) -> str:

    # Try config-defined template first
    if mode in SHELL_MODES:
        os_part = f" on {os_hint}" if os_hint else ""
        instruction = (
            f"You are a {mode} expert {os_part}."
            " Reply ONLY with the needed command(s), NO explanation."
            " Use variables only if necessary."
        )

    elif mode == "python":
        add_function = (
            " Create well named functions with at most 7 lines. Include an if __name__ == '__main__'."
            if not snippet and not file_output
            else ""
        )
        add_snippet = " Use a simple snippet with no functions if possible." if snippet else ""
        python_instructions = (
            f"Reply ONLY with Python (>= {python_version_str()}) code."
            + " NO explanation, no prose."
            + " Style: use comprehension instead of loops, modern type hints."
        )
        instruction = f"{python_instructions}{add_snippet}{add_function}"

    elif mode == "assistant":
        instruction = "You are a helpful assistant. Keep your answer short. Show only the best option."

    else:
        instruction = "You are a helpful assistant."

    return instruction


def resolve_mode(value: str) -> str | None:
    """Resolve a mode prefix to a full mode name.

    Returns the resolved mode name, or None if the prefix is ambiguous.
    Raises SystemExit if the prefix matches nothing.
    """
    if value in ALL_MODES:
        return value
    matches = sorted(m for m in ALL_MODES if m.startswith(value))
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        log(f"Error: unknown mode '{value}'. Available modes: {', '.join(sorted(ALL_MODES))}", verbose=True)
        raise SystemExit(1)
    return None  # ambiguous


def resolve_mode_interactive(value: str) -> str:
    """Resolve a mode prefix interactively when ambiguous."""
    resolved = resolve_mode(value)
    if resolved is not None:
        return resolved
    matches = sorted(m for m in ALL_MODES if m.startswith(value))
    sys.stderr.write(f"Ambiguous mode '{value}'. Please choose:\n")
    for i, m in enumerate(matches, 1):
        sys.stderr.write(f"  {i}) {m}\n")
    sys.stderr.flush()
    while True:
        try:
            choice = input("Enter number: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(matches):
            return matches[int(choice) - 1]
        sys.stderr.write(f"Please enter a number between 1 and {len(matches)}.\n")
        sys.stderr.flush()


def _print_banner(user_prompt: str) -> None:
    isatty = sys.stderr.isatty()
    cols = shutil.get_terminal_size().columns
    CYAN = "\033[36m" if isatty else ""
    BOLD = "\033[1m" if isatty else ""
    YELLOW = "\033[33m" if isatty else ""
    RESET = "\033[0m" if isatty else ""
    rule = CYAN + "─" * cols + RESET
    sys.stderr.write(f"\n{rule}\n")
    sys.stderr.write(f"  {BOLD}{CYAN}✦ shellmando{RESET}\n")
    sys.stderr.write(f"  {YELLOW}▶{RESET} {user_prompt}\n")
    sys.stderr.write(f"{rule}\n\n")
    sys.stderr.flush()


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
    llama_server: bool = True,
    top_p: float = 0.95,
    repeat_penalty: float = 1.0,
) -> str | None:
    """Send a streaming chat request to llama.cpp or Ollama; return the assistant content or None."""
    if llama_server:
        url = f"{host}/v1/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "top_k": 0,  # disabled
                "min_p": 0.05,  # keeps only tokens ≥5% of top token's probability
                "top_p": top_p,  # mild nucleus sampling as safety net
                "repeat_penalty": repeat_penalty,  # OFF for code — variable names NEED repetition
                "stream": True,
            }
        ).encode()
    else:
        url = f"{host}/api/chat"
        payload = json.dumps(
            {
                "model": "hf.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"temperature": temperature},
                "stream": True,
            }
        ).encode()

    headers = {"Content-Type": "application/json"}
    isatty = sys.stderr.isatty()

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        accumulated = ""
        try:
            with _alternate_screen():
                if isatty:
                    _print_banner(user_prompt)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if llama_server:
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            delta = (chunk.get("choices", [{}])[0].get("delta", {}) or {}).get("content") or ""
                        else:
                            if not line:
                                continue
                            try:
                                chunk = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            delta = (chunk.get("message") or {}).get("content") or ""
                        if delta and isatty:
                            sys.stderr.write(delta)
                            sys.stderr.flush()
                        accumulated += delta
                        if not llama_server and chunk.get("done"):
                            break
            return accumulated
        except urllib.error.URLError as exc:
            log(f"[attempt {attempt}/{retries}] {exc}", verbose=verbose)
            if attempt < retries:
                time.sleep(retry_delay)
        except KeyboardInterrupt:
            return accumulated + "\n<interrupted>\n"

    log("Error: all retries exhausted.", verbose=True)
    return None


# ---------------------------------------------------------------------------
# Response processing
# ---------------------------------------------------------------------------


def strip_fences(text: str) -> str:
    """Remove markdown fences and trim whitespace, just like the old sed chain."""
    return "\n".join([line for line in text.splitlines() if not line.strip().startswith("```")])


def print_code_blocks_colored(text: str):
    bat = shutil.which("bat") or shutil.which("batcat")
    inblock = False
    lang = ""
    collected = ""
    max_len = 0
    if bat and "```" in text:
        for line in text.splitlines():
            stripped = line.strip()
            if inblock and lang != "":
                if stripped.startswith("```"):
                    sys.stdout.write(f"\033[38;5;244m")
                    horizontal_line = " " + min(max_len + 2, shutil.get_terminal_size().columns - 1) * "─"
                    print(horizontal_line)
                    sys.stdout.write(f"\033[0m")
                    subprocess.run(
                        [bat, "--paging=never", "--style=plain", f"-l={lang}"], input=collected, text=True, check=False
                    )
                    sys.stdout.write(f"\033[38;5;244m")
                    print(horizontal_line)
                    sys.stdout.write(f"\033[0m")
                    inblock = False
                    collected = ""
                    max_len = 0
                else:
                    collected += "  " + line + "\n"
                    max_len = max(len(line), max_len)
            elif stripped.startswith("```"):
                lang = stripped[3:]
                inblock = True
            elif "`" in line:
                arr = line.split("`")
                print_orange = False
                for elem in arr:
                    if print_orange:
                        sys.stdout.write(f"\033[38;5;172m{elem}\033[0m")
                    else:
                        sys.stdout.write(elem)
                    print_orange = print_orange == False
                print("")
            else:
                print(line)
    else:
        print(text)


def is_oneliner(text: str) -> bool:
    """Heuristic: fits comfortably in a readline prompt."""
    return "\n" not in text and len(text) < 512


def _get_mapping():
    return {
        "python": ".py",
        "bash": ".sh",
        "sh": ".sh",
        "zsh": ".zsh",
        "fish": ".fish",
    }


def extension_for_mode(mode: str) -> str:
    return _get_mapping().get(mode, ".txt")


def mode_from_extension(filepath: Path) -> str | None:
    """Guess the language mode from a file extension."""
    ext_map: dict[str, str] = {value: key for key, value in _get_mapping().items()}
    return ext_map.get(filepath.suffix.lower())


def find_label(initial_label: str, content: str) -> str:
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
        filtered_names = [name for name in outermost_names if name != "main"]

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
    log(f"\n{line}\n")
    if bat:
        subprocess.run([bat, "--paging=never", "--style=plain", str(path)], check=False)
    else:
        line = shutil.get_terminal_size().columns * "_"
        log(path.read_text(), verbose=True)
    log(f"\n{line}\n")


def display_diff(old: Path, new: Path, verbose: bool) -> None:
    """Show differences between *old* and *new* using bat --diff or diff."""
    lines1 = old.read_text().splitlines(keepends=True)
    lines2 = new.read_text().splitlines(keepends=True)
    difftxt = "".join(difflib.unified_diff(lines1, lines2, fromfile=str(old), tofile=str(new)))
    bat = shutil.which("bat") or shutil.which("batcat")
    if bat:
        subprocess.run(["batcat", "--language", "diff"], input=difftxt, text=True)
        log("\n")
    else:
        diff = shutil.which("diff")
        if diff:
            subprocess.run(
                [diff, "--color=auto", "-u", str(old), str(new)],
                check=False,
            )
        else:
            log(difftxt)


# ---------------------------------------------------------------------------
# Clarify mode helpers
# ---------------------------------------------------------------------------


def parse_clarify_response(response: str) -> list[tuple[str, list[str]]] | None:
    """Parse the clarification LLM response.

    Returns ``None`` when the task is CLEAR, otherwise a list of
    ``(topic, [option, ...])`` tuples extracted from lines like:
    ``A: <topic> [<option1> | <option2> | ...]``
    """
    stripped = response.strip()
    if "CLEAR" in stripped.split("\n")[0]:
        return None
    ambiguities: list[tuple[str, list[str]]] = []
    for line in stripped.splitlines():
        m = re.match(r"[A-Z]*:?\s*(.+?)\s*\[(.+?)\]\s*$", line.strip())
        if m:
            options = [o.strip() for o in m.group(2).split("||") if o.strip()]
            if options:
                topic = m.group(1).strip()
                ambiguities.append((topic, options))
    return (ambiguities if ambiguities else None)


def prompt_user_clarifications(ambiguities: list[tuple[str, list[str]]]) -> list[str]:
    """Interactively ask the user to pick one option per ambiguity.

    Returns one selected option string per entry in *ambiguities*.
    All output goes to stderr; input is read from stdin.
    """
    selections: list[str] = []
    for i, (topic, options) in enumerate(ambiguities, 1):
        print(f"  {i}. {topic}", file=sys.stderr)
        for j, opt in enumerate(options, 1):
            print(f"       {j}) {opt}", file=sys.stderr)
        while True:
            try:
                raw = input(
                    f"     Your choice: enter the number [1-{len(options)}], 0 for no choice or enter plain text (default 1): "
                ).strip()
            except EOFError:
                raw = ""
            if not raw:
                raw = "1"
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    selections.append(options[idx])
                    break
                elif idx == -1:
                    selections.append("")
                    break
                else:
                    print(f"     Enter a number from 1 to {len(options)}.", file=sys.stderr)
            except ValueError:
                try:
                    numbers = [int(token.strip()) for token in re.findall(r"(?:^|\s)\d+(?:$|[\s.:,])", raw.strip())]
                    replaced = raw
                    for idx in numbers:
                        replaced = replaced.replace(f"{idx}", options[idx - 1])
                    raw = replaced
                except IndexError:
                    pass

                # use the response
                selections.append(raw)
                break

    return selections


def build_clarified_prompt(
    original: str,
    ambiguities: list[tuple[str, list[str]]],
    selections: list[str],
    add_constraints_prompt: bool = True,
) -> str:
    """Append the user's clarification choices to *original*."""
    parts = [f"{topic}: {sel}" for (topic, _), sel in zip(ambiguities, selections) if sel != ""]
    constraints_prompt = ".\nHints: {" if add_constraints_prompt else "; "
    return original.rstrip(".") + constraints_prompt + "; ".join(parts) + "}"


def perform_clarification(
    args: argparse.Namespace,
    user_prompt: str,
    query_llm: callable,
    verbose: bool
) -> str:
    
    log(f"[clarify]: {CLARIFY_SYSTEM_PROMPT}", verbose=verbose)

    clarify_raw = query_llm(
        host=args.host,  # "http://localhost:8282", # , #
        model=args.model,
        system_prompt=CLARIFY_SYSTEM_PROMPT,
        user_prompt=f"{user_prompt.rstrip('.')}, Used Language: {args.mode}.",
        temperature=0.1,  # TODO
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        verbose=verbose,
        top_p=0.95,
        repeat_penalty=1.01,
    )
    if clarify_raw is None:
        log("Error: no clarification response from LLM.", verbose=True)
        return 1
    ambiguities = parse_clarify_response(clarify_raw)
    if ambiguities:
        log("\n[clarify] the task has some ambiguities:\n", verbose=True)
        log(re.sub("[A-Z]: ", " * ", clarify_raw.strip()), verbose=True)
        selections = prompt_user_clarifications(ambiguities)
        user_prompt = build_clarified_prompt(user_prompt, ambiguities, selections)
        log(f"[clarify] updated prompt: {user_prompt}", verbose=verbose)
    else:
        log("[clarify] Task is clear, proceeding …", verbose=verbose)
    return user_prompt


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
    def _resolve(env_key: str | None, *cfg_keys: str, default, debug: bool = False):
        if env_key:
            env_val = os.environ.get(env_key)
            if env_val is not None:
                if debug:
                    log(f"resolved env {env_key} as {env_val}")
                return env_val
        cfg_val = _deep_get(cfg, *cfg_keys, default=None)
        if cfg_val is not None:
            if debug:
                log(f"resolved cfg {cfg_keys} as {cfg_val}")
            return cfg_val
        return default

    p = argparse.ArgumentParser(
        prog="shellmando",
        description="Query a local LLM for shell commands or code snippets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              shellmando "list all docker containers sorted by size"
              shellmando -m python "read a CSV and plot column 3"
              shellmando -v -t 0.5 "find duplicate files in /data"
        """
        ),
    )
    p.add_argument("task", nargs="*", help="Natural-language task description")

    p.add_argument(
        "--start",
        action="store_true",
        default=False,
        help=(
            "Check if the LLM is running; start it if not (using --starter), then exit. "
            "Useful as a login hook or manual warm-up command."
        ),
    )
    p.add_argument(
        "--autostart",
        choices=["true", "false"],
        default=None,
        metavar="{true,false}",
        help=(
            "Enable or disable automatic LLM startup on login. "
            "Uses a systemd user service when available, otherwise adds a line to your shell profile. "
            "Example: --autostart=true"
        ),
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Start an interactive session (mode + task prompted at the terminal).",
    )

    p.add_argument(
        "-d",
        "--defaults",
        action="store_true",
        default=False,
        help=(
            "Write all other supplied parameters as new defaults to the config file and exit. "
            "Example: -c -m python  sets the default mode to python."
        ),
    )

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
        "-m",
        "--mode",
        default=_resolve(None, "generation", "mode", default=None),
        help=(
            f"Language / shell mode (default: bash). "
            f"Available: {', '.join(sorted(ALL_MODES))}. "
            "Unique prefixes are accepted (e.g. '-m p' for python)."
        ),
    )
    g2.add_argument(
        "-t",
        "--temperature",
        type=float,
        default=float(_resolve(None, "generation", "temperature", default=DEFAULT_TEMPERATURE)),
        help=f"Sampling temperature (default: {DEFAULT_TEMPERATURE})",
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
    g2.add_argument(
        "-c",
        "--clarify",
        action="store_true",
        default=False,
        help=(
            "Before generating, ask the LLM to identify ambiguities in the task "
            "and let you choose between options. The selected answers are appended "
            "to the prompt before the real generation call."
        ),
    )

    # Network / resilience
    g3 = p.add_argument_group("Network / resilience")
    g3.add_argument(
        "--timeout",
        type=float,
        default=float(_resolve(None, "network", "timeout", default=DEFAULT_TIMEOUT)),
        help="HTTP timeout in seconds",
    )
    g3.add_argument(
        "--retries",
        type=int,
        default=int(_resolve(None, "network", "retries", default=DEFAULT_RETRIES)),
        help="Max retries for the LLM call",
    )
    g3.add_argument(
        "--retry-delay",
        type=float,
        default=float(_resolve(None, "network", "retry_delay", default=DEFAULT_RETRY_DELAY)),
        help="Seconds between retries",
    )
    g3.add_argument(
        "--startup-timeout",
        type=float,
        default=float(_resolve(None, "network", "startup_timeout", default=DEFAULT_STARTUP_TIMEOUT)),
        help="Max seconds to wait for LLM startup",
    )

    # Output
    g4 = p.add_argument_group("Output")
    g4.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(os.path.expanduser(_resolve("SHELLMANDO_OUTPUT", "output", "dir", default=DEFAULT_OUTPUT_DIR))),
        help=f"Output folder for saved scripts (env: SHELLMANDO_OUTPUT, default: {DEFAULT_OUTPUT_DIR})",
    )
    g4.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="File to write the one-liner into (for shell wrapper integration)",
    )
    g4.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Forward full LLM response and debug info to stderr",
    )
    g4.add_argument(
        "--raw",
        action="store_true",
        help="Print raw LLM output to stdout and exit (skip all processing)",
    )
    g4.add_argument(
        "-s",
        "--snippet",
        action="store_true",
        help="Generate snippet only: display, copy to clipboard, no file saved",
    )

    # File operations
    g5 = p.add_argument_group("File operations")
    g5.add_argument(
        "-e",
        "--edit",
        type=Path,
        default=None,
        metavar="FILE",
        help="Send FILE content with the prompt; write the result back (edit in place)",
    )
    g5.add_argument(
        "-a",
        "--append",
        type=Path,
        default=None,
        metavar="FILE",
        help="Send FILE content with the prompt; append the generated code to the file",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    cfg_path, cfg = _pre_parse_config(argv)
    parser = build_parser(cfg)
    args = parser.parse_args(argv)

    # -- Change defaults and exit -------------------------------------------
    if args.defaults:
        raw_argv = argv if argv is not None else sys.argv[1:]
        return _write_defaults(cfg_path, cfg, raw_argv)

    # -- Autostart management -----------------------------------------------
    if args.autostart is not None:
        return handle_autostart(args.autostart == "true", os.path.expanduser(args.starter))

    # -- Start-only mode ----------------------------------------------------
    if args.start:
        if args.starter:
            args.starter = os.path.expanduser(args.starter)
        running, _ = ensure_llm_running(args.host, args.starter, args.startup_timeout, verbose=True, use_starter=True)
        return 0 if running else 1

    # -- Interactive mode ---------------------------------------------------
    if args.interactive:
        from shellmando_interactive import interactive_mode
        return interactive_mode(args)

    if not args.task:
        parser.error("the following arguments are required: task")

    user_prompt: str = " ".join(args.task)
    verbose: bool = args.verbose

    if cfg_path and verbose:
        log(f"[config] loaded {cfg_path}", verbose=True)

    # -- Resolve mode prefix ------------------------------------------------
    if args.mode is not None:
        args.mode = resolve_mode_interactive(args.mode)

    # -- File operations (-e/--edit, -a/--append) ---------------------------
    if args.edit and args.append:
        log("Error: --edit and --append are mutually exclusive.", verbose=True)
        return 1

    target_file: Path | None = args.edit or args.append
    file_op: str | None = "edit" if args.edit else ("append" if args.append else None)

    file_content: str = ""
    if target_file is not None:
        if target_file.exists():
            file_content = target_file.read_text(encoding="utf-8")
        # Auto-detect mode from file extension overriding -m
        detected = mode_from_extension(target_file)
        if detected:
            args.mode = detected

    # Resolve mode default
    if args.mode is None:
        args.mode = "bash"

    # Expand ~ in starter path from config
    if args.starter:
        args.starter = os.path.expanduser(args.starter)

    # 1. Ensure LLM is available ----------------------------------------
    running, llama_server = ensure_llm_running(args.host, args.starter, args.startup_timeout, verbose, args.starter)
    if not running:
        return 1

    # Bind the detected backend so it can be reused for the clarify pre-call
    _query = lambda **kw: query_llm(llama_server=llama_server, **kw)

    # 2. Build prompts --------------------------------------------------
    if args.os_hint == "" and os.environ.get("SHELLMANDO_OS") is None:
        args.os_hint = detect_os()

    system_prompt = args.system_prompt or build_system_prompt(
        args.mode, args.os_hint, args.snippet, (args.edit or args.append) is not None
    )

    system_prompt += " On conflict: user prompt overrides system prompt."

    if (args.edit or args.append) and len(file_content) > 0:
        fcontent = f"\n```{args.mode}\n{file_content}```\n"
        if args.edit:
            edit_instruction = f". Edit the code in-place: {fcontent}"
        else:
            edit_instruction = f". Current code is: {fcontent}. Give me ONLY your additions! "
        user_prompt += edit_instruction

    if args.clarify:
        log("[clarify] Asking LLM to identify ambiguities …", verbose=True)

        user_prompt = perform_clarification(args, user_prompt, _query, verbose)

    log(f"[system] {system_prompt}", verbose=verbose)
    log(f"[user]   {user_prompt}", verbose=verbose)

    # 3. Query LLM ------------------------------------------------------
    raw_content = _query(
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

    if args.mode == "assistant":
        print_code_blocks_colored(raw_content)
        return 3

    # 4. Process response -----------------------------------------------
    cleaned = strip_fences(raw_content).strip()

    # -- File operation: write result to target file ---------------------
    if file_op is not None and not args.snippet:
        bak_file = target_file.with_suffix(target_file.suffix + ".bak")
        if target_file.exists():
            shutil.copy2(target_file, bak_file)

        def append_changes():
            with target_file.open("a", encoding="utf-8") as f:
                if file_content and not file_content.endswith("\n"):
                    f.write("\n")
                f.write("\n" + cleaned + "\n")
            log(f"Appended to: {target_file}", verbose=True)

        if file_op == "edit":
            target_file.write_text(cleaned + "\n", encoding="utf-8")
            log(f"Wrote: {target_file}", verbose=True)
        else:  # append
            append_changes()
        display_diff(bak_file, target_file, verbose=True)

        # Ask user for action
        print("\nDo you want to apply the changes")
        choice = input("Enter your choice (y/n) - default y: ").strip()
        print(f"choice = '{choice}'")

        if choice == "y" or choice == "Y" or choice == "J" or choice == "":
            log("done.", verbose=True)
        else:
            # Revert - restore from backup
            os.remove(target_file)
            shutil.move(bak_file, target_file)
            log(f"Changes reverted from backup: {target_file}", verbose=True)
            choice = input("retry (y/n) - default n: ").strip()
            if choice == "y" or choice == "Y" or choice == "J":
                log("Retrying")
                new_temp = args.temperature - 0.1 if args.temperature > 0.2 else args.temperature - 0.01
                return main([*argv, "-t", str(new_temp)])
        return 0

    # Snippet mode: display + clipboard, nothing else -------------------
    if args.snippet:
        print_code_blocks_colored(raw_content)
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
        print_code_blocks_colored(raw_content)
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
