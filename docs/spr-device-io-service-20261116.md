# Functional UART/APIC device service

Subsequent user decision, 2026-11-16: the SPR evaluation default now selects
`configs/spr-m4-device-io.cfg`, enabling both functional translation and device
service. The platform preset matches all ten current C4/C8 sources: 385-tick CPU
period, UART port region 0x80000000000003f8/8, and 100000-tick UART/APIC service.
This is shared platform configuration, with ordinary explicit overrides when
the target changes; there is no per-trace hash or gem5-code revision gate.
Use `device_io.enabled=false` for a device-only control. Generic non-SPR library
defaults remain unchanged. Earlier opt-in/default-off statements below describe
the candidate evaluation stage; frozen M1/M2/M3 are preserved.

Implemented as an independent opt-in `device_io.enabled` path within the existing
parallel two-stage simulator. The working SPR default advances to
`configs/spr-m3-functional-translation.cfg` with functional TLB enabled; frozen
SPR-M1/M2 remain unchanged. Device I/O is independently disabled by default.

The executable and exact experiment identities, CPI comparison and scoped host
costs are recorded in `tmp/device-io-service-20261116/host-final/`. Current host executable
SHA-256: `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`.
The SPR-M3 model-baseline ELF remains frozen at `tmp/spr-m3-20261116/fastsim`,
SHA-256 `2777a56faac2beaab2b2fdde493363d42a0fb8e3d9c0431fd5aea37c091637d5`.
The host revision moves classification into the existing MMIO-escape path;
qualified-source target results are checked independently of host timings.

The input consists of existing retired functional memory UOPs with physical
addresses. A stateless classifier recognizes the configured UART port range and
gem5 x86 local-APIC physical encoding (uint8 APIC ID). Native outcomes, response
timestamps and per-request measured latency are not consumed. No additional
interrupts, speculative traffic or architectural operations are generated.
Nonphysical requests and atomic device operations are outside this slice;
unrecognized escaped accesses remain explicitly unmodeled.

The classifier adds a compact source-configured service descriptor. The response
weave gates strict reads using the existing per-core head/store-drain constraints,
then propagates device response through producer completion, RAW, writeback,
retirement and LQ release. Writes retain address/data execution and retirement;
device service follows retirement and canWB propagation, and response holds SQ
credit and the memory-order/store-drain frontier. The first slice conservatively
uses memory-order frontiers for both configured device classes. It does not
reproduce every native LSQ issue rule or instruction-specific ordering relaxation.

Device accesses stay outside ordinary cache/DRAM events and their service is not
an L1/L2/LLC demand miss. The existing MMIO-escape diagnostics remain. Request
counts and service-volume counters are additive and separate from exposed CPI
cycles: `device_io_uart_requests`, `device_io_apic_requests`,
`device_io_unmodeled_requests`, `device_io_service_cycles`. Physical source ticks
are rounded up once per service to the configured CPU cycle. Device transport,
I/O-bus contention and response-port queuing remain unmodeled; independent cores
do not share an invented global device frontier. Device state/values and actual
interrupt generation are already reflected in the fixed functional execution.

Enable only after the source-bound configuration and functional TLB settings:

```ini
# Values below match the validated C4/C8 Zstd sources; rebind for a new source.
device_io.enabled = true
device_io.clock_period_ticks = 385
device_io.uart_address = 0x80000000000003f8
device_io.uart_size = 8
device_io.uart_latency_ticks = 100000
device_io.apic_latency_ticks = 100000
```

These are static platform values from the actual `config.ini`, not CPI-fitted
constants. This slice requires interval_weave/time_epoch, full retirement
exposure, sparse response feedback, TSO, MMIO escape and a source-mapped
commit-to-IEW delay. A single tick period currently requires uniform core clocks.
Set `device_io.enabled=false` for device-only rollback while retaining the TLB
repair. Select the immutable SPR-M2 profile with the corresponding source
geometry/thread plan for a historical translation rollback; merely disabling
functional TLB after the M3 profile leaves the M3 four-walker setting active.

Directed tests cover actual read readiness, post-retirement write response/SQ
release, cross-epoch ordering, four-core independence, certificate parity,
unknown/atomic requests, APIC range bounds, missing timing, heterogeneous clocks
and fractional-exposure rejection. A warmed aligned device-store → RAM-load
witness reproduced a fast-path bypass before repair (scalar 280 cycles versus
fast 18, 16 UOPs transferred). The repaired block certificate checks the carried
memory-order frontier; a no-device positive control still transfers eligible
blocks. The original red witness and pre-review library are retained in the
experiment directory.

Validation uses the maintained quick scorer and saved native task-clock audit.
Source identity, per-core macro/UOP populations and OFF equivalence are checked
separately. Native cache PMU and hardware equivalence are not certified by this
quick CPI evaluation. Concurrent replay times are not throughput qualification;
scoped host measurements must keep preparation, integrity, warmup and full
process costs separate.

Final paired quick CPI results (functional TLB enabled throughout):

| Zstd | gem5 CPI | device OFF CPI | device ON CPI | relative error OFF → ON | absolute CPI error OFF → ON |
|---|---:|---:|---:|---:|---:|
| C4 | 1.206049 | 1.050041 | 1.200836 | −12.936% → −0.432% | 0.156009 → 0.005214 |
| C8 | 1.052763 | 1.075338 | 1.097865 | +2.144% → +4.284% | 0.022576 → 0.045102 |

Absolute error and case-equal MAE use cycles/macro; relative error is
`(FastSim−gem5)/gem5`. Two-case MAE decreases from 0.089292 to 0.025158;
MAPE decreases from 7.540% to 2.358%. C8 is a retained regression, not a passed
general accuracy claim. C4 has 13,168 recognized device operations, with zero
unmodeled escapes. C8 has 2,537 recognized device operations and one WRMSR local
special access outside this device-service slice; that operation and its
unmodeled diagnostic remain present. Configuration, all per-core macro/UOP
populations and source identity match; effective configuration differs only in
`device_io.enabled`. OFF exactly reproduces the prior functional-TLB prediction.

The current host executable's serial C4 diagnostic measured device OFF
1.580539M and ON 1.537033M user UOP/s
(-2.753%). Full-process working frequencies were
3.0914/3.0915GHz. Validated preload
excludes input preparation and validation from the primary measurement;
phase costs and full process wall remain separate in `host-final/throughput.json`.
This is one pair, not stable throughput acceptance. Earlier same-model ELF2777
samples ranged from ON0.855M to ON1.488M user UOP/s; both initial and reversed
pairs are preserved separately under `final/` and `performance/reverse/` and are
not pooled with the host-revised executable. The earlier42.6% slowdown was not
stable across repetitions. Current default profile keeps device service opt-in.

## Other-workload C4/C8 quick validation, 2026-11-16

The same d6f5 host executable was evaluated on XZ, FLAC, Graph500 and Marian at C4/C8, with functional TLB enabled and device OFF/ON as the only effective configuration difference. Sixteen new replays supplement four reused same-executable Zstd pilot outputs. Native device addresses, latencies and clock periods were independently bound for each source.

| Case | gem5 CPI | OFF CPI | ON CPI | relative error OFF → ON | absolute CPI error OFF → ON |
|---|---:|---:|---:|---:|---:|
| c04-801.xz_s | 0.575297 | 0.604327 | 0.603435 | +5.046% → +4.891% | 0.029030 → 0.028138 |
| c04-817.flac_s | 0.284584 | 0.294756 | 0.294890 | +3.574% → +3.621% | 0.010172 → 0.010306 |
| c04-854.graph500_s | 0.966422 | 0.837905 | 0.839507 | -13.298% → -13.132% | 0.128518 → 0.126915 |
| c04-872.marian_s | 2.364997 | 3.055029 | 3.055426 | +29.177% → +29.194% | 0.690032 → 0.690429 |
| c08-801.xz_s | 0.529423 | 0.557367 | 0.553891 | +5.278% → +4.622% | 0.027943 → 0.024468 |
| c08-817.flac_s | 0.269360 | 0.273608 | 0.274364 | +1.577% → +1.858% | 0.004247 → 0.005004 |
| c08-854.graph500_s | 0.969791 | 0.848402 | 0.851812 | -12.517% → -12.165% | 0.121390 → 0.117979 |
| c08-872.marian_s | 2.144827 | 2.845395 | 2.853856 | +32.663% → +33.058% | 0.700568 → 0.709030 |

Relative error=(FastSim−gem5)/gem5; absolute errors and MAE are cycles/macro. Aggregates are case-equal, not WAPE.

- C4, four new cases: MAPE 12.774% → 12.710%; MAE 0.214438 → 0.213947; 2 improved, 2 worse, 0 unchanged.
- C8, four new cases: MAPE 13.009% → 12.926%; MAE 0.213537 → 0.214120; 2 improved, 2 worse, 0 unchanged.

All negative results and unmodeled escaped-request counts are retained. Complete ten-case, without-Marian and device-coverage views are in [the batch report](../tmp/device-io-c4-c8-quick-20261116/report.md). Saved native task-clock audits were reused in quick mode; native PMU and hardware equivalence remain unscored. Concurrent replay durations do not establish throughput. Model parameters and defaults were unchanged during this campaign.
