# Input And Label Scheme

This document records the current functional-input schema and the previous
label-head schema for rollback.

## Input Schema

Current input is functional-only. Each uop is encoded as six fixed slots:

```text
<OP_*> <RG_*> <MK_*> <RD_*> <ST_*> <BR_*>
```

- `OP`: functional instruction class.
- `RG`: register dependency bucket.
- `MK`: memory kind.
- `RD`: bounded sliding reuse-distance bucket.
- `ST`: cacheline stride bucket.
- `BR`: branch type.

Each core segment also has functional summary tokens after `<C{i}_BEGIN>`:

```text
<SM_MEM_*> <SM_LD_*> <SM_STF_*>
<SM_DLINE_*> <SM_DPAGE_*>
<SM_RD*_*> <SM_STR*_*>
```

These encode memory ratio, load/store fraction, distinct lines/pages, RD
histogram, and stride histogram. They are derived only from the functional
address stream.

## Current Label Scheme: Scheme A

Scheme A keeps CPI and the auxiliary labels that are most directly tied to
functional control/memory behavior:

```text
cpi
mpki_br
mr_l1d_ld
mr_l1d_st
dtlb_miss
```

This is the default training/eval label set after this change.

## Previous Label Scheme: 10-Head

The previous scheme predicted all PMU heads:

```text
cpi
mpki_br
mr_l1d_ld
mr_l1d_st
mr_l1i
mr_llc
dtlb_miss
itlb_miss
inv_recv
mshr_avg
```

Reason for replacing it:

- `mr_llc`, `inv_recv`, and `mshr_avg` depend heavily on cache hierarchy,
  coherence, and queue state that are not fully observable from functional
  trace input.
- These heads can introduce negative transfer into the shared trunk.
- `mr_l1i` and `itlb_miss` may still be useful, but should be added back only
  after an ablation against Scheme A.

## Rollback Notes

To restore the previous 10-head label scheme, set `PMU_KEYS` in both files back
to the previous list:

```text
model/regression_head.py
data/build_windows.py
```

Then increment `feat_version` in:

```text
train/dataset.py
```

and rebuild windows/cache. Old caches are not compatible across label-schema
changes.

The pre-RD input/windowing checkpoint for broader rollback is:

```text
c2a7898 save pre-rd-input windowing baseline
```
