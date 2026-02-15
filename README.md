# shellmando

AI-powered shell tool that generates commands and scripts from natural language using a local LLM.

Describe what you need in plain English or any language your local LLM supports -- shellmando talks to a local OpenAI-compatible endpoint and returns ready-to-run shell commands or Python scripts, injected straight into your readline prompt.

## Features

- Generate shell commands (bash, sh, zsh, fish) and Python code from natural language
- One-liner results are injected directly into your shell prompt for review before execution
- Multi-line scripts are automatically saved to organized, timestamped directories
- Auto-starts your local LLM if it isn't running (configurable via `SHELLMANDO_LLM_STARTER`)
- Pretty-prints saved scripts with `bat`/`batcat` when available
- Optional [Atuin](https://github.com/atuinsh/atuin) history integration
- Zero pip dependencies -- uses only the Python standard library

## Requirements

- Python 3.7+
- A local OpenAI-compatible LLM endpoint (e.g. llama.cpp, Ollama, LM Studio, vLLM)
- bash or zsh

## Installation

1. Copy both files into your scripts directory:

```bash
mkdir -p ~/scripts
cp shellmando.py ~/scripts/
cp shdo.sh ~/scripts/
```

2. Source the shell wrapper in your `.bashrc` or `.zshrc`:

```bash
echo 'source ~/scripts/shdo.sh' >> ~/.bashrc
```

3. (Optional) Copy and customize the config file:

```bash
mkdir -p ~/.config/shellmando
cp shellmando.toml ~/.config/shellmando/config.toml
```

See [docs/config.md](docs/config.md) for the full configuration reference.

4. (Optional) Configure environment variables in your shell profile:

```bash
export SHELLMANDO_HOST="http://localhost:8280"   # LLM API base URL
export SHELLMANDO_LLM_STARTER="$HOME/scripts/start_llm.sh"  # script to auto-start LLM
export SHELLMANDO_OUTPUT="$HOME/scripts/shellmando_out"  # where scripts are saved
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `SHELLMANDO_DIR` | Directory containing `shdo.sh` and `shellmando.py` (auto-detected) |
| `SHELLMANDO_PY` | Path to `shellmando.py` (defaults to `$SHELLMANDO_DIR/shellmando.py`) |
| `SHELLMANDO_HOST` | LLM API base URL (default: `http://localhost:8280`) |
| `SHELLMANDO_LLM_STARTER` | Path to a script that starts the LLM server |
| `SHELLMANDO_OUTPUT` | Output directory for saved scripts (default: `$SHELLMANDO_DIR/generated`) |
| `SHELLMANDO_CONFIG` | Path to a TOML config file (empty = auto-detect) |
| `SHELLMANDO_OS` | OS context string for the system prompt (auto-detected if unset) |
| `SHELLMANDO_MODEL` | Model name to use (default: `default`) |

## Usage

```
ask [options] <task ...>
```

### Options

| Flag | Description |
|------|-------------|
| `-m, --mode` | Language mode: `bash`, `sh`, `zsh`, `fish`, `python`, `none` (default: `bash`) |
| `-t, --temperature` | Sampling temperature, 0-2 (default: `0.1`) |
| `-s, --snippet` | Generate snippet only: display, copy to clipboard, no file saved |
| `-j, --justanswer` | Just answer the question: print LLM reply and exit (no prompt adaptation, no file generated) |
| `-v, --verbose` | Show full LLM response and debug info |
| `--raw` | Print raw LLM output without processing |
| `-a, --append` | Append the LLM response to the specified file (the file's content is sent as context with your prompt) |
| `-e, --edit` | Overwrite the specified file with the LLM response (the file's content is sent as context with your prompt) |
| `-o, --output` | Output folder for saved scripts |
| `--os` | OS context string for the system prompt (auto-detected if omitted) |
| `--host` | LLM API base URL |
| `--starter` | Script to auto-start the LLM if it is not running |
| `--model` | Model name to use |
| `--system-prompt` | Override the entire system prompt |
| `--config` | Path to a TOML config file (see [docs/config.md](docs/config.md)) |

## Examples

### Generate a quick shell one-liner

```bash
ask "find all files larger than 100MB in the current directory"
```

The generated command (e.g. `find . -size +100M`) lands in your readline prompt -- review it, edit if needed, then press Enter to run.

### Generate a Python script

```bash
ask -m python "read users.csv and print the top 5 rows sorted by age"
```

Multi-line results are saved to `~/scripts/shellmando_out/YYYYMMDD/` and the execution command is placed in your prompt.

### Use higher temperature for more creative output

```bash
ask -t 0.8 "write a bash one-liner that renames all .jpeg files to .jpg"
```

Bump the temperature when you want the model to explore alternative approaches.

### Pipe-friendly raw mode

```bash
ask --raw "generate a cron expression for every weekday at 9am" > cron.txt
```

With `--raw`, the unprocessed LLM response is printed to stdout -- useful for piping or saving directly to a file.

## Configuration

shellmando supports a TOML config file for persistent settings. See [docs/config.md](docs/config.md) for the full reference, including all sections (`[llm]`, `[generation]`, `[network]`, `[output]`, `[prompts.*]`) and template variables.

## How it works

shellmando has two layers:

1. **`shdo.sh`** -- a thin bash function (`ask`) that handles flag parsing, temp-file management, and readline injection.
2. **`shellmando.py`** -- the Python backend that manages LLM health checks, builds prompts, queries the model, and processes the response.

When a result is a single short command it is injected into your shell prompt via readline. When the result is a longer script it is saved to disk, pretty-printed, and the run command is placed in your prompt instead.

## Disclaimer

This tool generates and may execute code produced by a large language model.
See [README_DISCLAIMER.md](README_DISCLAIMER.md) for the full disclaimer.

**Always review generated commands before running them.**

## License

[Apache License 2.0](LICENSE)
