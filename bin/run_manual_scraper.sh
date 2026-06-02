#!/bin/bash
# Manual scraper wrapper script
# Usage: ./bin/run_manual_scraper.sh <site> <search> [OPTIONS]

# Change to project root
cd "$(dirname "$0")/.." || exit 1

# Set PYTHONPATH
export PYTHONPATH="./src:./src/lib"

# Run the manual scraper
uv run python -m scrapescore.batch.manual_scraper "$@"
