# Causal instruction supply through the shared CPU Sequencer

Next bounded P4/F2 runtime slice after F2c at de32229. The actual target has
fetch_supply_model, L1I, physical instruction ledger and lower hierarchy enabled;
these remain actual entry guards. Existing FFE and configured modeled instruction
address policy are present. Reuse the existing instruction-and-walker-routing
audit in the plan ledger; no new general source audit or collection project.

## Required ownership and source rules

Connect the existing causal retired frontend to real model-derived I request
and response edges, actual cache storage and NativeCoherence/SharedSystem DRAM.
Never disguise an I-cache miss as a retired data load or synthesize a data-UOP
callback. No source/native timing, hits, source-choice outcomes, wrong-path,
fixed-point, hidden legacy completion or constant lower-hierarchy response.
Preserve default/OFF behavior and all unsupported gates not implemented here.

Add a default-OFF causal instruction-fetch adapter requiring the causal retired
frontend, native memory/coherence, enabled L1I, configured request ledger/lower
hierarchy and supported geometry. Source/MMU/class/replacement/free-list and
unqualified shared-group gates stay separate. This slice is not full P4.

Source topology is authoritative: IFETCH/data/ITB-walk/DTB-walk share the physical
CPU's one ordinary16 Sequencer table. Optional store-prepare16 is separate.
Private L2 has no CPU Sequencer64; those64 objects are DMA. Reuse the existing
real SequencerKind::Ifetch and ordinary callback FIFO/coalescing identities.
Instruction packets bypass LSQ load/store send ports and L1D load-bank gates.
The native IFETCH mandatory entry omits Dcache.dataLatency-1; use the sourced
entry/action latencies without double-charging the existing no-L1I refill rule.

The real L0 controller shares its TBE/transition budgets between I/D, with distinct
Icache/Dcache arrays. Native Ifetch cold allocation uses Icache + shared TBE,
not a Dcache fill; Icache fill_capacity0 is unlimited. Private unified L2 cold
allocation uses its existing shared fill48/TBE resources and downstream routing.
Store hints remain their current typed optional path. No second ordinary table,
directory, LLC, DRAM controller, global prepare map or independent timing loop.

Use a typed instruction request carrying its real ordinary service/owner and
immutable instruction address provenance/placement. Physical transaction origin
must retain these fields through sends/forwarding/response/stale validation;
attaching followers never rewrites the physical origin. Implementer chooses the
minimal exact C++ interface in bounded preflight before edits. Extending the
existing tagged origin and storage hooks is preferable to an untyped side map
or pretending the service is a data read. Keep generation/token validation and
reject malformed envelopes before resource/cache/protocol mutation.

Add source Inst_IS/IFETCH actions to the existing native controller subset and
route storage operations to the correct real array. I fills do not create an
architectural write. Preserve parent dirty debt and symbolic data. Invalidations
and callbacks must use the resident array/real consumer kind, not merely the
origin of the newest requester. Native I/D same-PA dual residency is forbidden;
if an opposite-array transfer or capacity replacement needs unsupported existing
replacement machinery, reject before mutation and report it. This task does not
release the separate C1 private replacement project or silently drop victims.

## Explicit instruction address policy

Preserve the actual configured modeled instruction policy, geometry and seed.
Existing modeled_instruction_physical_address generates a synthetic cache
namespace plus dense DRAM placement. Keep cache identity distinct from its
model-derived physical routing/DRAM placement; never feed the high namespace to
a real physical directory range lookup or silently turn it into a data alias.
Use maintained DRAM geometry helpers to translate dense placement over actual
split RAM ranges, validating the relation/bounds before ownership changes.
Do not change target capacity, hash seed, address width, bank geometry or source.
Expose modeled placement/collision/no-native-physical-identity as an explicit
approximation, including the I/D sharing boundary. No strict physical identity
or hardware-equivalence claim follows from this configured policy.

The native protocol primitive may support exact physical I addresses for bounded
alias/ownership tests. If the production frontend accepts trace/exact mode,
copy its actual bound mapping facts through existing source getters and require
complete correct identity; never fall back to modeled on a missing mapping.
It is also valid to keep production exact mode explicitly unqualified in this
first modeled-policy slice. No new schema/capture is authorized by this spec.

## Frontend/coordinator integration

Emit a typed frontend instruction demand at the actual model fetch edge, before
rename. Track it independently of data-UOP/ROB completion, with bounded live
owners and the real ordinary capacity. At most one physical Fetch request per
physical core/thread is active, including cross-buffer instruction service.
Preserve exact instruction span: normal macros may need up to two64-byte blocks;
page translations need not be adjacent. Pseudo records do not fabricate bytes
or I packets. Model-selected DSB byte bypass stays the existing explicitly
qualified retired-calendar proxy, not an imported native DSB hit.

Only a real returned instruction service releases its ordinary owner and makes
bytes ready to the existing SprFrontend. Do not return IDQ at fetch response;
actual rename still owns that acknowledgement. A data follower and instruction
follower on one service have distinct consumer completions and populations;
never complete a frontend request by calling the data backend's on_completion.
A full ordinary table stalls frontend admission while older ROB/store/protocol
work continues. New arrivals and I response wakes enter the existing safe event
coordinator and obey its exclusive known-arrival frontier; no future I request
is published past an unknown earlier producer. Avoid stalled per-cycle polling.

Keep source graph parking, frontend cursors/bytes, pending request/generation,
DSB/IDQ and backend state across chunks/warmup. EOF closes input, not live I work.
Core local origin/end accounting remains tied to actual retained retirements;
detached stores/refresh/protocol cannot add CPI tail. Counters distinguish real
I requests/accesses/fills/coalesced consumers from D demand and retired source
records/macros; no extra trace/retirement population is manufactured.

Current retired batching, full native code-lease/ITLB qualification, physical
free-list and other existing approximations remain explicitly unqualified. Do
not claim physical code-lease perfection by connecting byte-ready ownership.
Lift only actual I-side composition guards after the implemented path exists.
DTLB/class/replacement/shared-group gates remain; no futile full-target retry
while known independent configuration adapters are still absent.

## Tests and acceptance

Bounded preflight verifies exact native Ifetch action, storage, entry latency,
Sequencer callback and address-routing contracts before code. Report concrete
contradictions rather than change source semantics or disable target features.
Write genuine REDs for causal frontend+L1I and native instruction ingress.

Use existing production fixtures/targets where practical. Required witnesses:
actual Simulator Binary+FFE, actual I cache miss/hit/DSB byte bypass and response
wake; cross-buffer/page service and modeled split-range routing; ordinary16
shared I/D pressure vs optional16, no LSQ load-bank/port charge for I; true L0
TBE/no-Dfill and L2 fill accounting; same-line real coalesced consumers and
malformed/stale origin rejection; unsupported opposite-array/replacement before
mutation. Direct protocol fixtures supplement real Simulator evidence.

Explicitly cover the previously unqualified combined-mode integration: actual
new frontend + I-side + coherent early stores, reserve/allocate/prepare/takeover/
write-release identities and full tuple invariance across chunks1/2/7/128 and
host delay. Include warmup while I request/rename live, graph park, unequal local
origins/EOF and detached-tail clocks. No native/captured-input qualification is
claimed from constructed binary fixtures. Source STOP, one scoped independent
review, canonical affected/default tests, actual CLI readback and historical OFF
compatibility precede checkpoint. Full FLAC/four-case CPI/PMU and throughput
remain separate eventual gates with a genuinely matched reference.
