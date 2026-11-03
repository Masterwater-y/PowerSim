# DRAM mechanism alignment audit implementation plan

Goal: distinguish configuration errors, controller semantic gaps and ordering
sensitivity on the current Marian/G500 sources before changing target timing.

The user authorized implementation directly. Execute inline, without gem5
recapture, global timing iteration, fitted constants or workload-specific rules.
Use the maintained profile audit for existing parameter identity; the new audit
adds every controller, write timings and explicit semantic coverage. It does not
select PMU references or replace the native validator.

- [x] Test and implement an all-controller DRAM configuration/coverage audit.
  Pin heterogeneous controllers, missing evidence, tick conversion, unsupported
  mapping and explicit refresh limitations. Record input SHA256.
- [x] Expose the production controller's address decoder through a read-only
  testing probe; compare deterministic boundary/random samples against gem5's
  AddrRange and RoRaBaCoCh decoding rules from the actual config.ini.
- [x] Run small fixed-request experiments through the production DRAM probe:
  same-row, row conflict, independent bank/channel, arrival-density changes,
  page-policy/row-cap variants. These are sensitivity tests, not CPI predictions.
- [x] Build and run the repository tests and targeted checks. Audit both current
  sources, document findings and prioritize only demonstrated mechanism fixes.

Acceptance: complete audit artifacts and a source-bounded report. If an isolated
counterexample demonstrates a new implementation error, repair it with a
regression; otherwise do not change production timing on aggregate evidence.
No commit/push or throughput/accuracy improvement claim is part of this audit.

## Evidence-driven continuation: single-PRE A/B

- [x] Replay both current P1 inputs with the frozen original binary and only
  `dram.frfcfs_row_cap_single_precharge=true`; retain unchanged source receipts,
  check exact effective configuration difference, and use the maintained
  native validator for the resulting CPI table and CPI MAE.
- [x] Document per-core and makespan changes; keep the candidate opt-in pending
  broader accuracy and independent throughput validation.

Expected direction: removing duplicate PRE shortens isolated service. It may
improve slow Marian and worsen fast G500. Global makespan can change through
reordering and overlap; do not promise monotonic workload gains or fit a size.
