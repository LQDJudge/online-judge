#!/bin/bash
set -e

docker rm --force bridge 2>/dev/null || true
docker run -d \
    --name bridge \
    --network=host \
    --restart=unless-stopped \
    --memory=700m \
    -v /tmp:/tmp \
    bridge
