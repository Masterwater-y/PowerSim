# Trace-open preprocessing and bounded parallelism

The current replay opens each manifest member before entering `Simulator::run`.
Opening a binary source constructs its functional companion readers; several of
those readers independently scan the hot stream and decompress companion data
for their existing cross-checks. These scans are also performed when the
optional functional-identity hashing is disabled. Published external and model
wall times do not separately measure this phase, so their difference is not a
measurement of trace-open time alone.

`open_trace_manifest` now constructs independent per-core readers concurrently,
with at most four workers by default. It retains every reader and verification,
returns sources in manifest core order, and reports source errors in that same
order. `FASTSIM_TRACE_OPEN_WORKERS=1` selects the old serial ordering;
integers 1 through 64 are accepted, limited by the number of manifest entries.
The variable controls host preprocessing only, not modeled timing or FST data.
The implementation uses independently owned file streams, gzip readers and
companion data; it does not share mutable reader state between workers.

The dedicated four-core test constructs real binary FSTs and frontend gzip
companions. It checks source identity and all five records per core in serial
and parallel, checks simultaneous corruptions select the same earliest-core
error, and rejects an invalid worker count. These checks establish correctness
for the fixture, not a measurement of production speedup or all possible traces.

## Reproducible FLAC trace-open measurement

After the coordinated build and completion of other measurements, run the two
commands **sequentially** with identical CPU affinity, system load and build.
The manifest is the full C4 10M run used by the existing prefetch-suppression
results. Store the outputs in a new directory and preserve the source files.

```bash
mkdir -p tmp/spr-trace-open-throughput-20261105
LD_LIBRARY_PATH=/opt/gcc-11/lib64 FASTSIM_TRACE_OPEN_WORKERS=1 ./build/fastsim_parallel_trace_open_tests --benchmark \
  tmp/spr-native-prefetch-observation-20261105/full-flac-c4-10m/tao_trace/manifest.txt \
  > tmp/spr-trace-open-throughput-20261105/serial.json
LD_LIBRARY_PATH=/opt/gcc-11/lib64 FASTSIM_TRACE_OPEN_WORKERS=4 ./build/fastsim_parallel_trace_open_tests --benchmark \
  tmp/spr-native-prefetch-observation-20261105/full-flac-c4-10m/tao_trace/manifest.txt \
  > tmp/spr-trace-open-throughput-20261105/parallel.json
```

Compare the `cores` arrays exactly, including `source_core`, record boundaries
and functional-companion record counts. The timings measure only manifest open
and reader construction; they exclude model execution and separately requested
functional identity hashing. The array is not a cryptographic identity check:
verify that the source artifacts remain those in the existing input identity
`sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`
from `tmp/spr-prefetch-suppression-20261105/off.json`, using the maintained
functional identity procedure. Record affinity, binary hash, environment, and
whether data is cold or warm; reverse order and repeat if cache/order effects
matter. Evaluate end-to-end and model wall times separately on the same input,
without simultaneous A/B runs. No full FLAC benchmark has been run for this
change in the following measurement.

## Real FLAC measurement

On CPUs 112–119, with no concurrent replay, the frozen benchmark binary
`sha256:8e73c156b75975709318225dc9d8e694ff5013a9696ffeb7cb224e1c4ef88d45`
was run sequentially against the above manifest, first with one worker and then
with four. `LD_LIBRARY_PATH=/opt/gcc-11/lib64` was applied to both runs. The
first attempted process failed before opening input because the system C++
library was too old; its logs are retained separately.

| Workers | Internal trace-open (s) | External elapsed (s) | User CPU (s) | Peak RSS (KiB) |
|---:|---:|---:|---:|---:|
| 1 | 262.213 | 262.22 | 257.13 | 88,228 |
| 4 | 77.3571 | 77.36 | 256.94 | 86,004 |

The observed trace-open speedup is 3.39×, or 70.5% less elapsed time, in this
single ordered pair. Both outputs have identical per-core source IDs, record
counts, warmup and measurement boundaries, and core companion record counts;
these source counts and warmup boundaries also match the existing functional
source identity metadata. This metadata comparison is not a fresh source hash.
`fincore` snapshots show all 60 `.fst*` files fully resident during the first
run, before the second, and after the second. There is no snapshot from before
the first run and no cache flush, so the first initial cache state is unknown.

Logs and per-core JSON are under `tmp/spr-trace-open-throughput-20261105/` as
`workers-1-retry.*`, `workers-4.*`, and `cache-*.txt`. The measurement covers
trace-open only; end-to-end replay, independent identity hashing, and model
throughput were not measured in this pair.
