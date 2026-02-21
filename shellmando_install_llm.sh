#!/usr/bin/env bash
# shellmando LLM backend installer
# ==================================
# Installs ollama or llama.cpp and downloads a model.
#
# Usage:
#   ./shellmando_install_llm.sh          # interactive (asks backend + model)
#   ./shellmando_install_llm.sh --ollama     # install ollama, then pick model
#   ./shellmando_install_llm.sh --llama-cpp  # install llama.cpp, then pick model

set -euo pipefail

# -- XDG defaults ----------------------------------------------------------
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
: "${SHELLMANDO_MODELS_DIR:=${XDG_DATA_HOME}/shellmando/models}"
CONFIG_DIR="${XDG_CONFIG_HOME}/shellmando"
CONFIG_FILE="${CONFIG_DIR}/config.toml"

# -- colours ---------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD='\033[1m'  GREEN='\033[32m'  YELLOW='\033[33m'  RED='\033[31m'  CYAN='\033[36m'  RESET='\033[0m'
else
    BOLD=''  GREEN=''  YELLOW=''  RED=''  CYAN=''  RESET=''
fi

info()  { printf "${GREEN}>>>${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}>>>${RESET} %s\n" "$*"; }
err()   { printf "${RED}>>>${RESET} %s\n" "$*" >&2; }
step()  { printf "\n${BOLD}${CYAN}=== %s ===${RESET}\n\n" "$*"; }

# -- OS / arch detection ---------------------------------------------------
detect_os()   { uname -s; }
detect_arch() { uname -m; }

# -- total RAM in GB -------------------------------------------------------
get_total_mem_gb() {
    if command -v free &>/dev/null; then
        free -g | awk 'NR==2{print $2}'
    elif [[ "$(detect_os)" == "Darwin" ]]; then
        local bytes
        bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        echo $(( bytes / 1024 / 1024 / 1024 ))
    else
        echo 8  # conservative fallback
    fi
}

# ==========================================================================
# CONFIG HELPERS
# ==========================================================================

# update_config_key <section_key> <value>
# Uses sed to replace a key inside the [llm] section of the config.
update_config_key() {
    local key="$1"
    local value="$2"

    if [[ ! -f "$CONFIG_FILE" ]]; then
        warn "Config file not found at ${CONFIG_FILE}, skipping ${key} update."
        return 0
    fi

    # sed -i requires an empty string extension argument on macOS (BSD sed),
    # while GNU sed accepts -i with no argument; .bak suffix works on both.
    sed -i.bak "s|^${key} = .*|${key} = \"${value}\"|" "$CONFIG_FILE" \
        && rm -f "${CONFIG_FILE}.bak"
    info "Config updated: ${key} = \"${value}\""
}

update_config_after_install() {
    local model="$1"
    local starter_path="${HOME}/.local/lib/shellmando/shellmando_start_llm.sh"

    update_config_key "model" "$model"
    update_config_key "starter" "$starter_path"
}

# ==========================================================================
# OLLAMA
# ==========================================================================

install_ollama() {
    if command -v ollama &>/dev/null; then
        info "ollama is already installed."
        return 0
    fi

    echo ""
    echo "This will run the official ollama installer:"
    echo "  curl -fsSL https://ollama.com/install.sh | sh"
    echo ""
    read -rp "Proceed? [Y/n]: " confirm
    if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
        err "Aborted."; exit 1
    fi
    curl -fsSL https://ollama.com/install.sh | sh
    info "ollama installed successfully."
}

install_ollama_model() {
    local total_mem
    total_mem=$(get_total_mem_gb)
    info "Detected RAM: ${total_mem} GB"
    echo ""

    # Ollama model tags
    local MODEL_LARGE="hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-1M-GGUF:IQ4_NL"
    local MODEL_MEDIUM="hf.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"
    local MODEL_SMALL="hf.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M"
    local MODEL

    if (( total_mem > 40 )); then
        echo "Example models for your system:"
        printf "  ${BOLD}1)${RESET} Qwen3-Coder-30B-A3B  IQ4_NL   (~19 GB) [default]\n"
        printf "  ${BOLD}2)${RESET} Qwen2.5-Coder-7B     Q4_K_M   (~4.7 GB)\n"
        printf "  ${BOLD}3)${RESET} Qwen2.5-Coder-3B     Q4_K_M   (~2 GB)\n"
        echo ""
        read -rp "Select model [1-3] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL="$MODEL_LARGE"  ;;
            2) MODEL="$MODEL_MEDIUM" ;;
            3) MODEL="$MODEL_SMALL"  ;;
            *) err "Invalid choice."; exit 1 ;;
        esac
    elif (( total_mem > 20 )); then
        echo "Recommended model for your system (limited RAM):"
        printf "  ${BOLD}1)${RESET} Qwen2.5-Coder-7B  Q4_K_M  (~4.7 GB) [default]\n"
        printf "  ${BOLD}2)${RESET} Qwen2.5-Coder-3B  Q4_K_M  (~2 GB)\n"
        echo ""
        read -rp "Select model [1-2] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL="$MODEL_MEDIUM" ;;
            2) MODEL="$MODEL_SMALL"  ;;
            *) err "Invalid choice."; exit 1 ;;
        esac
    else
        echo "Recommended model for your system (limited RAM):"
        printf "  ${BOLD}Qwen2.5-Coder-3B  Q4_K_M  (~2 GB)${RESET}\n"
        echo ""
        read -rp "Pull this model? [Y/n]: " confirm
        if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
            err "Aborted."; exit 1
        fi
        MODEL="$MODEL_SMALL"
    fi

    echo ""
    info "Pulling ${MODEL} via ollama..."
    ollama pull "${MODEL}"
    update_config_after_install "$MODEL"
}

# ==========================================================================
# LLAMA.CPP
# ==========================================================================

install_llama_cpp() {
    if command -v llama-server &>/dev/null; then
        info "llama-server is already installed."
        return 0
    fi

    local os arch
    os="$(detect_os)"
    arch="$(detect_arch)"

    echo ""
    info "Installing llama.cpp (llama-server)..."

    # macOS: try Homebrew first
    if [[ "$os" == "Darwin" ]] && command -v brew &>/dev/null; then
        info "Installing via Homebrew..."
        brew install llama.cpp
        info "llama.cpp installed via Homebrew."
        return 0
    fi

    # Check unzip is available (needed for GitHub release archive)
    if ! command -v unzip &>/dev/null; then
        err "unzip is required to extract the llama.cpp release archive."
        echo "Install it with:  sudo apt install unzip   (Debian/Ubuntu)"
        echo "                  sudo dnf install unzip   (Fedora)"
        exit 1
    fi

    # Map platform to the GitHub release filename tag
    local platform_tag
    case "${os}-${arch}" in
        Linux-x86_64)  platform_tag="ubuntu-x64" ;;
        Linux-aarch64) platform_tag="ubuntu-arm64" ;;
        Darwin-arm64)  platform_tag="macos-arm64" ;;
        Darwin-x86_64) platform_tag="macos-x64" ;;
        *)
            err "Unsupported platform: ${os}-${arch}"
            echo ""
            echo "Please build llama.cpp from source:"
            echo "  https://github.com/ggerganov/llama.cpp#building-the-project"
            exit 1
            ;;
    esac

    echo "Fetching latest release info from GitHub..."
    local api_url="https://api.github.com/repos/ggerganov/llama.cpp/releases/latest"
    local release_json download_url
    release_json=$(curl -sf "$api_url")

    # Pick the CPU-only zip for this platform (exclude GPU variants)
    download_url=$(echo "$release_json" \
        | grep '"browser_download_url"' \
        | grep "${platform_tag}" \
        | grep '\.zip"' \
        | grep -v 'cuda\|hip\|vulkan\|kompute\|sycl\|rpc' \
        | head -1 \
        | sed 's/.*"browser_download_url": *"\([^"]*\)".*/\1/')

    if [[ -z "$download_url" ]]; then
        err "Could not find a prebuilt binary for ${os}-${arch}."
        echo ""
        echo "Please build llama.cpp from source:"
        echo "  https://github.com/ggerganov/llama.cpp#building-the-project"
        echo ""
        echo "Or on macOS install via:  brew install llama.cpp"
        exit 1
    fi

    local install_dir="${HOME}/.local/bin"
    mkdir -p "$install_dir"

    local zip_file="/tmp/llama-cpp-$$.zip"
    local extract_dir="/tmp/llama-cpp-$$"

    info "Downloading: ${download_url}"
    curl -L --progress-bar "$download_url" -o "$zip_file"

    echo "Extracting archive..."
    mkdir -p "$extract_dir"
    unzip -q "$zip_file" -d "$extract_dir"

    local server_bin
    server_bin=$(find "$extract_dir" -name "llama-server" -type f | head -1)
    if [[ -z "$server_bin" ]]; then
        err "llama-server binary not found in archive."
        rm -rf "$zip_file" "$extract_dir"
        exit 1
    fi

    install -m755 "$server_bin" "${install_dir}/llama-server"
    rm -rf "$zip_file" "$extract_dir"

    info "llama-server installed to ${install_dir}/llama-server"

    if ! command -v llama-server &>/dev/null; then
        warn "${install_dir} is not in your PATH."
        warn "Add this line to your shell profile to fix it:"
        warn "  export PATH=\"\${HOME}/.local/bin:\$PATH\""
    fi
}

download_llama_cpp_model() {
    local total_mem
    total_mem=$(get_total_mem_gb)
    info "Detected RAM: ${total_mem} GB"
    echo ""

    # Direct GGUF download URLs from HuggingFace
    local URL_LARGE="https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-1M-GGUF/blob/main/Qwen3-Coder-30B-A3B-Instruct-1M-IQ4_NL.gguf"
    local URL_REASONING="https://huggingface.co/mradermacher/Qwen3-Coder-Next-REAP-40B-A3B-i1-GGUF/resolve/main/Qwen3-Coder-Next-REAP-40B-A3B.i1-Q4_K_M.gguf"
    local URL_MEDIUM="https://huggingface.co/bartowski/Qwen2.5.1-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5.1-Coder-7B-Instruct-Q4_K_M.gguf"
    local URL_SMALL="https://huggingface.co/mradermacher/Rombos-LLM-V2.5.1-Qwen-3b-i1-GGUF/resolve/main/Rombos-LLM-V2.5.1-Qwen-3b.i1-Q4_K_M.gguf"
    local MODEL_URL

    if (( total_mem > 40 )); then
        echo "Available models for your system:"
        printf "  ${BOLD}1)${RESET} Qwen3-Next-REAP-30B        Q4_K_XL  (~17.3 GB) [default]\n"
        printf "  ${BOLD}1)${RESET} Qwen3-Coder-Next-REAP-40B  Q4_K_M   (~25 GB) [slower but better]\n"
        printf "  ${BOLD}2)${RESET} Qwen2.5-Coder-7B           Q4_K_M   (~4.7 GB)\n"
        printf "  ${BOLD}3)${RESET} Rombos-LLM-3B              Q4_K_M   (~2.1 GB)\n"
        echo ""
        read -rp "Select model [1-3] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL_URL="$URL_LARGE"  ;;
            2) MODEL_URL="$URL_REASONING" ;;
            3) MODEL_URL="$URL_MEDIUM" ;;
            4) MODEL_URL="$URL_SMALL"  ;;
            *) err "Invalid choice."; exit 1 ;;
        esac
    elif (( total_mem > 20 )); then
        echo "Available models for your system:"
        printf "  ${BOLD}1)${RESET} Qwen2.5-Coder-7B  Q4_K_M  (~4.7 GB) [default]\n"
        printf "  ${BOLD}2)${RESET} Rombos-LLM-3B     Q4_K_M  (~2.1 GB)\n"
        echo ""
        read -rp "Select model [1-2] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL_URL="$URL_MEDIUM" ;;
            2) MODEL_URL="$URL_SMALL"  ;;
            *) err "Invalid choice."; exit 1 ;;
        esac
    else
        echo "Recommended model for your system (limited RAM):"
        printf "  ${BOLD}Rombos-LLM-3B  Q4_K_M  (~2.1 GB)${RESET}\n"
        echo ""
        read -rp "Download this model? [Y/n]: " confirm
        if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
            err "Aborted."; exit 1
        fi
        MODEL_URL="$URL_SMALL"
    fi

    # Derive filename from URL (strip query string)
    local model_file
    model_file="${MODEL_URL%%\?*}"
    model_file="${model_file##*/}"

    mkdir -p "$SHELLMANDO_MODELS_DIR"
    local dest="${SHELLMANDO_MODELS_DIR}/${model_file}"

    if [[ -f "$dest" ]]; then
        warn "Model file already exists: ${dest}"
        read -rp "Re-download? [y/N]: " redownload
        if [[ ! "${redownload:-n}" =~ ^[Yy]$ ]]; then
            info "Using existing model: ${model_file}"
            update_config_after_install "$model_file"
            return 0
        fi
    fi

    info "Downloading ${model_file}..."
    info "Destination: ${dest}"
    curl -L --progress-bar "$MODEL_URL" -o "$dest"
    info "Model saved to: ${dest}"

    update_config_after_install "$model_file"
}

# ==========================================================================
# MAIN
# ==========================================================================

main() {
    local force_backend=""

    # Parse optional flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ollama)    force_backend="ollama";    shift ;;
            --llama-cpp) force_backend="llama-cpp"; shift ;;
            -h|--help)
                echo "Usage: ${0##*/} [--ollama | --llama-cpp]"
                echo ""
                echo "  --ollama      Install ollama and pull a model"
                echo "  --llama-cpp   Install llama.cpp (llama-server) and download a GGUF model"
                echo ""
                echo "Without flags the script asks interactively."
                exit 0
                ;;
            *) err "Unknown option: $1"; exit 1 ;;
        esac
    done

    step "shellmando LLM Backend Setup"

    local backend_choice
    if [[ -n "$force_backend" ]]; then
        backend_choice="$force_backend"
    else
        echo "Which LLM backend would you like to use?"
        printf "  ${BOLD}1)${RESET} llama.cpp  - Direct inference via llama-server binary\n"
        printf "  ${BOLD}2)${RESET} ollama     - Easy setup, model management built in\n"
        echo ""
        read -rp "Select backend [1/2] (default: 1): " raw_choice
        case "${raw_choice:-1}" in
            1|llama-cpp) backend_choice="llama-cpp" ;;
            2|ollama)    backend_choice="ollama" ;;
            *) err "Invalid choice. Please enter 1 or 2."; exit 1 ;;
        esac
    fi

    case "$backend_choice" in
        llama-cpp)
            step "Installing llama.cpp"
            install_llama_cpp
            step "Downloading model"
            download_llama_cpp_model
            ;;
        ollama)
            step "Installing ollama"
            install_ollama
            step "Selecting and pulling model"
            install_ollama_model
            ;;
    esac

    echo ""
    info "LLM setup complete!"
    echo ""
    echo "The LLM server will start automatically when you use shellmando."
    echo "You can also start it manually with:"
    echo "  ${HOME}/.local/lib/shellmando/shellmando_start_llm.sh"
}

main "$@"
