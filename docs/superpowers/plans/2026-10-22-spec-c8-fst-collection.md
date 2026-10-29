# Eight-core FST collection plan

> Execute inline with the existing collection workflow. User authorized collection and retirement of confirmed superseded/failed diagnostic FSTs; no additional approval or automatic commit.

**Goal:** Collect C8 sources for SQLite/Zstd rate and Marian/Graph500/XZ/FLAC speed binaries, with explicit input qualification.
**Architecture:** Reuse proven marked binaries and frozen runner; new C8 staged KVM checkpoints, computational ROI handoff, bounded O3 warmup, common-end stat. Experimental orchestration lives in `tmp/spec-c8-fst-20261022/`; no simulator model changes.
**Spec:** User request and existing `docs/spec-marian-speed-collection-20261020.md`, `docs/spec-graph500-xz-flac-speed-roi-20261020.md`, `docs/trace-storage-policy-20261015.md` and accuracy contract sections0/8.

## Constraints

- Preserve current six full C4 sources, synchronization/PMU probes, CPython smoke and ALS inputs. Retire only enumerated superseded recovery/short diagnostics, with SHA256/inode/size receipts and per-directory non-replayable markers. Preserve logs, sidecars and checkpoints.
- Current user-authorized FST cap200,000,000,000 bytes; stop199GB; disk free>=20GB. User-authorized maximum four parallel workload workers with isolated state/CPU placement and atomic byte reservations; per-full-case cap10GB with flush margin.
- Eight physical participants, eight memory channels, buffer114,3GiB. SQLite/Zstd use eight independent rate copies; native speed uses one process and eight compute workers (XZ also counts active producer).
- Initial warmup1M gem5 instruction-stop counts,180s wall limit;10M measured user UOP target on fastest core; user+kernel and warmup histories retained. Do not wait for a full application iteration as warmup.
- Execution ruling:1M eight-core pilots exceeded180s before stat, so production queue is frozen at500K instruction-stop counts with the same180s cap. Graph500100K recovery passed all eight-core common-end/owner/identity gates at168.51s. This changes bounded warmup, not target hardware, and is not a steady-state claim.
- User-authorized wall-time revision: retain500K instruction-stop warmup, relax detailed warmup guard180→600s. Archive failed pre-stat attempts and restart only after old driver/publisher exit; preserve qualified Graph500 and all current C8 checkpoints. Command guard and post-capture acceptance share the frozen wall limit.
- User-authorized recovery storage revision: combined warmup+100K recovery budget8GB (was2.5GB), formal10GB/global100GB unchanged. Retire only failed diagnostics or recovery replaced by a qualified full source, with per-file hash/attribute journals; persist source completion before retirement. Cleanup refusal leaves source valid and global guard active.
- New checkpoint identities bind actual C8 geometry, immutable producer/workload/input and runner. Never restore C4 checkpoints with C8 geometry.
- User selected scaled research input for XZ:96MiB derived from official train CR2 input,level4; preserve source binary/algorithm and mark nonstandard input. Host execution must demonstrate eight encoder workers and expected bounded memory before guest capture.
- User subsequently authorized reduced workspace and mini-batch for Marian research capture. Eight-worker ROI RSS remained4.90GB for workspace64/32MiB and mini-batch32,4.11GB for mini-batch8,3.90GB even for mini-batch1. Mark Marian `blocked-capacity`; do not launch a3GiB guest capture or silently change beam/model/thread count.
- After one C8 guest IO-APIC timer boot panic at KVM quantum10us, use the runner default1000us for fresh C8 checkpoints. This is a KVM fast-forward setting; detailed O3 hardware profile remains unchanged. Preserve boot-failure and retired initial-launcher checkpoint evidence.
- CPython is not included: it has no completed C4 full source; this campaign follows the six-case estimate the user accepted.

## Review focus

Wrong checkpoint geometry, missing worker admission, guest OOM/swap, repeated ROI, and silently treating scaled XZ as official train/refspeed must reject qualification or be explicitly reported. A producer exit0 is not capture success.

## Tasks

- [x] Inventory, freeze protected sources and retire32 superseded diagnostic FSTs (5.654991296GB); attributes of24 protected full C4 FSTs unchanged.
- [x] Freeze existing runner, helper/config snapshot, producer and workload identities; prepare C8 command matrix.
- [x] Host worker/memory preflight for the five active cases; unchanged input correctness evidence retained, scaled XZ original/marked roundtrip checked. User explicitly deferred Marian after authorized workspace/mini-batch tests still exceeded capacity.
- [ ] New C8 checkpoint and100K recovery gates, then10M common-end source per workload. Save per-phase status and failures; independent cases continue.
- [ ] Prepare maintained common-end/identity/ownership and syscall-population audits for successful full sources; report exact completed/failed/pending case set and storage.

Ledger and artifacts: `tmp/spec-c8-fst-20261022/progress.md`, command matrix, cleanup plan/journal/receipt, frozen identities and campaign status. No new CPI/PMU accuracy claim is part of collection alone.

Execution update: user requested maximum parallelism. Independent FLAC/XZ/SQLite/Zstd workers configured; original FLAC601.75s pre-stat warmup timeout is a failed attempt, not an active lane. Three other collectors verified concurrently live. Atomic snapshot reservations and all-wait/no-producer detection preserve the global storage boundary.

User-authorized FLAC retry exception: dedicated1800s warmup/16GB smoke+full/3600s smoke-total/7200s full-total, same500K instruction-stop budget. Other workers retain600s/8GBsmoke/10GBfull. Original producer/ROI/checkpoints/hardware unchanged; all share99GB admission and100GB globalcap. Actual FLAC launch verified; original other workers not restarted.

User-authorized XZ retry:1800s detailed warmup,9GB smoke/12GBfull,3600/7200 total wall caps; retain500K and96MiB research input. Other three workers preserved, shared99GB admission/global100GB maintained. Actual launch and frozen file hashes verified; independent review found no newP1/P2.

2026-10-22 capacity revision: user explicitly raised global limit100→200GB, admission199GB. SQLite formal source was already auto-started before policy update and is preserved. FLAC-only waiting worker reloaded new policy; other producers preserved. Existing in-flight guards retain100GB launch snapshots until their phases finish; new stages read200GB. Historical receipts remain immutable.

User explicitly chose SQLite900s (overrides proposed1800). Dedicated driver/audit both use900s, retain500K/8GBsmoke/10GBfull and1800/4200 total wall limits. Reuse qualified recovery+checkpoint; failed600s source archived. Actual new formal launch verified with200GB policy. FLAC/XZ1800s configs unchanged; aggregate now3/5sources qualified.
