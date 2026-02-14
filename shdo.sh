# --------------------------------------------------------------------------
# ask() – thin shell wrapper around microagent.py
#
# Source this file from your .bashrc / .zshrc:
#   source ~/scripts/ask.bash
# --------------------------------------------------------------------------

# Override these via environment if you like:
: "${MICROAGENT_PY:=${HOME}/scripts/microagent.py}"
: "${MICROAGENT_HOST:=http://localhost:8280}"
: "${MICROAGENT_STARTER:=${HOME}/scripts/start_llm.sh}"
: "${MICROAGENT_OUTPUT:=${HOME}/scripts/microagent_out}"
: "${MICROAGENT_CONFIG:=}"   # path to TOML config (empty = auto-detect)

function ask() {
    # -- collect flags that we forward to microagent.py -------------------
    local -a py_args=()
    local OPTIND opt

    # quick pre-scan: pass everything before the bare task words
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -t|--temperature) py_args+=("$1" "$2"); shift 2 ;;
            -v|--verbose)     py_args+=("$1");       shift   ;;
            -m|--mode)        py_args+=("$1" "$2"); shift 2 ;;
            -o|--output)      py_args+=("$1" "$2"); shift 2 ;;
            --os|--host|--starter|--model|--system-prompt|--config)
                              py_args+=("$1" "$2"); shift 2 ;;
            --raw)            py_args+=("$1");       shift   ;;
            --help|-h)        python3 "$MICROAGENT_PY" --help; return 0 ;;
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
    prompt_file=$(mktemp /tmp/microagent_prompt.XXXXXX)
    trap 'rm -f "$prompt_file"' RETURN

    # -- call the python backend -----------------------------------------
    local -a base_args=(
        --host "$MICROAGENT_HOST"
        --starter "$MICROAGENT_STARTER"
        --output "$MICROAGENT_OUTPUT"
        --prompt-file "$prompt_file"
    )
    [[ -n "$MICROAGENT_CONFIG" ]] && base_args+=(--config "$MICROAGENT_CONFIG")

    local exit_code
    python3 "$MICROAGENT_PY" \
        "${base_args[@]}" \
        "${py_args[@]}" \
        -- "$@"
    exit_code=$?

    # -- interpret the result --------------------------------------------
    if [[ $exit_code -eq 1 ]]; then
        return 1
    fi

    # exit_code == 0  →  one-liner
    local cmd=""
    if [[ -s "$prompt_file" ]]; then
        cmd=$(<"$prompt_file")
    else
        cmd="$stdout_capture"
    fi

    if [[ -z "$cmd" ]]; then
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