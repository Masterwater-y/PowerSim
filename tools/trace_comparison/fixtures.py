"""Small functional FST fixtures, including an incomplete final macro."""

import pathlib
import struct


def make_fixture(root, cores=2, macros=128, tail=True, dependent=True):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for c in range(cores):
        records = []
        for i in range(macros + int(tail)):
            flags = 1 | 1024 | (2048 if i < macros else 0)
            dep = 1 if dependent and i else 0
            records.append(
                struct.pack(
                    "<QQQQ4IHHhBB4BI",
                    0x1000,
                    0,
                    0,
                    0,
                    dep,
                    0,
                    0,
                    0,
                    0,
                    flags,
                    1,
                    int(bool(dep)),
                    1,
                    255,
                    255,
                    255,
                    255,
                    0,
                )
            )
        path = root / f"core{c}.fst"
        path.write_bytes(
            struct.pack(
                "<8sIIIIQQ4Q", b"FSTRC01\0", 7, 72, 64, c, len(records), 32, 0, 0, 0, 0
            )
            + b"".join(records)
        )
        pathlib.Path(str(path) + ".deps").write_bytes(
            struct.pack("<8sIIIIQQQ", b"FSTDEP1\0", 1, 48, c, 0, len(records), 0, 0)
        )
        pathlib.Path(str(path) + ".imap").write_bytes(
            struct.pack(
                "<8sIIIIQQII", b"FSTIMP1\0", 1, 48, 32, c, len(records), 1, 1, 0
            )
            + struct.pack("<QQQHB5s", 0x1000, 0x1004, 0, 0, 4, b"\0" * 5)
        )
        entries.append(
            f"{c} binary-warmup-slice {path.name} {c} 0 {macros} 0 {len(records)}"
        )
    manifest = root / "manifest.txt"
    manifest.write_text("\n".join(entries) + "\n")
    (root / "code.cfg").write_text(
        "trace.instruction_address_mode = modeled\ndram.size = 34359738368\ndram.memory_layout = x86-low3g-high4g-v1\n"
    )
    return manifest


def add_dependencies(root, degree):
    """Keep all producers resident behind a long-latency head instruction."""
    root = pathlib.Path(root)
    for f in sorted(root.glob("core*.fst")):
        blob = bytearray(f.read_bytes())
        core = struct.unpack_from("<I", blob, 20)[0]
        count = struct.unpack_from("<Q", blob, 24)[0]
        last = 72 + (count - 1) * 64
        struct.pack_into(
            "<h", blob, 72 + 52, 11
        )  # long-latency floating sqrt at ROB head
        for i in range(count):
            struct.pack_into("<4I", blob, 72 + i * 64 + 32, 0, 0, 0, 0)
            struct.pack_into("<B", blob, 72 + i * 64 + 54, 0)
        struct.pack_into("<4I", blob, last + 32, 1, 2, 3, 4)
        struct.pack_into("<B", blob, last + 54, degree)
        f.write_bytes(blob)
        h = 2166136261
        for b in blob[last : last + 64]:
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        extras = list(range(5, degree + 1))
        pathlib.Path(str(f) + ".deps").write_bytes(
            struct.pack(
                "<8sIIIIQQQ", b"FSTDEP1\0", 1, 48, core, 0, count, 1, len(extras)
            )
            + struct.pack("<QII", count - 1, len(extras), h)
            + struct.pack("<" + "I" * len(extras), *extras)
        )


def memory_at_first_record(root, role=0, serialize=False, write=False):
    for f in sorted(pathlib.Path(root).glob("core*.fst")):
        b = bytearray(f.read_bytes())
        core = struct.unpack_from("<I", b, 20)[0]
        count = struct.unpack_from("<Q", b, 24)[0]
        struct.pack_into("<Q", b, 32, 96)
        for i in range(count):
            struct.pack_into("<4I", b, 72 + i * 64 + 32, 0, 0, 0, 0)
            struct.pack_into("<B", b, 72 + i * 64 + 54, 0)
        struct.pack_into("<Q", b, 80, 0x100000)
        struct.pack_into("<H", b, 120, 8)
        struct.pack_into(
            "<H", b, 122, 1 | (4 if write else 2) | 1024 | 2048 | 4096 | (8192 if serialize else 0)
        )
        f.write_bytes(b)
        h = 14695981039346656037
        for byte in b[72:]:
            h = ((h ^ byte) * 1099511628211) & ((1 << 64) - 1)
        rows = b""
        if role:
            hot = 2166136261
            for byte in b[72:136]:
                hot = ((hot ^ byte) * 16777619) & 0xFFFFFFFF
            rows = struct.pack("<QIIB7s", 0, hot, 0, role, b"\0" * 7)
        pathlib.Path(str(f) + ".mreq").write_bytes(
            struct.pack(
                "<8sIIIIQQQII",
                b"FSTMREQ1",
                2,
                56,
                core,
                1,
                count,
                int(bool(rows)),
                h,
                24,
                0,
            )
            + rows
        )
