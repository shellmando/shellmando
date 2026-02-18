#!/usr/bin/env bash
set -euo pipefail
: "${SHELLMANDO_HOST:=http://localhost:8280}"
: "${SHELLMANDO_DIR:=$(dirname "$(realpath "${BASH_SOURCE[0]}")")}"
: "${SHELLMANDO_MODELS_DIR:=${SHELLMANDO_DIR}/models}"
: "${SHELLMANDO_MODEL:=$(ls ${SHELLMANDO_MODELS_DIR} | head -n 1)}"

function check_running() {
    if curl -s --max-time 0.5 "${SHELLMANDO_HOST}/health" >/dev/null 2>&1; then
        return 1
    fi
    if curl -s --max-time 0.5 "${SHELLMANDO_HOST}" >/dev/null 2>&1; then
        return 2
    fi
    return 0
}

function install_ollama() {
    echo "curl -fsSL https://ollama.com/install.sh | sh"
    read -rp "Install ollama? [Y/n]: " confirm
    if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
        echo "Aborted."; exit 0
    fi
    curl -fsSL https://ollama.com/install.sh | sh
}

function install_ollama_model() {
    local total_mem=$(free -g | awk 'NR==2{print $2}')
    echo "Detected total memory: ${total_mem} GB"
    echo
    # Define models    
    MODEL_LARGE="hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-1M-GGUF:IQ4_NL"
    MODEL_MEDIUM="hf.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M"
    MODEL_SMALL="hf.co/bartowski/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M"

    if (( total_mem > 40 )); then
        echo "Available models for your system:"
        echo "  1) Qwen3-Coder-30B-A3B  Q4_K_XL  (~19 GB) [default]"
        echo "  2) Qwen2.5-Coder-7B     Q4_K_M   (~4.7 GB)"
        echo "  3) Qwen2.5-Coder-3B     Q4_K_M   (~2 GB)"
        echo
        read -rp "Select model [1-3] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL="$MODEL_LARGE"  ;;
            2) MODEL="$MODEL_MEDIUM" ;;
            3) MODEL="$MODEL_SMALL"  ;;
            *) echo "Invalid choice."; exit 1 ;;
        esac

    elif (( total_mem > 20 )); then
        echo "Available models for your system:"
        echo "  1) Qwen2.5-Coder-7B  Q4_K_M  (~4.7 GB) [default]"
        echo "  2) Qwen2.5-Coder-3B  Q4_K_M  (~2 GB)"
        echo
        read -rp "Select model [1-2] (default: 1): " choice
        case "${choice:-1}" in
            1) MODEL="$MODEL_MEDIUM" ;;
            2) MODEL="$MODEL_SMALL"  ;;
            *) echo "Invalid choice."; exit 1 ;;
        esac

    else
        echo "Recommended model for your system:"
        echo "  Qwen2.5-Coder-3B  Q4_K_M  (~2 GB)"
        echo
        read -rp "Install? [Y/n]: " confirm
        if [[ "${confirm:-y}" =~ ^[Nn]$ ]]; then
            echo "Aborted."; exit 0
        fi
        MODEL="$MODEL_SMALL"
    fi

    echo
    echo "Pulling ${MODEL}..."
    ollama pull "${MODEL}"
}



# TODO ask user what to download and download file to models directory
# 17.3 GB
MODEL_LARGE_DOWNLOAD="https://huggingface.co/lovedheart/Qwen3-Next-REAP-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Next-REAP-30B-A3B-Instruct-Q4_K_XL.gguf?download=true"
# 4.68 GB
MODEL_MEDIUM_DOWNLOAD="https://huggingface.co/bartowski/Qwen2.5.1-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5.1-Coder-7B-Instruct-Q4_K_M.gguf?download=true"
# 2.1 GB
MODEL_SMALL_DOWNLOAD="https://huggingface.co/mradermacher/Rombos-LLM-V2.5.1-Qwen-3b-i1-GGUF/resolve/main/Rombos-LLM-V2.5.1-Qwen-3b.i1-Q4_K_M.gguf?download=true"

# TODO if no server is running: ask user if he wants to install ollama or llama-server
function check_install() {
    if (( check_running() == 0 )); then
        echo "===================================================================="
        echo "no running llm found"
        echo "please install ollama - otherwise setup your own start_llm script"
        echo "see shellmando documentation"
        echo "===================================================================="
        if which ollama > /dev/null 2>&1; then
            start_ollama()
        fi
        if which llama-server > /dev/null 2>&1; then
            start_llama_server()
        fi


    fi
}

