# Configuration

shellmando can be configured through a TOML config file, environment variables, and CLI flags.

## Precedence

Settings are resolved in this order (highest wins):

1. CLI flags (`--host`, `--temperature`, etc.)
2. Environment variables (`SHELLMANDO_HOST`, etc.)
3. Config file (`shellmando.toml`)
4. Built-in defaults

## Config file locations

shellmando follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/) and searches for a config file in this order:

1. Path given via `--config` CLI flag
2. Path in the `SHELLMANDO_CONFIG` environment variable
3. `$XDG_CONFIG_HOME/shellmando/config.toml` (default: `~/.config/shellmando/config.toml`)
4. `shellmando.toml` next to `shellmando.py`

The first file found is used. If no file is found, built-in defaults apply.

## Sections

### `[llm]` -- LLM connection

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `host` | string | `"http://localhost:8280"` | Base URL of the OpenAI-compatible LLM endpoint. |
| `starter` | string | `$XDG_CONFIG_HOME/shellmando/shellmando_start_llm.sh` | Path to a script that starts the LLM server when it is not running. Supports `~` expansion. |
| `model` | string | `"default"` | Model name sent in the API request. |

### `[generation]` -- Generation defaults

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `"bash"` | Language mode. One of `bash`, `sh`, `zsh`, `fish`, `python`, or `none`. |
| `temperature` | float | `0.1` | Sampling temperature (0-2). Lower values produce more deterministic output. |
| `os` | string | `""` (auto-detect) | OS context string included in the system prompt. Leave empty to let shellmando detect the OS automatically. |

### `[network]` -- Network / resilience

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `timeout` | int | `120` | HTTP timeout per request in seconds. |
| `retries` | int | `30` | Maximum number of retry attempts for a failed LLM request. |
| `retry_delay` | float | `1.0` | Seconds to wait between retries. |
| `startup_timeout` | int | `50` | Maximum seconds to wait for the LLM server to become ready after launching the starter script. |

### `[output]` -- Output

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `dir` | string | `$XDG_DATA_HOME/shellmando` | Directory where generated multi-line scripts are saved (default: `~/.local/share/shellmando`). Supports `~` expansion. Scripts are organized into `YYYYMMDD/` subdirectories. |

### `[prompts.*]` -- Prompt templates

Prompt templates let you customize the system prompt and the user prompt wrapping sent to the LLM. Two sections are available:

- `[prompts.shell]` -- used for all shell modes (`bash`, `sh`, `zsh`, `fish`)
- `[prompts.python]` -- used for Python mode

Each section supports three keys:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `system` | string | _(built-in)_ | The system prompt sent to the LLM. |

If a section or key is missing, the built-in defaults are used.

#### Template variables

All three fields support these placeholder variables, replaced at runtime:

| Variable | Description | Example |
|----------|-------------|---------|
| `{mode}` | Current language mode | `bash`, `python` |
| `{os}` | Detected or configured OS description | `Linux / Ubuntu 22.04 / x86_64` |
| `{python_version}` | Python version running shellmando | `3.12` |

## Full example

```toml
# -- LLM connection --------------------------------------------------------

[llm]
host = "http://localhost:8280"
starter = "~/.config/shellmando/shellmando_start_llm.sh"
model = "default"

# -- Generation defaults ----------------------------------------------------

[generation]
mode = "bash"                  # bash | sh | zsh | fish | python
temperature = 0.1
os = ""                        # leave empty to auto-detect

# -- Network / resilience ---------------------------------------------------

[network]
timeout = 120                  # HTTP timeout per request (seconds)
retries = 30                   # max retry attempts
retry_delay = 1.0              # seconds between retries
startup_timeout = 50           # max seconds to wait for LLM startup

# -- Output -----------------------------------------------------------------

[output]
dir = "~/.local/share/shellmando"

# -- Prompt templates -------------------------------------------------------

[prompts.shell]
system = """\
You are a {mode} expert on {os}. \
Reply ONLY with the needed command(s), no explanation. \
Use variables only if necessary."""

[prompts.python]
system = """\
Reply ONLY with Python (>= {python_version}) code. NO explanation, no prose. Style: use comprehension instead of loops, modern type hints."""
```

## Python 3.11+ note

Config file parsing requires `tomllib` (part of the standard library since Python 3.11). On Python 3.7-3.10 you can install the backport:

```bash
pip install tomli
```

If neither `tomllib` nor `tomli` is available, the config file is silently ignored and built-in defaults are used.
