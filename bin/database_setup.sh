#!/bin/bash
# Database Setup Script
# This script sets up and initializes the Job Finder database

SCRIPTDIR=$(readlink -f $(dirname $0))
PROJDIR=$(dirname $SCRIPTDIR)
SCRIPTNAME=$(basename $0)
cd $PROJDIR

# Add uv to PATH
export PATH=$PATH:~/.local/bin

echo "Running database_setup.py with args: $*"

# Use uv run to execute the script with proper dependencies
uv run python -m scrapescore.db_setup "$@"
