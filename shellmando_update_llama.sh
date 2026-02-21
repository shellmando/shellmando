#!/usr/bin/env bash
# shellmando llama-server updater
# =================================
# Updates llama-server to the latest release from the llama.cpp GitHub page.
# Installs to the same location as the currently active binary.
#
# Shared install logic is sourced from shellmando_install_llm.sh so there
# is no code duplication between the installer and this updater.
#
# Usage:
#   ./shellmando_update_llama.sh          # update (asks for confirmation)
#   ./shellmando_update_llama.sh --yes    # non-interactive, skip confirmation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"

# Source shared functions from the LLM installer.
# The BASH_SOURCE guard in shellmando_install_llm.sh prevents its main()
# from running when sourced.
# shellcheck source=shellmando_install_llm.sh
source "${SCRIPT_DIR}/shellmando_install_llm.sh"

# --------------------------------------------------------------------------

_llama_server_version() {
    # llama-server prints "version: b<N> (<hash>)" or similar; grab first line.
    llama-server --version 2>&1 | head -1 || echo "unknown"
}

main() {
    local auto_yes=false
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) auto_yes=true; shift ;;
            -h|--help)
                echo "Usage: ${0##*/} [--yes]"
                echo ""
                echo "  --yes   Skip the confirmation prompt"
                exit 0
                ;;
            *) err "Unknown option: $1"; exit 1 ;;
        esac
    done

    step "shellmando llama-server Update"

    # Show where llama-server currently lives and what version it is.
    if command -v llama-server &>/dev/null; then
        local current_bin current_version
        current_bin="$(command -v llama-server)"
        current_version="$(_llama_server_version)"
        info "Current binary : ${current_bin}"
        info "Current version: ${current_version}"
    else
        warn "llama-server is not currently installed (not found in PATH)."
        warn "It will be installed to ${HOME}/.local/bin/llama-server"
    fi

    echo ""

    if ! $auto_yes; then
        read -rp "Download and install the latest llama-server release? [Y/n]: " confirm
        if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
            echo "Aborted."
            exit 0
        fi
    fi

    echo ""
    update_llama_server

    # Show the new version.
    echo ""
    if command -v llama-server &>/dev/null; then
        info "New version: $(_llama_server_version)"
    fi
    info "Update complete."
}

main "$@"
