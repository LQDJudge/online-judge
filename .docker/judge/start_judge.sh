#!/bin/bash
set -e

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <judge_id>"
    echo "Example: $0 judge1"
    exit 1
fi

judge_id="$1"
config_file="${JUDGE_CONFIG:-/problems/__conf__/general.yml}"
problems_dir="${PROBLEMS_DIR:?Set PROBLEMS_DIR to your problems directory (e.g. export PROBLEMS_DIR=/mnt/problems)}"
judge_image="${JUDGE_IMAGE:-vnoj/judge-tierlqdoj:latest}"

docker rm --force "$judge_id" 2>/dev/null || true

docker run -d \
    --name "$judge_id" \
    --network=host \
    --cap-add=SYS_PTRACE \
    --restart=always \
    -v "$problems_dir":/problems \
    "$judge_image" \
    run -c "$config_file" --no-watchdog 0.0.0.0 "$judge_id"

echo "Judge container started: $judge_id"
