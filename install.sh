#!/usr/bin/env bash
# shellmando installer
# ====================
# Copies shellmando files into XDG-compliant locations and adds the
# shell wrapper to the user's profile.
#
# Usage:
#   ./install.sh                   # interactive install with defaults
#   ./install.sh --lib-dir DIR     # custom directory for shellmando.py + .sh
#   ./install.sh --uninstall       # remove installed files

set -euo pipefail

# -- XDG defaults ----------------------------------------------------------
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

# -- configurable paths ----------------------------------------------------
LIB_DIR="${LIB_DIR:-$HOME/.local/lib/shellmando}"
CONFIG_DIR="${XDG_CONFIG_HOME}/shellmando"
DATA_DIR="${XDG_DATA_HOME}/shellmando"

# -- source directory (where this script lives) ----------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# -- colours ---------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD='\033[1m'  GREEN='\033[32m'  YELLOW='\033[33m'  RED='\033[31m'  RESET='\033[0m'
else
    BOLD=''  GREEN=''  YELLOW=''  RED=''  RESET=''
fi

info()  { printf "${GREEN}>>>${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}>>>${RESET} %s\n" "$*"; }
err()   { printf "${RED}>>>${RESET} %s\n" "$*" >&2; }

# -- usage -----------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${0##*/} [OPTIONS]

Install shellmando into XDG-compliant locations.

Options:
  --lib-dir DIR     Install shellmando.py and shellmando.sh into DIR
                    (default: ~/.local/lib/shellmando)
  --no-config       Skip copying the example config file
  --no-profile      Skip adding the 'source' line to your shell profile
  --uninstall       Remove installed files and the profile source line
  -h, --help        Show this help message

Directories used (following the XDG Base Directory Specification):
  Config:   \$XDG_CONFIG_HOME/shellmando/  (default: ~/.config/shellmando/)
  Data:     \$XDG_DATA_HOME/shellmando/    (default: ~/.local/share/shellmando/)
  Scripts:  ~/.local/lib/shellmando/       (or --lib-dir)
EOF
}

# -- detect shell profile ---------------------------------------------------
detect_profile() {
    local shell_name
    shell_name="$(basename "${SHELL:-bash}")"
    case "$shell_name" in
        zsh)  echo "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash)
            # Prefer .bashrc; fall back to .bash_profile on macOS
            if [[ -f "$HOME/.bashrc" ]]; then
                echo "$HOME/.bashrc"
            else
                echo "$HOME/.bash_profile"
            fi
            ;;
        *)    echo "$HOME/.profile" ;;
    esac
}

# -- source line helper ------------------------------------------------------
SOURCE_LINE_MARKER="# shellmando"

source_line() {
    echo "source \"${LIB_DIR}/shellmando.sh\"  ${SOURCE_LINE_MARKER}"
}

profile_has_source() {
    local profile="$1"
    [[ -f "$profile" ]] && grep -qF "$SOURCE_LINE_MARKER" "$profile"
}

# -- install ----------------------------------------------------------------
do_install() {
    local install_config=true
    local install_profile=true

    # parse flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lib-dir)     LIB_DIR="$2"; shift 2 ;;
            --no-config)   install_config=false; shift ;;
            --no-profile)  install_profile=false; shift ;;
            -h|--help)     usage; exit 0 ;;
            *)             err "Unknown option: $1"; usage; exit 1 ;;
        esac
    done

    info "Installing shellmando"
    echo ""
    echo "  Scripts:  ${LIB_DIR}"
    echo "  Config:   ${CONFIG_DIR}"
    echo "  Data:     ${DATA_DIR}"
    echo ""

    # 1. Copy scripts
    mkdir -p "$LIB_DIR"
    cp "$SCRIPT_DIR/shellmando.py" "$LIB_DIR/shellmando.py"
    cp "$SCRIPT_DIR/shellmando.sh" "$LIB_DIR/shellmando.sh"
    chmod +x "$LIB_DIR/shellmando.py"
    info "Copied shellmando.py and shellmando.sh to ${LIB_DIR}"

    # 2. Copy example config (if not already present)
    if $install_config; then
        mkdir -p "$CONFIG_DIR"
        if [[ -f "$CONFIG_DIR/config.toml" ]]; then
            warn "Config already exists at ${CONFIG_DIR}/config.toml -- skipping (not overwritten)"
        else
            cp "$SCRIPT_DIR/shellmando.toml" "$CONFIG_DIR/config.toml"
            info "Copied example config to ${CONFIG_DIR}/config.toml"
        fi
    fi

    # 3. Create data directory
    mkdir -p "$DATA_DIR"
    info "Created data directory ${DATA_DIR}"

    # 4. Add source line to shell profile
    if $install_profile; then
        local profile
        profile="$(detect_profile)"

        if profile_has_source "$profile"; then
            warn "Shell profile ${profile} already sources shellmando -- skipping"
        else
            echo "" >> "$profile"
            source_line >> "$profile"
            info "Added 'source' line to ${profile}"
        fi
    fi

    echo ""
    info "Done! Restart your shell or run:"
    echo "  source $(detect_profile)"
}

# -- uninstall --------------------------------------------------------------
do_uninstall() {
    info "Uninstalling shellmando"

    # Remove scripts
    if [[ -d "$LIB_DIR" ]]; then
        rm -f "$LIB_DIR/shellmando.py" "$LIB_DIR/shellmando.sh"
        rmdir "$LIB_DIR" 2>/dev/null || warn "  ${LIB_DIR} not empty, left in place"
        info "Removed scripts from ${LIB_DIR}"
    fi

    # Remove source line from profile
    local profile
    profile="$(detect_profile)"
    if profile_has_source "$profile"; then
        # Remove the source line (portable sed -i)
        local tmp
        tmp="$(mktemp)"
        grep -vF "$SOURCE_LINE_MARKER" "$profile" > "$tmp"
        mv "$tmp" "$profile"
        info "Removed source line from ${profile}"
    fi

    echo ""
    warn "Config (${CONFIG_DIR}) and data (${DATA_DIR}) were kept."
    warn "Remove them manually if you no longer need them."
}

# -- main -------------------------------------------------------------------
main() {
    if [[ "${1:-}" == "--uninstall" ]]; then
        do_uninstall
    elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
        usage
    else
        do_install "$@"
    fi
}

main "$@"
