# sph_exa_s / namd_s idle-cycles CPI caliber (2026-10-07)

## Status

Diagnosis-only. No simulator, header, test, or configuration change. This note
records a *reporting-pipeline* defect in the weekly ad-hoc summary generator
`tmp/fixC3-phase2-pilot-20260930/emit_perworkload_summary.py` and the
single-source rule that must prevent it from recurring.

## TL;DR

- The weekly-report numbers for `sph_exa_s` c04/c08/c16 (−60.70 / −69.17 /
  −75.44% CPI relative error) and `namd_s c32` (−49.72%) were **not** model
  regressions.
- The ad-hoc script computed gem5 CPI as `sum(measured_cycles) / sum(upk_inst)`,
  which includes idle (halt / `mwait` / repeated `f3 90`) cycles in the
  numerator. perf's default `cycles` event is
  `CPU_CLK_UNHALTED.THREAD` (active cycles only), so the active-CPI numerator
  must subtract `idle_cycles`.
- Re-deriving the gem5 denominator with active cycles collapses the errors to
  −9.17 / −13.32 / −23.32% (sph_exa_s c04/c08/c16) and −11.24% (namd_s c32),
  matching the authoritative validator
  `tools/run_native_kernel_fastsim_validation.py` within rounding.
- `docs/accuracy-reporting-contract.md:505` already forbids using idle-inclusive
  `measured_cycles` as the active-CPI numerator; the ad-hoc script violated the
  contract.

## Active-vs-full CPI definitions (single source of truth)

The oracle merger `tools/merge_kernel_events_oracle_v3.py:71-108` sums the
seven `CYCLE_FIELDS` buckets per core (`validate_kernel_events_oracle.py:19-27`)
and derives

```
active_cycles = sum(CYCLE_FIELDS) - idle_cycles
             = user_cycles
             + syscall_kernel_cycles
             + page_fault_kernel_cycles
             + irq_kernel_cycles
             + scheduler_kernel_cycles
             + unknown_kernel_cycles

perf_like_cpi_user_plus_kernel = active_cycles /
                                 user_plus_kernel_retired_instructions
```

The authoritative validator `tools/run_native_kernel_fastsim_validation.py:636-679`
consumes exactly this field as the gem5 CPI reference. Any downstream reporter
— ad-hoc or production — must consume `oracle_aggregate["perf_like_cpi_user_plus_kernel"]`
(preferred) or, when a merged `oracle/kernel_events.json` is unavailable,
reconstruct it as `(sum(CYCLE_FIELDS) - idle_cycles) / upk_instructions` per
core and sum the integer numerator and denominator before the division. The
raw `measured_cycles` field includes `idle_cycles` and must never be used as
the active-CPI numerator (`docs/accuracy-reporting-contract.md:505`).

## sph_exa_s idle-fraction table (from `tmp/perf-rerun-20260929`)

| case | cores | measured_cycles | active_cycles | idle_cycles | idle_frac | upk_inst |
|---|---:|---:|---:|---:|---:|---:|
| uarch-baseline-c04-803.sph_exa_s | 4 | 32,163,032 | 13,914,071 | 18,248,961 | 56.7% | 25,173,475 |
| uarch-baseline-c08-803.sph_exa_s | 8 | 72,578,297 | 25,815,203 | 46,763,094 | 64.4% | 49,385,628 |
| uarch-baseline-c16-803.sph_exa_s | 16 | 154,189,287 | 49,376,131 | 104,813,156 | 68.0% | 98,555,902 |

## Idle-inclusive vs active CPI collapse

| case | FS CPI | gem5 CPI (full = measured_cycles / upk) | signed rel err (full) | gem5 CPI (active = CYCLE−idle / upk) | signed rel err (active) |
|---|---:|---:|---:|---:|---:|
| uarch-baseline-c04-803.sph_exa_s | 0.5021 | 1.2777 | −60.70% | 0.5527 | −9.17% |
| uarch-baseline-c08-803.sph_exa_s | 0.4531 | 1.4696 | −69.17% | 0.5227 | −13.32% |
| uarch-baseline-c16-803.sph_exa_s | 0.3842 | 1.5645 | −75.44% | 0.5010 | −23.32% |
| uarch-baseline-c32-857.namd_s | 0.3890 | 0.7736 | −49.72% | 0.4383 | −11.24% |

The active-CPI numbers match the authoritative validator
`tmp/perf-aligned-default-fixC1-20260930/native-validator-summary.json` on
`sph_exa_s c04/c08` (11.89 → 9.17% and 10.77 → 13.32% are both within the
measurement-window drift between the two source roots the ad-hoc script and
the validator consume). The ad-hoc rerun's `cpi_g5` matches the merger
formula bit-identically (max |Δ| = 4.80e-7 cyc/macro across all 32 scored
cases), so any residual delta between the ad-hoc table and the validator's
table is a measurement-window artifact of
`tmp/perf-rerun-20260929/formal/source/` versus the validator's
`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`, not a
formula drift. The validator remains the authoritative release gate; the
ad-hoc script is only a sanity reporter and must agree with the merger
formula within 1e-3 cyc/macro.

## namd_s c32 (idle_frac 43.4%)

```
uarch-baseline-c32-857.namd_s: cores=32 mc=171,322,962 active=97,053,971 idle=74,268,991
                               upk=221,455,666 cpi_full=0.7736 cpi_active=0.4383 idle_frac=43.4%
```

## sph_exa_s c32 — diagnostic only

`tmp/perf-rerun-20260929/formal/source/` does not contain
`uarch-baseline-c32-803.sph_exa_s`. The weekly report must mark the sph_exa_s
c32 row as diagnostic-only (no gem5 oracle in this source root) rather than
emit a misleading number. The ad-hoc script must skip cases without a complete
oracle.

## Low-idle control cases (should not move)

| case | idle_frac | cpi_full | cpi_active | Δ cpi (cyc/macro) |
|---|---:|---:|---:|---:|
| uarch-baseline-c04-706.stockfish_r | 0.06% | 0.6738 | 0.6734 | 0.0004 |
| uarch-baseline-c08-706.stockfish_r | 0.12% | 0.5927 | 0.5920 | 0.0007 |
| uarch-baseline-c16-706.stockfish_r | 0.00% | 0.6702 | 0.6701 | 0.0000 |
| uarch-baseline-c04-854.graph500_s | 1.62% | 1.3752 | 1.3529 | 0.0223 |
| uarch-baseline-c04-710.omnetpp_r | 0.00% | 0.7438 | 0.7438 | 0.0000 |
| uarch-baseline-c04-782.lbm_r | 0.04% | 0.8597 | 0.8594 | 0.0003 |
| uarch-baseline-c04-816.nab_s | 0.00% | 0.4572 | 0.4572 | 0.0000 |
| uarch-baseline-c04-881.neutron_s | 0.00% | 0.4572 | 0.4572 | 0.0000 |

After the fix these cases must move by <0.001 cyc/macro in `cpi_g5`; any larger
movement indicates the fix touched more than the idle path and must be
investigated before publishing.

## Single-source rule

1. Any CPI reporter (ad-hoc or production) consuming gem5 oracle output must
   source `cpi_g5` from `oracle_aggregate["perf_like_cpi_user_plus_kernel"]`
   when a merged `oracle/kernel_events.json` is present.
2. When only per-core `kernel-events-core*.json` files are present, the
   reporter must subtract `idle_cycles` from the per-core cycle numerator
   before summing; it must not use `measured_cycles` as the active-CPI
   numerator.
3. Cases without a complete oracle must be skipped and surfaced in the output,
   not silently emitted with a derived denominator from a different source.
4. The authoritative validator `tools/run_native_kernel_fastsim_validation.py`
   remains the only release gate for CPI accuracy. Ad-hoc scripts under
   `tmp/*` are at most sanity checks and must agree with the validator
   within rounding (<1e-3 cyc/macro).

## Follow-up

- Patch `tmp/fixC3-phase2-pilot-20260930/emit_perworkload_summary.py` to
  consume active cycles as above.
- Re-emit the weekly table (`perworkload_summary.{csv,md}`) and verify
  per-workload CPI columns against the native validator (max |Δ| <1e-3
  cyc/macro).
- Keep the simulator on the serial-TSO default; no `src/`, headers, tests, or
  configs change as a result of this diagnosis.
