#!/bin/sh
# luban-os-ci: ./run.sh <layer> [scenario ...] [--release VER | --bin DIR] [--index REF|DIR]
# The same on a computer as in CI (README.md). Needs python3 (3.11+) and,
# for machines, /dev/kvm.
here=$(cd "$(dirname "$0")" && pwd)
exec python3 "$here/harness/lci.py" "$@"
