#!/bin/bash
set -e

if [ $# -ne 2 ]; then
    echo "Usage: $0 <start> <end>"
    echo "Example: $0 1 5  (starts judge1 through judge5)"
    exit 1
fi

start_range=$1
end_range=$2

if ! [[ "$start_range" =~ ^[0-9]+$ ]] || ! [[ "$end_range" =~ ^[0-9]+$ ]]; then
    echo "Error: Both arguments must be positive integers"
    exit 1
fi

if [ "$start_range" -gt "$end_range" ]; then
    echo "Error: Start range must be less than or equal to end range"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting judge$start_range through judge$end_range..."

for i in $(seq "$start_range" "$end_range"); do
    echo "Running: judge$i"
    "$SCRIPT_DIR/start_judge.sh" "judge$i"
done

echo "All judges started (judge$start_range to judge$end_range)."
