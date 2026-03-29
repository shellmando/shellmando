# --------------------------------------------------------------------------
# shellmando() – thin shell wrapper around shellmando.py
#
# Source this file from your .bashrc / .zshrc:
#   source ~/.local/lib/shellmando/shellmando.sh
# --------------------------------------------------------------------------

# XDG Base Directory defaults
: "${XDG_CONFIG_HOME:=$HOME/.config}"
export XDG_CONFIG_HOME
: "${XDG_DATA_HOME:=$HOME/.local/share}"
export XDG_DATA_HOME

# Override these via environment if you like:
: "${SHELLMANDO_DIR:=$(dirname "$(realpath "${BASH_SOURCE[0]}")")}"
export SHELLMANDO_DIR
: "${SHELLMANDO_OUTPUT:=${XDG_DATA_HOME}/shellmando}"
export SHELLMANDO_OUTPUT
: "${SHELLMANDO_MODELS_DIR}:=${SHELLMANDO_OUTPUT}/models"
export SHELLMANDO_MODELS_DIR
: "${SHELLMANDO_MODEL}:=$(ls ${SHELLMANDO_MODELS_DIR} | head -n 1)"
export SHELLMANDO_MODEL
: "${SHELLMANDO_HOST:=http://localhost:8280}"
export SHELLMANDO_HOST
: "${SHELLMANDO_LLM_STARTER:=${SHELLMANDO_DIR}/shellmando_start_llm.sh}"
export SHELLMANDO_LLM_STARTER

: "${SHELLMANDO_CONFIG:=}"   # path to TOML config (empty = auto-detect)
: "${SHELLMANDO_PY:=${SHELLMANDO_DIR}/shellmando.py}"

function shellmando() {
    # -- resolve Python interpreter ----------------------------------------
    # Priority: SHELLMANDO_PYTHON env var > python3 > uv run
    local -a _py
    if [[ -n "${SHELLMANDO_PYTHON:-}" ]]; then
        _py=("$SHELLMANDO_PYTHON")
    elif command -v python3 &>/dev/null; then
        _py=(python3)
    elif command -v uv &>/dev/null; then
        if [[ -n "${SHELLMANDO_PYTHON_VERSION:-}" ]]; then
            _py=(uv run --python "$SHELLMANDO_PYTHON_VERSION")
        else
            _py=(uv run)
        fi
    else
        echo "shellmando: python3 or uv is required but neither was found." >&2
        echo "  Install python3:  https://www.python.org/downloads/" >&2
        echo "  Install uv:       curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        return 1
    fi

    # -- collect flags that we forward to shellmando.py -------------------
    local -a py_args=()
    local OPTIND opt
    local snippet_mode=false
    local call_start=false

    # quick pre-scan: pass everything before the bare task words
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -s|--snippet)     py_args+=("$1"); snippet_mode=true; shift   ;;
            -c|--clarify)     py_args+=("$1");       shift ;;
            -d|--defaults)    py_args+=("$1");       shift ;;
            -v|--verbose)     py_args+=("$1");       shift   ;;
            -m|--mode)        py_args+=("$1" "$2"); shift 2 ;;
            -o|--output)      py_args+=("$1" "$2"); shift 2 ;;
            -e|--edit)        py_args+=("$1" "$2"); shift 2 ;;
            -a|--append)      py_args+=("$1" "$2"); shift 2 ;;
            -t|--temperature) py_args+=("$1" "$2"); shift 2 ;;
            --start)
                              py_args+=("$1"); call_start=true; shift ;;
            --autostart)
                              py_args+=("$1" "$2"); call_start=true; shift 2 ;;
            --os|--host|--starter|--model|--system-prompt|--config)
                              py_args+=("$1" "$2"); shift 2 ;;
            --raw)            py_args+=("$1");       shift   ;;
            --help|-h)        "${_py[@]}" "$SHELLMANDO_PY" --help; return 0 ;;
            --)               shift; break ;;
            -*)               echo "Unknown flag: $1 (try --help)"; return 1 ;;
            *)                break ;;       # first non-flag word → task starts
        esac
    done

    if [[ $# -eq 0 && ! call_start ]]; then
        echo "Usage: shellmando [options] <task …>" >&2
        return 1
    fi

    # -- temp file for the readline payload ------------------------------
    local prompt_file
    prompt_file=$(mktemp /tmp/shellmando_prompt.XXXXXX)
    trap 'rm -f "$prompt_file"' RETURN

    # -- call the python backend -----------------------------------------
    local -a base_args=(
        --host "$SHELLMANDO_HOST"
        --starter "$SHELLMANDO_LLM_STARTER"
        --output "$SHELLMANDO_OUTPUT"
        --prompt-file "$prompt_file"
        --model "$SHELLMANDO_MODEL"
    )
    [[ -n "$SHELLMANDO_CONFIG" ]] && base_args+=(--config "$SHELLMANDO_CONFIG")

    local exit_code
    "${_py[@]}" "$SHELLMANDO_PY" \
        "${base_args[@]}" \
        "${py_args[@]}" \
        -- "$@"
    exit_code=$?

    # -- interpret the result --------------------------------------------
    if [[ $exit_code -eq 1 ]]; then
        return 1
    fi

    # assistant mode: answer already printed to stdout, nothing else to do
    if [[ $exit_code -eq 3 ]]; then
        return 0
    fi

    # exit_code == 0  →  one-liner
    local cmd=""
    if [[ -s "$prompt_file" ]]; then
        cmd=$(<"$prompt_file")
    else
        cmd="$stdout_capture"
    fi

    if [[ -z "$cmd" && ! snippet_mode ]]; then
        echo "(no result)" >&2
        return 1
    fi
    local run_prompt="${PS1@P}"
    run_prompt="${run_prompt##*$'\n'}"
    read -e -i "$cmd" -p "$run_prompt" final_cmd
    history -s "$final_cmd"
    _shellmando_exec "$final_cmd"
}

# -- helper: execute with optional atuin tracking ------------------------
function _shellmando_exec() {
    local final_cmd="$1"
    if command -v atuin &>/dev/null; then
        local atuin_id
        atuin_id=$(atuin history start -- "$final_cmd" 2>/dev/null)
        eval "$final_cmd"
        local ec=$?
        atuin history end --exit "$ec" -- "$atuin_id" 2>/dev/null
        return $ec
    else
        eval "$final_cmd"
    fi
}

alias ask=shellmando
alias shdo=shellmando
alias shmdo=shellmando

