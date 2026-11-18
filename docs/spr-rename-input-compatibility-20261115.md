# SPR rename elimination input compatibility

The default consumer accepts static UOP semantics v1 instead of an exact
producer source SHA-256. The binary reader validates the v1 header/layout,
known flags, operand classes, per-record identity and functional dependency
projection. TraceSource exposes `static_uop_semantics_version()`; wrappers
forward it and custom/library sources default to unsupported (0). Changes to
field meanings require a new semantics/format version, independently of source
revision. Existing v1 producers need no recapture or header rewriting.

The original per-record eligibility checks remain mandatory: instruction bytes,
full macro boundaries, operand roles, MOV width, zero-idiom classification,
functional producers, pinning and special effects. Unknown versions, missing
fields and ineligible instructions execute normally. Producer hashes remain in
input provenance; no input hashes or companion fields are modified.

`core.spr_rename_require_source_policy=true` explicitly restores the historical
exact-hash gate in addition to semantic checks. Default is false. The existing
`core.spr_rename_policy_sha256` setting remains accepted for this rollback; it
does not gate normal operation. `core.spr_rename_elimination=false` still disables
elimination entirely. Frozen milestone configs and ELFs remain historical.

Stats expose `spr_rename_elimination` with enabled state, compatibility contract,
rollback selection, eliminated UOP count, missing-input UOP count and status.
Counts describe measured functional members, including kernel members, rather
than certified hardware elimination counts. Zero elimination with missing input
is reported explicitly. Actual CPI/PMU accuracy and host throughput require
separate source-bound evaluation; this interface change is not a milestone
accuracy certification.

The former executable is preserved in
`tmp/rename-compatibility-20261115/fastsim-before` (SHA-256
`6a67a3aa5c18a7640d89e8d4b8c95d133cb9be79fe548c8835fd49e5fe1db341`).
