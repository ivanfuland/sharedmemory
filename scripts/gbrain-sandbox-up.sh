#!/usr/bin/env bash
# M0 GBrain PGLite 沙盒（无嵌入——M0 只验结构，BGE-M3 是 M1）。
set -euo pipefail
export PATH="$HOME/.bun/bin:$PATH"
export GBRAIN_HOME="$(cd "$(dirname "$0")/.." && pwd)/sandbox/gbrain"
mkdir -p "$GBRAIN_HOME"; cd "$GBRAIN_HOME"
[ -f "$GBRAIN_HOME/.gbrain/config.json" ] || gbrain init --pglite --no-embedding
echo "GBRAIN_HOME=$GBRAIN_HOME 就绪"
