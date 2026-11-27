#!/bin/sh
# Start from this folder without installing:  sh run.sh --music-dir ~/Musik
cd "$(dirname "$0")" && exec python3 -m toccata "$@"
