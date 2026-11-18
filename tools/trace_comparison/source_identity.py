"""Bind completed runs to unchanged local files using SHA256 and inode times.

This retrospective check requires local filesystem ctime semantics. It rejects
any input touched since the earliest campaign prelaunch receipt, including
same-size edits and file replacement. It is not an adversarial storage proof.
"""

import argparse
import concurrent.futures
import hashlib
import json
import pathlib


def stat_identity(path):
    s = pathlib.Path(path).stat()
    return dict(
        device=s.st_dev,
        inode=s.st_ino,
        size_bytes=s.st_size,
        mtime_ns=s.st_mtime_ns,
        ctime_ns=s.st_ctime_ns,
    )


def audit_artifact(artifact, cutoff_ns):
    path = pathlib.Path(artifact["path"])
    before = stat_identity(path)
    if before["size_bytes"] != artifact["size_bytes"]:
        raise ValueError(f"artifact size changed: {path}")
    if max(before["mtime_ns"], before["ctime_ns"]) >= cutoff_ns:
        raise ValueError(f"artifact changed since campaign prelaunch: {path}")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    if stat_identity(path) != before:
        raise ValueError(f"artifact changed during hashing: {path}")
    if h.hexdigest() != artifact["sha256"]:
        raise ValueError(f"artifact differs from FastSim consumed identity: {path}")
    return dict(
        path=str(path), role=artifact["role"], sha256=h.hexdigest(), stat=before
    )


def check_inventory(identity):
    """Reject optional companions added/removed after FastSim took its identity."""
    directories = {}
    for source in identity["sources"]:
        artifacts = source["artifacts"]
        fst = pathlib.Path(next(a["path"] for a in artifacts if a["role"] == "fst"))
        expected = {str(pathlib.Path(a["path"])) for a in artifacts}
        current = {str(f) for f in fst.parent.glob(fst.name + "*") if f.is_file()}
        if current != expected:
            raise ValueError(f"functional companion inventory changed: {fst}")
        if any(pathlib.Path(p).is_symlink() for p in current):
            raise ValueError("symlinked trace artifacts require an immutable snapshot")
        directories[str(fst.parent.resolve())] = stat_identity(fst.parent)
    return directories


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", type=pathlib.Path, required=True)
    p.add_argument("--sniper-subdir", default="sniper-repaired")
    a = p.parse_args()
    folders = [a.campaign / name for name in ["fastsim", "zsim", a.sniper_subdir]]
    cutoff_ns = min((f / "freeze.json").stat().st_mtime_ns for f in folders)
    summary = json.loads((folders[0] / "summary.json").read_text())
    if len(summary) != 10:
        raise ValueError("require full ten-case FastSim matrix")

    def one(row):
        identity = json.loads(pathlib.Path(row["statistics_file"]).read_text())[
            "functional_source_identity"
        ]
        if identity["status"] != "complete":
            raise ValueError("incomplete FastSim functional source identity")
        directories = check_inventory(identity)
        if any(
            max(s["mtime_ns"], s["ctime_ns"]) >= cutoff_ns for s in directories.values()
        ):
            raise ValueError("trace directory namespace changed after prelaunch")
        artifacts = [
            audit_artifact(art, cutoff_ns)
            for source in identity["sources"]
            for art in source["artifacts"]
        ]
        if check_inventory(identity) != directories:
            raise ValueError("trace directory namespace changed during hashing")
        print(
            row["case"], len(artifacts), "SHA256 and prelaunch ctime match", flush=True
        )
        return row["case"], dict(
            source_identity=identity["source_identity"],
            artifacts=artifacts,
            directories=directories,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        cases = dict(pool.map(one, summary))
    receipt = dict(
        policy="local-sha256-and-prelaunch-ctime-v1",
        cutoff_ns=cutoff_ns,
        adversarial_storage_proof=False,
        stable_ancestor_paths_and_clock_assumed=True,
        cases=cases,
    )
    (a.campaign / "input-identity.json").write_text(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
