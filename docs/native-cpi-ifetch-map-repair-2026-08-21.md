# Native CPI I-side physical mapping repair (2026-08-21)

## Status: modeled-I-fetch profile promoted to maintained default

The `.ifmap` gem5 producer experiment was withdrawn from the active
`/data00/yinhaolang/gem5-fs` source tree on 2026-08-21. The functional
translation fallback is not safe in the target Ruby FS configuration: the
Stockfish C4 pilot terminated at `RubyPort.cc:463` with `Ruby functional read
failed for address 0x5758000`. The original request observation also keyed
virtual pages without a complete CR3/address-space namespace, so a successful
lookup would not by itself prove that the emitted physical page belonged to
the retiring instruction's current address space.

The unsafe producer is not a dependency of the implemented repair. FastSim now
has a portable, deterministic instruction-page model and can replay committed
L1I misses through the target's unified private L2, LLC, CHA, and DRAM timing
path. Exact `.ifmap` consumption remains available only as an oracle/debug
mode. The p4/p4b/p4c producer patches remain forensic prototypes and must not
be applied to production collection.

After reverting the active producer, `gem5.opt` was rebuilt with the existing
Python 3.11 ABI. Neither the source nor the rebuilt binary contains the p4
instruction-page-map producer symbols.

## Finding

Adding CPL0 records fixed instruction-population coverage but did not add the
missing I-side timing causality. FST v7's hot `address` field belongs to data
accesses, while `.fst.imap` intentionally contains static virtual-PC decoding
only and explicitly forbids physical instruction addresses. The existing L1I
candidate therefore tags with virtual PC and charges a fixed local miss term;
it cannot generate physical L2/LLC/DRAM requests, model I-side contention, or
feed a delayed instruction response back into a time epoch.

This explains why native kernel instructions alone do not reduce the CPI tail:
the trace now says *what retired in the kernel*, but still not *which physical
instruction blocks supplied those records*.

## Address inputs and active consumer

The optional `coreN.fst.ifmap` v1 companion adds only functional instruction
translation state:

- key/effective edge: `(record ordinal, address-space ID, virtual page)`;
- value: physical 4-KiB page;
- a row applies before its anchor record and remains active until replacement;
- address-space switches and same-AS remaps are represented exactly;
- the 64-byte FST hot record and all existing files remain byte-compatible.

Forbidden inputs remain forbidden: fetch/response ticks, cache or ITLB results,
send retries, wrong-path instruction identity, PMU labels, and CPI-derived
corrections are absent.

The C++ reader/writer, slice/warmup delegation, v7 upgrade, formal-dataset copy,
strict audit tool, and round-trip tests are implemented. The withdrawn gem5
and TCSim patch prototypes describe one attempted producer/plumbing path; they
are not part of the active collection stack.

FastSim supports two address modes after a committed L1I miss:

- `modeled` (the portable default) preserves the configured page offset and
  hashes `(address-space ID, virtual page, mapping seed)` into a valid DRAM-page
  placement. A disjoint upper-half cache identity prevents accidental aliases
  with exact data addresses while the corresponding low placement is sent to
  DRAM. The mapping is stateless and independent of host worker scheduling.
- `trace` reads `.fst.ifmap`. `trace.require_instruction_page_map=true` makes a
  missing translation fatal. This mode is for exact-oracle experiments, not a
  production collection requirement.

The L1I lookup is VIPT: virtual block bits choose the set and the selected
address mode supplies the tag. Configuration validation requires all L1I set
index bits to fit within the page offset. A committed L1I miss creates a
request descriptor containing its cache line, request edge, local baseline
response, privilege class, and address provenance. With
`core.fetch_supply_lower_hierarchy=true`, that descriptor bypasses L1D, enters
the unified private L2, and shares LLC capacity, CHA queues, MSHRs, and DRAM
timing with data requests. A response slower than the local L1I baseline feeds
back into committed frontend timing. Requests beyond the current time-epoch
horizon defer their owning UOP instead of being committed early.

The current PMU validator's cache oracle is explicitly the committed
data-demand population reconstructed from LSQ/Ruby lifecycle sideband. Direct
instruction requests therefore use separate `instruction_l2`,
`instruction_llc`, and `instruction_cha` counters. They still mutate the same
cache arrays and timing queues, so later data-demand misses caused by I-side
capacity pollution remain visible in the existing PMU fields. This separation
prevents an I-side miss from being compared directly with a data-only gem5
reference.

The immutable versioned profile is
`configs/gem5-v28_4-fs-modeled-ifetch.cfg`. On 2026-08-25 it was promoted
through the maintained `configs/gem5-fs-native-kernel.cfg` production alias.
The established v28_1/v28_2 profiles remain unchanged for controlled
comparison; v28_2 is the explicit no-lower-I-fetch control.

## Validation completed

- `cmake --build build -- -j16`: pass;
- `./build/fastsim_tests`: pass;
- C++ round trip covers ASID 7 → 11 → 7 and a later ASID-7 remap;
- FST upgrade preserves all mapping rows;
- the producer patch was subsequently pilot-tested and withdrawn after the
  Ruby functional-translation failure described above;
- Python collection/audit tools pass bytecode compilation;
- the old Stockfish C4 dataset remains readable without `.ifmap`, while strict
  audit rejects it and reports all 11,826,861 core-0 records as unmapped;
- with the new ledger disabled, the Stockfish C4 control is bit-identical in
  total cycles (10,258,538), CPI (0.2564634308), perf-like CPI (0.5242552968),
  and its 19,567,829-instruction denominator; all new counters stay zero;
- repository diff whitespace check passes.

The portable lower-hierarchy implementation additionally passes:

- deterministic mapping checks across address spaces and mapping seeds;
- separate VIPT index/tag and unified-L2 bypass unit tests;
- an end-to-end L1I-miss replay test with LLC outcome conservation;
- the full `fastsim_tests` binary;
- 10,000-instruction production-profile smoke runs for seeds 1, 2, and 17.
  All three replay 256 L1I misses as exactly 256 instruction-L2 requests and
  preserve both data and instruction CHA outcome invariants. Their modeled CPI
  spans 15.7499--15.9899 versus 15.4704 for ledger-only, which is a 1.52%
  max/min seed spread in this small, deliberately cold synthetic workload—not
  a formal accuracy result.

## Formal 40-case result (2026-08-25)

The C4/C8/C16/C32 native matrix completed 40/40 for mapping seeds 1, 2, and 17
with no source, conservation, or replay failure. The comparison control is the
same current binary and v28_2 profile with the I-side bridge disabled (including
the already-validated disjoint LLC-outcome fix).

| Metric | Control | Modeled-I range across seeds 1/2/17 |
|---|---:|---:|
| CPI mean absolute error | 7.457% | 6.837%--6.857% |
| CPI P99 absolute error | 13.524% | 13.171%--13.538% |
| private-L2 miss MAPE | 4.497% | 3.546%--3.595% |
| private-L2 miss P99 | 19.002% | 18.696%--18.796% |
| private-L2 miss maximum | 20.451% | 22.228%--22.459% |
| LLC miss MAPE | 5.561% | 5.553%--5.556% |
| LLC miss P99 | 21.532% | 21.532% (unchanged) |

Across all cases, seed 1 improved CPI absolute error in 29/40 cases, seed 2 in
25/40, and seed 17 in 27/40. The per-case CPI spread caused by physical
placement has P99 1.276 percentage points of gem5 CPI and maximum 1.334 points;
the aggregate mean and L2-P99 results are much more stable. The three runs have
an identical 452,770,644 L1I accesses and 3,582,405 L1I misses. Seed-dependent
lower outcomes are small: instruction-L2 misses range from 516,397 to 516,687,
and instruction-LLC misses from 93,443 to 93,658.

Conclusion: modeled mapping is sufficient to retain the causal cache path;
exact gem5 PFNs are not the dominant blocker at the current 13% CPI and
19%--22% cache tails. The profile improves mean CPI and private-L2 accuracy,
but does not repair the LLC P99 and worsens the private-L2 maximum. Following
the explicit default-enable decision on 2026-08-25, maintained native-FS
entrypoints now select it through `configs/gem5-fs-native-kernel.cfg`; v28_2
remains available as the immutable control. Throughput is tracked separately
because shared-host concurrent measurements are not a valid candidate/control
A/B gate.

## Remaining validation edge

Modeled placement is intentionally not a claim about gem5's exact guest page
allocator, page coloring, code/data physical aliasing, or remap lifetime. Cache
geometry and physical-address width alone cannot recover those OS decisions.
Exact mapping is useful only when validating those effects and only if a safe,
address-space-correct producer becomes available.

The fixed-seed result and seed envelope must remain separate; the seed must not
be tuned per workload. A safe exact producer would be useful only if the goal
requires reproducing the remaining per-case page-coloring spread or studying
code/data aliases. Wrong-path instruction identities are still absent, so this
repair models committed instruction supply and lower-level contention, not
speculative I-cache pollution. A dedicated, load-controlled throughput gate is
still required before attributing a speed delta to this profile.
