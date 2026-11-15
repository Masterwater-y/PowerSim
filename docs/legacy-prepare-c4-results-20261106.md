# Legacy prepare timing: C4 diagnostic comparison

The compatible legacy buffer-TSO profile now enables the timing-only prepare
calendar by default; `core.legacy_prepare_timing = false` restores the former
path. Explicit `true` is rejected for incompatible modes. Credits are 16.

Four C4 prefetch-on cases were replayed concurrently using the same frozen
binary and the same functional manifests as the previous resident-prefetch
experiment. Target identity validation passed 4/4; functional source identity,
retired UOP totals and per-core task macro populations match the previous
same-input outputs. This is a diagnostic comparison: all four independent
maintained-validator rescores of the new outputs completed and passed source
and population gates. References come from native Ruby/SLICC summaries, not
TaoTrace path-class cache fields. A validator pass is not an accuracy threshold
or hardware-equivalence certification. Timing start windows are not strictly matched. Hardware PMU
equivalence and Atomic/QEMU producer equivalence remain unverified.

| workload | gem5 CPI | FastSim CPI | absolute CPI error (cycles/macro) | signed CPI error |
| --- | ---: | ---: | ---: | ---: |
| FLAC | 0.281872 | 0.306884 | 0.025012 | +8.87% |
| Graph500 | 0.652711 | 0.753753 | 0.101042 | +15.48% |
| Marian | 1.428304 | 2.759166 | 1.330861 | +93.18% |
| XZ | 0.603524 | 0.547135 | 0.056388 | −9.34% |
| Four cases, equal weight | — | — | **MAE 0.378326** | **MAPE 31.72%** |

These are workload-task active cycles / retired macro instructions, not IPC or
UOP CPI. The aggregate is case-equal MAE/MAPE, not WAPE.

Native Ruby/SLICC request-level tag-miss diagnostics, signed difference
`(FastSim - gem5) / gem5`. These are not retired-load PMU counts:

| workload | L1D tag misses | private-L2 tag misses | LLC tag misses | branch misses |
| --- | ---: | ---: | ---: | ---: |
| FLAC | +90.65% | +383.21% | +369.13% | +2.07% |
| Graph500 | +7.44% | +344.91% | +427.04% | +12.54% |
| Marian | −3.39% | +41.08% | −25.31% | −5.83% |
| XZ | +89.56% | +115.69% | +57.93% | +0.76% |

Retired task-load L1 miss model estimate versus audited known native
membership: FLAC 5,176 / 3,898 (+32.79% provisional; native unknown 0),
Graph500 296,659 / 276,155 (+7.42% provisional; native unknown 0), Marian
55,443 / 43,387 (native unknown 4), XZ 60,280 / 23,616 (native unknown 27).
FLAC and Graph500 still have incomplete event qualification. Marian and XZ
are unavailable as complete errors; no unknown is treated as zero. All four
L2/L3 retired-load references have native unknowns and are not complete error
scores.

The run froze `tmp/legacy-prepare-c4-20261106/fastsim` and records its hash,
commands and per-case CPU affinities in `progress.json`. Scoring and result
outputs are under that same directory. Since all four cases ran concurrently,
their wall times are not an isolated throughput measurement. A later source
change fixed an audit-only field; the frozen evaluation binary predates that
fix, and these cases used the fast kernel with per-instruction audit disabled.
The model remains a single-cache-replay approximation, not independent
physical prepare/head hierarchy requests.
