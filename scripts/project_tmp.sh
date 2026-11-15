#!/usr/bin/env bash
# Source this file, or pass a command: bash scripts/project_tmp.sh COMMAND ARGS...
# Keep both application and compiler temporary files on the project filesystem.
_fastsim_tmp_script_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
_fastsim_tmp_project_root="$(dirname "$_fastsim_tmp_script_dir")"
export TMPDIR="$_fastsim_tmp_project_root/tmp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
unset _fastsim_tmp_script_dir _fastsim_tmp_project_root
if [[ ! -d "$TMPDIR" || ! -w "$TMPDIR" ]]; then
    printf 'FastSim temporary directory is not writable: %s\n' "$TMPDIR" >&2
    return 1 2>/dev/null || exit 1
fi
if [[ "${BASH_SOURCE[0]}" == "$0" && "$#" -gt 0 ]]; then
    exec "$@"
fi
