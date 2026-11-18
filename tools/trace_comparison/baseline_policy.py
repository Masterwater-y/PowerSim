"""User-selected zsim throughput baseline; serial is an explicit historical control."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ZSIM_PARALLEL_BASELINE = (
    ROOT / "tmp/e2e-throughput-20261116/campaign-zsim-parallel/zsim-frozen"
)


def require_parallel_zsim(statistics, historical=False):
    workers = statistics.get("preparation_workers", 1)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("invalid zsim preparation-worker metadata")
    if statistics.get("cores", 2) > 1 and workers == 1:
        if not historical:
            raise ValueError(
                "zsim primary throughput requires parallel file preparation; use --historical-serial-zsim for archived serial controls"
            )
        return "historical-serial"
    return "parallel-files"
