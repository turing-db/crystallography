#!/bin/bash
# Self-healing tunnel for the CCDC crystallography demo.
#   8087 -> visualizer (proxies /api to TuringDB on 6691)
#   6691 -> TuringDB REST, for running Cypher directly from the laptop
# macOS has no setsid, so launch with nohup.
while true; do
  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
      -L 8087:localhost:8087 \
      -L 6691:localhost:6691 \
      ubuntu@hetz-adam
  echo "$(date -Is) tunnel dropped, reconnecting in 3s"
  sleep 3
done
