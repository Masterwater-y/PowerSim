"""Scheduler oracle accounting is a labelled proxy, never exact active truth."""
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))


def row(ordinal,kind,**data):
    return {"schema":"fastsim-thread-state-v1","event_id":f"core0:e{ordinal}",
            "core_id":0,"record_ordinal":ordinal,"pc":4096+4*ordinal,
            "boundary":"before_record" if kind=="initial_owner" else "after_record",
            "phase":"warmup" if ordinal<2 else "measurement","event_kind":kind,**data}


def fixture(trace):
    events=[row(0,"initial_owner",os_tid=101,task_id=1,state="RUNNING"),
            row(1,"identity",os_tid=101,os_tgid=101,task_id=1,runtime_thread_id=0,
                request_id=0,sync_object_id=0,detail=101),
            row(3,"sched_switch",prev_pid=101,next_pid=0,prev_task_id=1,next_task_id=2,
                preempt=False,raw_state=1,state_status="state-at-switch-hook",state_can_race_wakeup=True),
            row(5,"wakeup",target_pid=101,target_task_id=1,source_pid=0,source_task_id=2,
                request_id=None,wait_attempt_event_id=None,cause_status="unknown",
                kernel_state_after_wake=0,target_state="RUNNING_OR_RUNNABLE",self_wakeup=False),
            row(6,"sched_switch",prev_pid=0,next_pid=101,prev_task_id=2,next_task_id=1,
                preempt=False,raw_state=0,state_status="state-at-switch-hook",state_can_race_wakeup=True)]
    meta={"schema":"fastsim-thread-state-metadata-v1","core_id":0,
          "source_file":"core0.records.micro.fst","source_record_count":8,"total_records":8,
          "warmup_records":2,"measurement_records":6,"event_count":len(events),
          "initial_owner":True,"coverage_status":"partial","strict_active_scope":False}
    (trace/"thread-state-metadata-core0.json").write_text(json.dumps(meta))
    (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    oracle=trace/"oracle";oracle.mkdir()
    ticks=[80,90,120,140,150]
    observations=[{"schema":"fastsim-thread-state-observation-v1","oracle_only":True,
                   "event_id":e["event_id"],"event_kind":e["event_kind"],"core_id":0,
                   "record_ordinal":e["record_ordinal"],"commit_tick":tick}
                  for e,tick in zip(events,ticks)]
    (oracle/"thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in observations))
    clock={"schema":"gem5-time-accounting-v1","oracle_only":True,"core_id":0,
           "scope":"user-plus-kernel","active_scope":"workload-task",
           "active_status":"proxy-scheduler-boundary","active_source":"kernel-hook-observations",
           "active_cycles":None,"inactive_cycles":None,"unknown_cycles":None,
           "elapsed_status":"available","elapsed_source":"common-serial-marker-and-first-core-target",
           "common_start_tick":100,"common_end_tick":160,"clock_period_ticks":10,
           "ticks_per_second":1000,"elapsed_reference_cycles":6,
           "elapsed_time_seconds":0.06,"reference_frequency_hz":100}
    (oracle/"time-accounting-core0.json").write_text(json.dumps(clock))
    # Measurement ordinals2/3 form one macro; the scheduler hook at last-uop3
    # still belongs to the old task. Ordinal6 switch-in belongs to idle.
    flags=[1,1,1|1024,1|1024|2048,1,1,1,1]
    record=struct.Struct("<QQQQ4IHHhBB4BI")
    data=struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,0,8,0,0,0,0,0)
    for ordinal,flag in enumerate(flags):
        data+=record.pack(4096+4*ordinal,0,0,0,0,0,0,0,0,flag,0,0,0,255,255,255,255,0)
    (trace/meta["source_file"]).write_bytes(data)
    return events,observations,clock


class ThreadStateAccountingOracleTest(unittest.TestCase):
    def test_observed_cross_core_wake_cannot_precede_proven_switch_out(self):
        from test_thread_state_graph import blocked_fixture, meta
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);_,_,clock=fixture(trace)
            rows=blocked_fixture()
            record=struct.Struct("<QQQQ4IHHhBB4BI")
            for core in (0,1):
                metadata=meta(core);metadata["event_count"]=sum(row["core_id"]==core for row in rows)
                (trace/f"thread-state-metadata-core{core}.json").write_text(json.dumps(metadata))
                (trace/f"thread-state-core{core}.jsonl").write_text("\n".join(json.dumps(row) for row in rows if row["core_id"]==core))
                (trace/f"oracle/time-accounting-core{core}.json").write_text(json.dumps({**clock,"core_id":core}))
                source=struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,core,12,0,0,0,0,0)
                for ordinal in range(12):
                    source+=record.pack(4096+4*ordinal,0,0,0,0,0,0,0,0,1,0,0,0,255,255,255,255,0)
                (trace/metadata["source_file"]).write_bytes(source)
            ticks={"core0:e0":80,"core0:e1":90,"core0:e2":110,"core0:e3":150,
                   "core0:e7":155,"core0:e8":159,"core1:e0":80,"core1:e1":90,
                   "core1:e4":120,"core1:e5":130,"core1:e6":140}
            observations=[{"schema":"fastsim-thread-state-observation-v1","oracle_only":True,
                           "event_id":row["event_id"],"event_kind":row["event_kind"],
                           "core_id":row["core_id"],"record_ordinal":row["record_ordinal"],
                           "commit_tick":ticks[row["event_id"]]} for row in rows]
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(row) for row in observations))
            with self.assertRaisesRegex(ValueError,"causal|dependency"):
                self.audit(trace)
            for observation in observations:
                if observation["event_id"]=="core0:e3":observation["commit_tick"]=125
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(row) for row in observations))
            result=self.audit(trace)
            self.assertEqual(result["active_cycles_proxy"],9)
            self.assertEqual(result["task_macro_instructions"],16)

    def test_certificate_cannot_fill_missing_owner_before_initial_snapshot(self):
        from test_thread_state_graph import certify
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);events,obs,_=fixture(trace)
            path=trace/"thread-state-metadata-core0.json"
            meta=certify(json.loads(path.read_text()));meta.update(warmup_records=0,measurement_records=8)
            path.write_text(json.dumps(meta))
            for event in events:event["phase"]="measurement"
            obs[0]["commit_tick"]=110;obs[1]["commit_tick"]=111
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
            result=self.audit(trace)
        self.assertNotEqual(result["active_status"],"available")
        self.assertIsNone(result["active_cpi"])
        self.assertEqual(result["unknown_cycles"],1)

    def test_certified_owner_tail_produces_active_cpi_without_perf_claim(self):
        from test_thread_state_graph import certify
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace)
            path=trace/"thread-state-metadata-core0.json"
            path.write_text(json.dumps(certify(json.loads(path.read_text()))))
            result=self.audit(trace)
        self.assertEqual(result["active_status"],"available")
        self.assertEqual(result["accounting_contract"],"task-oncpu-switch-entry-v1")
        self.assertEqual(result["perf_equivalence"],"unverified")
        self.assertEqual(result["ownership_status"],"complete")
        self.assertEqual(result["active_cycles"],3)
        self.assertEqual(result["inactive_cycles"],3)
        self.assertEqual(result["unknown_cycles"],0)
        self.assertEqual(result["active_cpi"],1.5)
        self.assertEqual(result["task_macro_instructions"],2)
        self.assertEqual(result["functional_validation"]["unknown_wake_causes"],1)
        self.assertEqual(result["per_core"][0]["active_cycles"],3)

    def audit(self,path):
        self.assertIsNotNone(importlib.util.find_spec("audit_thread_state_accounting"),"owner accounting audit missing")
        from audit_thread_state_accounting import audit
        return audit(path)

    def test_macro_owner_and_active_proxy_conserve_elapsed(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace);result=self.audit(trace)
        self.assertIsNone(result["active_cycles"])
        self.assertEqual(result["active_status"],"proxy-scheduler-boundary")
        self.assertEqual(result["active_cycles_proxy"],3)
        self.assertEqual(result["inactive_cycles_proxy"],3)
        self.assertEqual(result["unknown_cycles_proxy"],0)
        self.assertEqual(result["task_macro_instructions"],2)
        self.assertEqual(result["other_context_macro_instructions"],3)
        self.assertEqual(result["measurement_macro_instructions"],5)
        self.assertEqual(result["active_macro_cpi_proxy"],1.5)
        self.assertEqual(result["elapsed_reference_cycles"],6)
        self.assertEqual(result["elapsed_time_seconds"],0.06)
        self.assertEqual(result["task_ids"],[101])
        self.assertTrue(result["per_core"][0]["owner_time_conserved"])
        self.assertFalse(result["strict_active_scope"])

    def test_wakeup_and_selfwake_do_not_change_cpu_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);events,obs,_=fixture(trace)
            # Self-wake while task running at tick110, before preempt at120.
            wake=events[3];wake.update(record_ordinal=2,pc=4104,self_wakeup=True,source_pid=101,source_task_id=1)
            obs[3].update(record_ordinal=2,commit_tick=110)
            events[2].update(preempt=True)
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
            # The wake anchor becomes a completed macro (a normal instruction).
            source=trace/"core0.records.micro.fst";data=bytearray(source.read_bytes())
            struct.pack_into("<H",data,72+2*64+50,1)
            source.write_bytes(data)
            result=self.audit(trace)
            self.assertEqual(result["active_cycles_proxy"],3)
            self.assertEqual(result["inactive_cycles_proxy"],3)

    def test_missing_duplicate_extra_or_wrong_identity_observation_fail_closed(self):
        changes=[lambda rows:rows.pop(),lambda rows:rows.append(dict(rows[0])),
                 lambda rows:rows.append({**rows[0],"event_id":"core0:e999"}),
                 lambda rows:rows[0].update(core_id=2),
                 lambda rows:rows[0].update(oracle_only=False)]
        for change in changes:
            with tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);_,obs,_=fixture(trace);change(obs)
                (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
                with self.assertRaises(ValueError):self.audit(trace)

    def test_phase_time_bounds_and_source_monotonicity_are_checked(self):
        for index,tick in ((0,101),(1,70),(2,99),(4,170)):
            with tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);_,obs,_=fixture(trace);obs[index]["commit_tick"]=tick
                (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
                with self.assertRaises(ValueError):self.audit(trace)

    def test_event_must_anchor_last_microop_and_record_pc(self):
        for mutation in ("notlast","wrongpc"):
            with tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);fixture(trace)
                source=trace/"core0.records.micro.fst";data=bytearray(source.read_bytes())
                if mutation=="notlast":struct.pack_into("<H",data,72+3*64+50,1|1024)
                else:struct.pack_into("<Q",data,72+3*64,99999)
                source.write_bytes(data)
                with self.assertRaisesRegex(ValueError,"macro|PC|anchor"):self.audit(trace)

    def test_unavailable_elapsed_and_missing_source_are_rejected(self):
        for missing in ("clock","fst"):
            with tempfile.TemporaryDirectory() as directory:
                trace=Path(directory);_,_,clock=fixture(trace)
                if missing=="clock":
                    clock["elapsed_status"]="unavailable"
                    (trace/"oracle/time-accounting-core0.json").write_text(json.dumps(clock))
                else:(trace/"core0.records.micro.fst").unlink()
                with self.assertRaises((ValueError,OSError)):self.audit(trace)

    def test_common_start_before_initial_snapshot_keeps_leading_time_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);events,obs,_=fixture(trace)
            path=trace/"thread-state-metadata-core0.json";meta=json.loads(path.read_text())
            meta.update(warmup_records=0,measurement_records=8);path.write_text(json.dumps(meta))
            for event in events:event["phase"]="measurement"
            obs[0]["commit_tick"]=110;obs[1]["commit_tick"]=111
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
            result=self.audit(trace)
            self.assertEqual(result["active_cycles_proxy"],2)
            self.assertEqual(result["inactive_cycles_proxy"],3)
            self.assertEqual(result["unknown_cycles_proxy"],1)
            self.assertEqual(result["task_macro_instructions"],4)

    def test_warmup_switch_establishes_owner_at_common_measurement_start(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);events,obs,_=fixture(trace)
            path=trace/"thread-state-metadata-core0.json";meta=json.loads(path.read_text())
            meta.update(warmup_records=4,measurement_records=4);path.write_text(json.dumps(meta))
            events[2]["phase"]="warmup";obs[2]["commit_tick"]=95
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(e) for e in events))
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs))
            result=self.audit(trace)
            self.assertEqual(result["active_cycles_proxy"],1)
            self.assertEqual(result["inactive_cycles_proxy"],5)
            self.assertEqual(result["task_macro_instructions"],1)

    def test_multicore_elapsed_is_not_summed_with_cpu_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);events,obs,clock=fixture(trace)
            meta=json.loads((trace/"thread-state-metadata-core0.json").read_text())
            meta.update(core_id=1,source_file="core1.records.micro.fst")
            (trace/"thread-state-metadata-core1.json").write_text(json.dumps(meta))
            translated=[]
            for event in events:
                event={**event,"core_id":1,"event_id":event["event_id"].replace("core0:","core1:")}
                for field in ("os_tid","os_tgid","prev_pid","next_pid","target_pid"):
                    if event.get(field)==101:event[field]=202
                for field in ("task_id","prev_task_id","next_task_id","target_task_id","source_task_id"):
                    if field in event:event[field]+=2
                if "runtime_thread_id" in event:event["runtime_thread_id"]=1
                translated.append(event)
            (trace/"thread-state-core1.jsonl").write_text("\n".join(json.dumps(e) for e in translated))
            translated_obs=[{**o,"core_id":1,"event_id":o["event_id"].replace("core0:","core1:")} for o in obs]
            (trace/"oracle/thread-state-observation.jsonl").write_text("\n".join(json.dumps(o) for o in obs+translated_obs))
            (trace/"oracle/time-accounting-core1.json").write_text(json.dumps({**clock,"core_id":1}))
            data=bytearray((trace/"core0.records.micro.fst").read_bytes());struct.pack_into("<I",data,20,1)
            (trace/meta["source_file"]).write_bytes(data)
            result=self.audit(trace)
            self.assertEqual(result["active_cycles_proxy"],6)
            self.assertEqual(result["inactive_cycles_proxy"],6)
            self.assertEqual(result["elapsed_reference_cycles"],6)
            self.assertEqual(result["task_macro_instructions"],4)

    def test_extra_functional_sidecar_cannot_be_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace)
            (trace/"thread-state-core999.jsonl").write_text("{}\n")
            with self.assertRaisesRegex(ValueError,"sidecar|metadata|participant"):self.audit(trace)

    def test_cli_writes_proxy_oracle_to_default_path(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);fixture(trace)
            process=subprocess.run([sys.executable,str(ROOT/"tools/audit_thread_state_accounting.py"),
                "--trace-dir",str(trace)],capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads((trace/"oracle/thread-state-accounting.json").read_text())
            self.assertEqual(result["active_status"],"proxy-scheduler-boundary")
            self.assertIsNone(result["active_cycles"])


if __name__=="__main__":unittest.main()
