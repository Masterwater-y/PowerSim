# TSim v28 business-oriented workload set

This workload set targets the planned deployment families: Marine online
search/ads/recommendation, gofeed online microservices, Flink streaming/batch
data processing, MySQL, Redis, PyTorch AI-head compute, and BVC encoding.

The measured ROI contains no atomic, lock, barrier, spin, futex, yield, sleep,
or explicit scheduling operation.  Allocation, first touch, thread creation,
pinning, and the start barrier all happen before `WORKBEGIN`.  Business state
is private per core; immutable metadata may be shared.  The sparse-coherence
anchor writes only disjoint per-thread words that may share cache lines.

Build:

```bash
make -C workloads/dr_validation all
make -C workloads/dr_validation dynamoRIO
```

Sets:

- `train`: nine mechanism anchors plus one base proxy for each of seven
  deployment families (16 total).
- `heldout`: one unseen but business-plausible variant for each deployment
  family (7 total).
- old v27 pointer chase, pure DRAM saturation, and all-core shared-write tests
  are stress diagnostics only; they are not v28 train or business heldout.

All traces use the same cold-start collector contract as v27.  Cold start is
retained, but every memory-heavy kernel interleaves independent memory
requests with useful compute so the beginning of the ROI is not a pure miss
burst.
