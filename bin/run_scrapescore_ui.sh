#!/bin/bash
SCRIPTDIR=$(readlink -f $(dirname $0))
PROJDIR=$(dirname $SCRIPTDIR)
SCRIPTNAME=$(basename $0)
cd $PROJDIR

mkdir logs 2>/dev/null
mkdir work 2>/dev/null
pidfile="./work/.$SCRIPTNAME.pid"
. $SCRIPTDIR/run_functions

ARGS="$*"
if [ -f .env ]; then
# load environment variables from .env file if it exists
    . ./.env
fi



function start_this {
    ${SCRIPTDIR}/database_setup.sh
    echo "Running job_score with args: $ARGS"
    uv run python -m scrapescore.app "$@"
}
start