#!/usr/bin/env bash
# get.sh — One-line bootstrap installer for shellmando
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/shellmando/shellmando/main/get.sh | bash
#
# To pass options to the installer (e.g. skip LLM setup):
#   curl -sSL .../get.sh | bash -s -- --skip-llm
#
# To install a specific branch or from a custom repo:
#   SHELLMANDO_BRANCH=dev curl -sSL .../get.sh | bash
#   SHELLMANDO_REPO_URL=https://github.com/myfork/shellmando curl -sSL .../get.sh | bash
#
# To update an existing install, just re-run — scripts are overwritten,
# your config (~/.config/shellmando/config.toml) is left untouched.

set -euo pipefail

REPO_URL="${SHELLMANDO_REPO_URL:-https://github.com/shellmando/shellmando}"
BRANCH="${SHELLMANDO_BRANCH:-main}"

# -- colours (only when connected to a terminal) ---------------------------
if [[ -t 1 ]]; then
    GREEN='\033[32m'  YELLOW='\033[33m'  RED='\033[31m'  RESET='\033[0m'
else
    GREEN=''  YELLOW=''  RED=''  RESET=''
fi

info() { printf "${GREEN}>>>${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}>>>${RESET} %s\n" "$*"; }
err()  { printf "${RED}>>>${RESET} %s\n" "$*" >&2; }

# -- sanity checks ---------------------------------------------------------
if ! command -v curl &>/dev/null; then
    err "curl is required to install shellmando"
    exit 1
fi

if ! command -v tar &>/dev/null; then
    err "tar is required to install shellmando"
    err "  Debian/Ubuntu:  sudo apt install tar"
    err "  Fedora:         sudo dnf install tar"
    exit 1
fi

# -- scratch space ---------------------------------------------------------
TMP_DIR=$(mktemp -d /tmp/shellmando.XXXXXX)
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# -- download --------------------------------------------------------------
TARBALL_URL="${REPO_URL}/archive/refs/heads/${BRANCH}.tar.gz"
info "Downloading shellmando (${BRANCH})..."
if ! curl -sSL --fail "$TARBALL_URL" -o "${TMP_DIR}/shellmando.tar.gz"; then
    err "Download failed: ${TARBALL_URL}"
    err "Check your internet connection or set SHELLMANDO_REPO_URL / SHELLMANDO_BRANCH."
    exit 1
fi

# -- extract ---------------------------------------------------------------
info "Extracting..."
tar -xz -C "$TMP_DIR" -f "${TMP_DIR}/shellmando.tar.gz"

SRC_DIR=$(find "${TMP_DIR}" -maxdepth 1 -name 'shellmando-*' -type d | head -n1)
if [[ -z "${SRC_DIR}" ]]; then
    err "Extraction failed — could not find source directory in archive"
    exit 1
fi

# -- run the real installer with any forwarded arguments -------------------
info "Running installer..."
bash "${SRC_DIR}/install.sh" "$@"
