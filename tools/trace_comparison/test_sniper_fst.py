"""Regression for residual UOPs at a macro boundary in the native Sniper ROB."""

import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from fixtures import make_fixture, add_dependencies, memory_at_first_record

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNIPER_TREE = pathlib.Path(
    os.environ.get(
        "TRACEBENCH_SNIPER_TREE",
        str(ROOT / "tmp/native-trace-adapters-20261116/sniper-source-final"),
    )
)


class SniperFstTest(unittest.TestCase):
    def test_tail_is_executed_without_extra_macro(self):
        source = SNIPER_TREE
        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-sniper-tail-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture)
        cmd = [
            str(source / "run-sniper"),
            "--build-dir=build/fst-native",
            "-c",
            str(ROOT / "tmp/sniper-zsim-spr-20261112/sniper/config/spr_m1_fastsim.cfg"),
            "-d",
            str(fixture / "output"),
            "-g",
            "general/total_cores=2",
            "-g",
            "traceinput/num_apps=2",
            "-g",
            f"traceinput/fst_manifest={manifest}",
            "--sim-end=last",
            "--trace-manual",
        ]
        env = dict(os.environ, SNIPER_SIM_LD_LIBRARY_PATH="/opt/gcc-11/lib64")
        p = subprocess.run(
            cmd, cwd=source, env=env, capture_output=True, text=True, timeout=60
        )
        (fixture / "run.log").write_text(p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr)
        con = sqlite3.connect(fixture / "output/simulation/sim.stats.sqlite3")
        q = "select v.value from 'values' v join names n using(nameid) where n.objectname=? and n.metricname=? and v.prefixid=3 order by v.core"
        self.assertEqual(
            [r[0] for r in con.execute(q, ("core", "instructions"))], [128, 128]
        )
        self.assertEqual(
            [r[0] for r in con.execute(q, ("rob_timer", "uops_total"))], [129, 129]
        )
        log = (fixture / "output/simulation/sim.stdout").read_text()
        self.assertEqual(log.count("[FST-BEGIN:"), 2)
        self.assertEqual(log.count("[FST-ENDPOINT:"), 2)

    def test_extended_dependencies(self):
        source = SNIPER_TREE
        for degree in [9, 15]:
            with self.subTest(degree=degree):
                fixture = pathlib.Path(
                    tempfile.mkdtemp(prefix="native-sniper-degree-", dir=ROOT / "tmp")
                )
                manifest = make_fixture(fixture, macros=32, tail=False)
                add_dependencies(fixture, degree)
                cmd = [
                    str(source / "run-sniper"),
                    "--build-dir=build/fst-native",
                    "-c",
                    str(
                        ROOT
                        / "tmp/sniper-zsim-spr-20261112/sniper/config/spr_m1_fastsim.cfg"
                    ),
                    "-d",
                    str(fixture / "output"),
                    "-g",
                    "general/total_cores=2",
                    "-g",
                    "traceinput/num_apps=2",
                    "-g",
                    f"traceinput/fst_manifest={manifest}",
                    "--sim-end=last",
                    "--trace-manual",
                ]
                p = subprocess.run(
                    cmd,
                    cwd=source,
                    env=dict(
                        os.environ, SNIPER_SIM_LD_LIBRARY_PATH="/opt/gcc-11/lib64"
                    ),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                (fixture / "run.log").write_text(p.stdout + p.stderr)
                self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr)
                con = sqlite3.connect(fixture / "output/simulation/sim.stats.sqlite3")
                q = "select v.value from 'values' v join names n using(nameid) where n.objectname=? and n.metricname=? and v.prefixid=3 order by v.core"
                self.assertEqual(
                    [x[0] for x in con.execute(q, ("rob_timer", "fst_extra_declared"))],
                    [degree - 4] * 2,
                )

    def test_noaccess_and_serialization_are_preserved(self):
        source = SNIPER_TREE
        fixture = pathlib.Path(
            tempfile.mkdtemp(prefix="native-sniper-role-", dir=ROOT / "tmp")
        )
        manifest = make_fixture(fixture, macros=1, tail=False)
        memory_at_first_record(fixture, role=1, serialize=True)
        cmd = [
            str(source / "run-sniper"),
            "--build-dir=build/fst-native",
            "-c",
            str(ROOT / "tmp/sniper-zsim-spr-20261112/sniper/config/spr_m1_fastsim.cfg"),
            "-d",
            str(fixture / "output"),
            "-g",
            "general/total_cores=2",
            "-g",
            "traceinput/num_apps=2",
            "-g",
            f"traceinput/fst_manifest={manifest}",
            "--sim-end=last",
            "--trace-manual",
        ]
        p = subprocess.run(
            cmd,
            cwd=source,
            env=dict(os.environ, SNIPER_SIM_LD_LIBRARY_PATH="/opt/gcc-11/lib64"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr)
        con = sqlite3.connect(fixture / "output/simulation/sim.stats.sqlite3")
        q = "select v.value from 'values' v join names n using(nameid) where n.objectname=? and n.metricname=? and v.prefixid=3 order by v.core"
        # Sniper omits zero values from its sparse SQLite snapshots. Require
        # the metric registration before interpreting an absent value as zero.
        self.assertEqual(
            con.execute(
                "select count(*) from names where objectname='rob_timer' and metricname='loads-count'"
            ).fetchone()[0],
            1,
        )
        loads = dict(
            con.execute(
                "select v.core,v.value from 'values' v join names n using(nameid) "
                "where n.objectname='rob_timer' and n.metricname='loads-count' and v.prefixid=3"
            )
        )
        self.assertEqual([loads.get(c, 0) for c in range(2)], [0, 0])
        self.assertEqual(
            [x[0] for x in con.execute(q, ("rob_timer", "fst_serial_declared"))], [1, 1]
        )


if __name__ == "__main__":
    unittest.main()
