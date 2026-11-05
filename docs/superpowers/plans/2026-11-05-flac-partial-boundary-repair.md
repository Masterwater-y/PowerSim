# FLAC partial-boundary reader repair

The FLAC capture has partial architectural entry terminals emitted while the
containing FFE macro is still open. Preserve the original source and all bundle
companions. Never promote a partial event to a complete boundary.

1. Add a regression for a partial entry ending inside an otherwise complete
   frontend macro, including rejection of forged begin, end and identities.
2. Defer only the partial terminal's macro cross-check until its containing FFE
   macro is observed; keep event/member hashes, identity, footer and status
   validation. Bound outstanding deferred events and reject unconsumed entries.
3. Run the focused regression and full FastSim tests, then retry the original
   four-core warmup-plus-ROI capture with all companions enabled. Record errors.
4. Separately check whether the execution runtime consumes each companion; an
   accepted reader is not proof of MMU, boundary or core semantic execution.
   Do not claim full alignment or score incompatible native timing windows.
