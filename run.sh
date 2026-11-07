#!/usr/bin/env bash
# Start LocalScript and open it in your browser. First run installs dependencies.
cd "$(dirname "$0")" && exec uv run localscript serve --open "$@"
