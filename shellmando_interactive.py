"""shellmando_interactive – interactive session for shellmando.

Provides the terminal UX when shellmando is invoked without arguments:
mode selection, task input, LLM warmup ping, and post-response
execute/copy prompt.

Imported lazily from shellmando.main() to avoid a module-level cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import threading
import urllib.request
from pathlib import Path

from shellmando import (
    SHELLMANDO_DIR,
    build_system_prompt,
    copy_to_clipboard,
    detect_os,
    display_code,
    ensure_llm_running,
    is_oneliner,
    log,
    print_code_blocks_colored,
    query_llm,
    save_script,
    strip_fences,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def getch() -> str:
    """Read a single keypress from stdin without requiring Enter (Unix only)."""
    try:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch
    except Exception:
        try:
            line = input()
            return line[0] if line else "\n"
        except (EOFError, KeyboardInterrupt):
            return "\x03"


def _ping_llm(host: str, model: str, llama_server: bool) -> None:
    """Send a tiny warmup request to the LLM and discard the response silently."""
    try:
        if llama_server:
            url = f"{host}/v1/chat/completions"
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.1,
                "stream": False,
                "max_tokens": 1,
            }).encode()
        else:
            url = f"{host}/api/chat"
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "options": {"temperature": 0.1},
                "stream": False,
            }).encode()
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()  # discard
    except Exception:
        pass  # warmup failure is non-critical


# ---------------------------------------------------------------------------
# Interactive session
# ---------------------------------------------------------------------------

_MODE_MAP = {
    "a": "assistant",
    "b": "bash",
    "f": "fish",
    "p": "python",
    "s": "sh",
    "z": "zsh",
}


def interactive_mode(args: argparse.Namespace) -> int:
    """Run an interactive shellmando session (invoked when no arguments are given)."""
    isatty = sys.stderr.isatty()
    BOLD = "\033[1m" if isatty else ""
    CYAN = "\033[36m" if isatty else ""
    RESET = "\033[0m" if isatty else ""

    sys.stderr.write(f"{BOLD}{CYAN}Entering shellmando interactive session{RESET}\n")
    sys.stderr.write(
        "What mode would you like to use: a(ssistant), b(ash), f(ish), p(ython), s(h), z(sh)\n"
    )
    sys.stderr.write("Your choice (press key): ")
    sys.stderr.flush()

    while True:
        ch = getch()
        if ch in _MODE_MAP:
            mode = _MODE_MAP[ch]
            break
        if ch in ("\x03", "\x04"):  # Ctrl+C / Ctrl+D
            sys.stderr.write("\n")
            return 1

    sys.stderr.write(f"\n{mode.capitalize()} mode\n\n")
    sys.stderr.write("Please enter your task:\n")
    sys.stderr.flush()

    # Ensure LLM is running
    if args.starter:
        args.starter = os.path.expanduser(args.starter)
    running, llama_server = ensure_llm_running(
        args.host, args.starter, args.startup_timeout, verbose=False, use_starter=True
    )
    if not running:
        log("Error: LLM not reachable.", verbose=True)
        return 1

    # Kick off a silent warmup ping so the LLM is hot by the time the user submits
    ping_thread = threading.Thread(
        target=_ping_llm,
        args=(args.host, args.model, llama_server),
        daemon=True,
    )
    ping_thread.start()

    try:
        user_prompt = input()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return 1

    if not user_prompt.strip():
        log("No task entered.", verbose=True)
        return 1

    if args.os_hint == "" and os.environ.get("SHELLMANDO_OS") is None:
        args.os_hint = detect_os()

    system_prompt = build_system_prompt(mode, args.os_hint, False, False)
    system_prompt += " On conflict: user prompt overrides system prompt."

    _query = lambda **kw: query_llm(llama_server=llama_server, **kw)

    raw_content = _query(
        host=args.host,
        model=args.model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=args.temperature,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        verbose=args.verbose,
    )

    if raw_content is None:
        log("Error: no response from LLM.", verbose=True)
        return 1

    cleaned = strip_fences(raw_content).strip()
    has_code = "```" in raw_content

    if mode == "assistant":
        print_code_blocks_colored(raw_content)
        if has_code:
            sys.stderr.write("\nWould you like to c(opy) the code? (any other key to skip)\n")
            sys.stderr.write("Your choice (press key): ")
            sys.stderr.flush()
            ch = getch()
            sys.stderr.write("\n")
            sys.stderr.flush()
            if ch == "c":
                if copy_to_clipboard(cleaned):
                    sys.stderr.write("code copied to clipboard\n")
                else:
                    sys.stderr.write("clipboard not available\n")
                sys.stderr.flush()
        return 3

    # Shell / code modes
    print_code_blocks_colored(raw_content)

    sys.stderr.write("\nWould you like to e(xecute) or c(opy) the code? (any other key to skip)\n")
    sys.stderr.write("Your choice (press key): ")
    sys.stderr.flush()

    ch = getch()
    sys.stderr.write("\n")
    sys.stderr.flush()

    if ch == "c":
        if copy_to_clipboard(cleaned):
            sys.stderr.write("code copied to clipboard\n")
        else:
            sys.stderr.write("clipboard not available\n")
            if platform.system() == "Linux":
                sys.stderr.write("  >> suggest installing xclip: sudo apt install xclip\n")
        sys.stderr.flush()
        return 3  # done, nothing to execute

    if ch == "e":
        if is_oneliner(cleaned):
            if args.prompt_file:
                args.prompt_file.write_text(cleaned, encoding="utf-8")
            return 0
        p = save_script(cleaned, args.output, mode=mode, label="script", make_executable=True)
        try:
            p = p.relative_to(Path(SHELLMANDO_DIR))
        except Exception:
            pass
        display_code(p, verbose=True)
        exec_cmd = f"python3 {p}" if mode == "python" else str(p)
        if args.prompt_file:
            args.prompt_file.write_text(exec_cmd, encoding="utf-8")
        return 2

    # Any other key → skip execution
    return 3
