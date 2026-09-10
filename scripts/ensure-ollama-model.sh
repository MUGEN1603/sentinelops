#!/bin/bash
# scripts/ensure-ollama-model.sh — Ensure Ollama model is available
# 
# Usage: ./scripts/ensure-ollama-model.sh [model_name]
# Default model: qwen3-coder:latest

set -euo pipefail

MODEL="${1:-qwen3-coder:latest}"

echo "=== Ensuring Ollama model '$MODEL' is available ==="

# Check if ollama is running
if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "Starting Ollama server..."
    ollama serve >/dev/null 2>&1 &
    OLLAMA_PID=$!
    sleep 3
fi

# Check if model exists
if ollama list | grep -q "^$MODEL "; then
    echo "✅ Model '$MODEL' already available"
    exit 0
fi

echo "📥 Pulling model '$MODEL'..."
ollama pull "$MODEL"

echo "✅ Model '$MODEL' ready"