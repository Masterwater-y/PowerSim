# Functional Device I/O Service Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline, with test-driven-development and verification-before-completion.

**Goal:** Restore source-configured UART/APIC response service and strict ordering for existing functional requests; validate Zstd C4/C8 in quick mode with functional TLB enabled.

**Architecture:** A bounded stateless address classifier supplies device kind and tick-derived service cycles. Carry this descriptor through the existing two-stage response weave: reads complete after device response; writes retire normally and retain their SQ entry through post-retirement device response. Existing per-core memory-barrier/frontier state carries ordering across epochs; no global device serialization or additional trace traffic.

**Spec:** The approved design in the current conversation and `tmp/zstd-non-tlb-diagnosis-20261116/report.md`.

**Constraints:** Retired functional input only. No native timestamps/outcomes as input. Preserve immutable M1/M2. Generic device model remains opt-in. Bind device addresses/latencies and CPU tick period per source. Unknown escaped devices remain explicitly unmodeled. Preserve functional population and cache traffic. All scratch under project tmp.

**Review focus:** Device address/size overflow; unknown or nonphysical addresses; read RAW readiness; writes must not wait for response before retiring; cross-epoch ordering and cross-core independence; fast certificate must not bypass devices; OFF neutrality.

- [x] Add `tests/test_device_io.cpp`: classification, source tick rounding, overflow, unknown devices, behavioral read/write timing, cross-epoch dependency and C4 independence. Observe RED before implementation.
- [x] Add `DeviceIoConfig` and `device_io.hpp`, config validation/parser, explicit configuration and request/service-volume diagnostics. Verify classifier tests and behavioral RED.
- [x] Integrate compact descriptors in `src/simulator.cpp`; restore device read completion and post-retire store release through existing memory-order carry. Verify runtime tests with certificate ON/OFF and multiple epochs.
- [x] Build Release using existing parallel LTO; run fastsim_tests and relevant runtime tests. Preserve pre-change binary/source identity and review the complete new diff.
- [x] Freeze candidate; run paired device OFF/ON with TLB ON on C4/C8 Zstd, source-bound device parameters, maintained quick scorer, macro/UOP identity checks. Report absolute CPI error and MAE as well as relative errors; throughput separately.
- [x] Document results, limits and opt-in/rollback settings. No commit/push requested.

Execution ledger: `tmp/device-io-service-20261116/progress.md`. Existing dirty SPR source snapshots are preserved under its `before/` directory. Work proceeds in place on fastsim-spr to retain the authorized uncommitted TLB baseline.
