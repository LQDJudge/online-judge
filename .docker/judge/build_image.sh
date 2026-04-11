#!/bin/bash
set -e

# Build the judge Docker image from the DMOJ judge-server repo.
# Assumes judge-server is cloned at the same level as online-judge.
# Adjust the path and tier target as needed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PARENT_DIR="$(cd "$REPO_DIR/.." && pwd)"

JUDGE_SERVER_DIR="${JUDGE_SERVER_DIR:-$PARENT_DIR/judge-server}"

cd "$JUDGE_SERVER_DIR/.docker" && make judge-tierlqdoj-nocache
