#!/bin/bash
set -e

# Build from the parent of the online-judge directory,
# so that COPY online-judge /app works in the Dockerfile.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"
REPO_NAME="$(basename "$REPO_DIR")"

docker build --no-cache -t bridge -f "$SCRIPT_DIR/Dockerfile" "$PARENT_DIR"
