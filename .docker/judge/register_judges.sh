#!/bin/bash
set -e

if [ $# -ne 3 ]; then
    echo "Usage: $0 <start> <end> <key>"
    echo "Example: $0 1 10 'your-judge-authentication-key'"
    echo ""
    echo "Registers judge1 through judge10 in the site database."
    echo "Run this from the online-judge directory with the virtualenv activated."
    exit 1
fi

start_range=$1
end_range=$2
key=$3

if ! [[ "$start_range" =~ ^[0-9]+$ ]] || ! [[ "$end_range" =~ ^[0-9]+$ ]]; then
    echo "Error: Start and end must be positive integers"
    exit 1
fi

for i in $(seq "$start_range" "$end_range"); do
    echo "Registering judge$i..."
    python3 manage.py addjudge "judge$i" "$key"
done

echo "Registered judge$start_range through judge$end_range."
