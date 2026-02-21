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

function start_ollama() {
    echo "Starting ollama on ${SHELLMANDO_HOST}..."
    OLLAMA_HOST=${SHELLMANDO_HOST/localhost/0.0.0.0} ollama serve > /dev/null 2>&1 &
}

function start_llama_server() {
    echo "Starting llama-server on ${SHELLMANDO_HOST}..."
    MODEL_FILE=${SHELLMANDO_MODELS_DIR}/${SHELLMANDO_MODEL}
    ALIAS=${SHELLMANDO_MODEL}
    llama-server  -m ${MODEL_FILE} \
        --alias ${ALIAS} \
        --jinja \
        -ngl 99 --ctx-size 16384 --temp 0.7 --top_p 0.8 --top_k 20 --min-p 0.01 --repeat-penalty 1.05 --fit on \
        --sleep-idle-seconds 300 \
        --host 0.0.0.0 --port 8280 \
        > /dev/null 2>&1 &
}


function start_server() {
    if check_running; then
        if which llama-server > /dev/null 2>&1; then
            start_llama_server
            return 0
        fi
        
        if which ollama > /dev/null 2>&1; then
            start_ollama
            return 0
        fi    
    fi
}

start_server
