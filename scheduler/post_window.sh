#!/bin/sh
# Called by the scheduler after each real listen window with the IQ file path.
cd "$(dirname "$0")/../sdrmux" || exit 1
exec nice -n 10 ../.venv/bin/python ham_process.py "$@" >> "$HOME/influ/sched/ham_process.log" 2>&1
