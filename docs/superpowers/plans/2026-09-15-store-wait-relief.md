# Partial store-wait repair within the two-stage framework

Authorized by the user's 2026-09-15 request: reduce some of the identified mechanism error without replacing the two-stage framework. Preserve a default-off rollback and avoid scalar fitting.

1. Snapshot the maintained dirty baseline and isolate the candidate. Completed in `tmp/lbm-store-wait-relief-20260915-czins1ei/`.
2. Carry explicit ordinary-store ownership and full DRAM response into a local max-plus counterfactual. Track PRE ownership explicitly and require bank-command benefit; leave unowned ACT/window constraints intact; no future arrival projection. Implemented.
3. Consume only at a covered TSO store-drain edge, cap by origin displacement and service lower bound, and aggregate fragments by max. Invalidate certificates after controller replacement; constrain incompatible timing modes. Implemented and reviewed.
4. Validate ownership/exclusion, exact rollback, real positive consumption, multi-fragment release, cross-Q/warmup state, accepted-only counters and runtime DVFS rejection. Passed the isolated full C++ suite and Python mechanism/audit tests.
5. Run frozen full common-end LBM C4/C8/C16/C32 on/off; require C32 absolute error improvement and inspect four-case CPI MAE and underprediction regressions. Completed: C32 abs improves 14.20%, four-case CPI MAE improves 14.10%; C16 slightly regresses. The broader v1 failed and is retained only as history.
6. If directional gate passes, rerun frozen positive/negative windows and non-LBM controls, record identity/throughput/accuracy provenance, then decide whether to retain an opt-in candidate in maintained sources. Completed: both frozen windows improve; tealeaf unchanged, stockfish minimally regresses; NAMD rejected on population mismatch. Final bank-gated candidate retained in maintained sources, default off. Maintained build, full C++ suite and both Python suites passed; maintained and isolated binary hashes are identical. Validation is recorded in the main report and final-verification.json.

Follow-up requested by the user: further optimize the effective version. An isolated
ACT-owner extension was designed, implemented and tested with the old bank gate
preserved, separate base/extra parent certificates, a default-off rollback, mixed-tie
exclusion and fragment-aware clipping. Full C++ and three Python suites pass.
Thirteen full matrix/control/rollback runs and both frozen audits completed. ACT
regresses all four LBM absolute errors; CPI MAE 0.100394→0.102885. C32 signed error
+6.4665%→+6.5913%, absolute CPI error 0.252580→0.257455 cycles/macroinstruction.
Both windows regress; non-LBM controls are unchanged. The extension is rejected,
maintained v2 files/binary remain identical, and no scalar is tuned. Evidence and
reproduction scripts: `tmp/lbm-store-act-relief-20260915-cwyc6iww/`.

Next investigation is not an approved-for-promotion repair: trace the first changed
accepted request, including warmup, and separate direct local credits from induced
raw service/arrival changes. Require paired ownership/order evidence and the same
full-case, per-core and bidirectional window gates before another coverage change.
