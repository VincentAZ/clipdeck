#!/usr/bin/env bash
# Engine tests (real ffmpeg) and the headless UI scenario.
set -e
cd "$(dirname "$0")"
python3 -m unittest tests.test_decoders tests.test_installer tests.test_engine "$@"
python3 -m unittest tests.test_ui
