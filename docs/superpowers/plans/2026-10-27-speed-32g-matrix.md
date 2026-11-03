# Speed 32GiB C4/C8/C16 collection implementation plan

> Execute in the current authorized dirty workspace; preserve prior changes. Main coordinates, focused independent agents own syscall boundary support and native admission/warmup. No commit/push.

**Goal:** collect Marian/Graph500/XZ/FLAC speed at C4/C8/C16, 32GiB each, with exact common end and bounded disk use.
**Architecture:** opt-in raw syscall micro-ops plus existing ABI metadata; preserve legacy FST; initialize real worker pools and finish census/UART under KVM, retain 500K detailed instruction-stop warmup then fastest-core10M user-UOP common end. Reuse maintained source auditors.
**Spec:** explicit user instructions 2026-10-27 and prior common-end/large-memory contracts; 300GB FST newly authorized, gem5 build -j128.

## Global constraints

- Four speed binaries, one native process each; original worker count scales4/8/16 (XZ active main adds one native task); no rate-copy substitution.
- 32GiB opt-in X86 low3GiB/high4GiB layout, eight channels per range, sixteen controllers/directories; buffer TSO114, same cache/CPU settings across cases except core count.
- Ten-million fastest-core measured user-UOP target inherited from current collection; one immutable global measurement end, retain all other actual work.
- Raw syscall option emits only actual committed micro-ops; no inferred counts patched into old sources, no validation tolerance.
- Total FST300,000,000,000 bytes, stop margin5,000,000,000, minimum free50,000,000,000. Atomic reservation counts in-flight remaining FST budgets; independent runtime free-space guard includes checkpoint/log growth. Never replace missing sources with short samples.
- Detailed warmup wall limit disabled (user authorized), positive instruction budget500,000 retained; whole-run bound48h protects stuck tasks. Checkpoints built fresh with current producer and matching hardware/workload identities.
- XZ uses smallest official refspeed cld.tar.xz2700MiB level8 consistently across core counts, replacing earlier research-scaled input only in this new campaign; real host ROI/RSS preflight required.
- No hardware PMU certification or CPI prediction report implied by successful collection. Unknown metadata remains unknown.

## Review focus

- Partial syscall at start/end/warmup, multiple reads and last-micro semantics, legacy feature compatibility.
- Post-switch census leakage; worker identities and unmarked same-process threads.
- Parallel budget oversubscription, disks shrinking from non-FST files, orphan producers and stale reservations.
- C16 official XZ admission and actual worker pool, missing computation/tasks or early application exit.
- Detached coordinator survives tool turn, status never treats exit0 or stale ready receipt as full acceptance.

## Tasks

- [x] Reproduce compact syscall over-expansion and add raw opt-in producer/reader/writer/auditor support with prefix1/12/19/24/25 and backward-compat tests.
- [x] Move native /proc census and tcdrain before KVM-to-O3; preserve parsable census labels and post-switch identity evidence; focused CLI regressions.
- [x] Build gem5 using128 jobs and embedded Python3.11, freeze built binary + runner/tools/config/shared inputs; required FastSim and targeted tests.
- [x] Freeze12-case manifest; host ROI/RSS admission including C16XZ official input; disk policy/admission regression.
- [x] Generic per-source preparation using independent hardware, common-end, thread/accounting, task-PMU and native Ruby/SLICC conservation tools; raw ABI metadata qualification separately.
- [x] Atomic bounded coordinator: new checkpoint then source per case, at most4 concurrent case workers, per-case FST budgets,48h whole-run guard, explicit failure/pending states.
- [x] Run raw-syscall FS smoke and full source audits before wider collection, fresh read-only review; resolve important findings without relaxing source rules.
- [ ] Launch12-case campaign, monitor receipts/current bytes/free space, complete source audits and document outcomes/failures.

## Decisions

Native initialization/census belongs under KVM; small committed identity/release handoff remains in retained warmup. A500K instruction-stop budget is not proof of cache convergence. Preserve all measured-prefix facts rather than delaying common end until a syscall completes. Existing raw event unit and hardware-equivalence limits remain.

Review: fresh read-only review found final-log milestone race and exited-leader/live-writer descendant gap in storage guard. Both reproduced RED, fixed GREEN; guard8/8 and combinedfocused29/29 pass. Full FastSim suite passes. Actual native FS smoke and12-source outcomes remain pending.

Final native FS instruction-checkpoint smoke passed source/native checks (Graph500C4/32GiB,100K fastest-core userUOP;399292 actualuserUOP), after legacyrawFLACC4smoke passed. Final12-case frozen queue and disk/log monitors running under tmp/spec-speed-32g-c4-c8-c16-20261027-final/. OriginaldeferredMMIOattempts retiredwithhashreceipts; O3instructionABI fixes confirmedlate-registersampling. Formal10M dataset outcomes remain in progress, not markedcomplete.
