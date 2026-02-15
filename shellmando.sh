# --------------------------------------------------------------------------
# ask() – thin shell wrapper around shellmando.py
#
# Source this file from your .bashrc / .zshrc:
#   source ~/scripts/ask.bash
# --------------------------------------------------------------------------

# Override these via environment if you like:
: "${SHELLMANDO_DIR:=$(dirname "$(realpath "${BASH_SOURCE[0]}")")}"
export SHELLMANDO_DIR
: "${SHELLMANDO_PY:=${SHELLMANDO_DIR}/shellmando.py}"
: "${SHELLMANDO_HOST:=http://localhost:8280}"
: "${SHELLMANDO_LLM_STARTER:=${SHELLMANDO_DIR}/start_llm.sh}"
: "${SHELLMANDO_OUTPUT:=${SHELLMANDO_DIR}/generated}"
: "${SHELLMANDO_CONFIG:=}"   # path to TOML config (empty = auto-detect)
export SHELLMANDO_OUTPUT

function ask() {
    # -- collect flags that we forward to shellmando.py -------------------
    local -a py_args=()
    local OPTIND opt
    local snippet_mode=false
    local justanswer=false

    # quick pre-scan: pass everything before the bare task words
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -t|--temperature) py_args+=("$1" "$2"); shift 2 ;;
            -s|--snippet)     py_args+=("$1"); snippet_mode=true; shift   ;;
            -j|--justanswer)  py_args+=("$1"); justanswer=true; shift ;;
            -v|--verbose)     py_args+=("$1");       shift   ;;
            -m|--mode)        py_args+=("$1" "$2"); shift 2 ;;
            -o|--output)      py_args+=("$1" "$2"); shift 2 ;;
            -e|--edit)        py_args+=("$1" "$2"); shift 2 ;;
            -a|--append)      py_args+=("$1" "$2"); shift 2 ;;
            --os|--host|--starter|--model|--system-prompt|--config)
                              py_args+=("$1" "$2"); shift 2 ;;
            --raw)            py_args+=("$1");       shift   ;;
            --help|-h)        python3 "$SHELLMANDO_PY" --help; return 0 ;;
            --)               shift; break ;;
            -*)               echo "Unknown flag: $1 (try --help)"; return 1 ;;
            *)                break ;;       # first non-flag word → task starts
        esac
    done

    if [[ $# -eq 0 ]]; then
        echo "Usage: ask [options] <task …>" >&2
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
    )
    [[ -n "$SHELLMANDO_CONFIG" ]] && base_args+=(--config "$SHELLMANDO_CONFIG")

    local exit_code
    python3 "$SHELLMANDO_PY" \
        "${base_args[@]}" \
        "${py_args[@]}" \
        -- "$@"
    exit_code=$?

    # -- interpret the result --------------------------------------------
    if [[ $exit_code -eq 1 ]]; then
        return 1
    fi

    # justanswer: answer already printed to stdout, nothing else to do
    if [[ $justanswer -eq 1 ]]; then
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
    _ask_exec "$final_cmd"
}

# -- helper: execute with optional atuin tracking ------------------------
function _ask_exec() {
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
