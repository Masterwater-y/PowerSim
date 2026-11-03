#!/usr/bin/env bash
set -euo pipefail

# Compatibility entry point retained for existing automation and approvals.
# The managed launcher adds resumable status/actions, strict FST v7 syscall
# metadata auditing, and separate C4 calibration/C8 held-out reporting.
exec /data00/yinhaolang/FastSim/scripts/launch_taotrace_fst_v7_c4_c8_formal.sh "$@"
