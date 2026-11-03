"""Portable scheduler facts must not fabricate sleep or wake causality."""
import importlib.util
import json
import subprocess
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def meta(core, total=12, warmup=2):
    return dict(schema="fastsim-thread-state-metadata-v1", core_id=core,
                source_file=f"core{core}.fst", source_record_count=total,
                total_records=total, warmup_records=warmup,
                measurement_records=total-warmup, initial_owner=True,
                coverage_status="partial", scope_status="context-boundary-proxy",
                strict_active_scope=False)


def certify(metadata):
    return {**metadata, "accounting_contract":"task-oncpu-switch-entry-v1",
            "kernel_stream_complete":True,"common_cut_complete":True,
            "owner_continuity_to_common_cut":True,
            "common_cut_id":"first-core-target-common-end-v1",
            "read_failures":0,"identity_failures":0,"unretained_hook_events":0}


def event(core, ordinal, kind, *, event_number=None, **fields):
    return dict(schema="fastsim-thread-state-v1",
                event_id=f"core{core}:e{ordinal if event_number is None else event_number}",
                core_id=core, record_ordinal=ordinal, pc=4096+4*ordinal,
                boundary="before_record" if kind == "initial_owner" else "after_record",
                phase="warmup" if ordinal < 2 else "measurement", event_kind=kind,
                **fields)


def initial(core, pid):
    return event(core, 0, "initial_owner", os_tid=pid, task_id=pid+10000, state="RUNNING")


def identity(core, ordinal, tid, runtime=1):
    return event(core, ordinal, "identity", os_tid=tid, os_tgid=tid,
                 task_id=tid+10000, runtime_thread_id=runtime, request_id=0,
                 sync_object_id=0, detail=tid)


def marker(core, ordinal, kind, tid=101, req=7, **fields):
    return event(core, ordinal, kind, os_tid=tid, os_tgid=tid,
                 task_id=tid+10000, runtime_thread_id=1 if tid==101 else 2,
                 request_id=req, sync_object_id=77,
                 detail=fields.pop("detail", 128 << 32), **fields)


def switch(core, ordinal, prev, nxt, **fields):
    return event(core, ordinal, "sched_switch", prev_pid=prev, next_pid=nxt,
                 prev_task_id=prev+10000, next_task_id=nxt+10000,
                 preempt=fields.pop("preempt", False),
                 raw_state=fields.pop("raw_state", 1),
                 state_status="state-at-switch-hook",
                 state_can_race_wakeup=fields.pop("state_can_race_wakeup", True), **fields)


def blocked_fixture():
    return [initial(0,101), identity(0,1,101), marker(0,2,"wait_attempt"),
            switch(0,3,101,0,state_can_race_wakeup=False),
            switch(0,7,0,101,raw_state=0),
            marker(0,8,"wait_return",detail=0,attempt_event_id="core0:e2"),
            initial(1,202),identity(1,1,202,runtime=2),
            marker(1,4,"wake_attempt",tid=202,req=9,detail=129 << 32),
            event(1,5,"wakeup",target_pid=101,target_task_id=10101,
                  source_pid=202,source_task_id=10202,request_id=7,
                  wait_attempt_event_id="core0:e2",sync_object_id=77,
                  wake_request_id=9,wake_attempt_event_id="core1:e4",
                  cause_status="kernel-futex-call-path"),
            marker(1,6,"wake_return",tid=202,req=9,detail=1,
                   attempt_event_id="core1:e4")]


class ThreadStateGraphTest(unittest.TestCase):
    def test_certified_ownership_is_independent_of_unknown_wake_causes(self):
        rows=blocked_fixture()
        rows[9]["cause_status"]="unknown"
        rows[3]["state_status"]="verified-dequeue-sleep"
        result=self.graph(rows,[certify(meta(0)),certify(meta(1))])
        self.assertEqual(result["summary"]["active_status"],"available")
        self.assertEqual(result["summary"]["perf_equivalence"],"unverified")
        self.assertEqual(result["summary"]["unknown_wake_causes"],1)
        self.assertEqual(result["summary"]["proven_block_wake_admissions"],1)
        self.assertTrue(all(core["owner_continuity_to_common_cut"] for core in result["cores"]))

    def test_partial_forged_or_cross_core_certification_fails_closed(self):
        rows=[initial(0,101),identity(0,1,101),initial(1,202),identity(1,1,202,2)]
        mutations=[lambda m:m.pop("common_cut_complete"),
                   lambda m:m.update(kernel_stream_complete=False),
                   lambda m:m.update(owner_continuity_to_common_cut=1),
                   lambda m:m.update(accounting_contract="perf-unhalted-v1"),
                   lambda m:m.update(common_cut_id="another-cut"),
                   lambda m:m.pop("read_failures")]
        for change in mutations:
            metas=[certify(meta(0)),certify(meta(1))];change(metas[1])
            with self.subTest(metadata=metas[1]),self.assertRaises(ValueError):
                self.graph(rows,metas)
        mixed=self.graph(rows,[certify(meta(0)),meta(1)])
        self.assertNotEqual(mixed["summary"]["active_status"],"available")

    def test_certified_compiler_requires_headers_and_exports_closure_without_time(self):
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);rows=[initial(0,101),identity(0,1,101)]
            m=certify(meta(0));m["event_count"]=2
            (trace/"thread-state-metadata-core0.json").write_text(json.dumps(m))
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            with self.assertRaisesRegex(ValueError,"source|header"):
                compile_plan(trace,trace/"missing.plan")
            (trace/m["source_file"]).write_bytes(struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,0,12,0,0,0,0,0)+bytes(64*12))
            compile_plan(trace,trace/"state.plan")
            plan=(trace/"state.plan").read_text()
            self.assertIn("TASK 101 101 1\nACCOUNTING task-oncpu-switch-entry-v1\nCLOSURE 0 1\nEVENT",plan)
            self.assertNotIn("tick",plan)

    def graph(self, rows, metas=None):
        spec = importlib.util.find_spec("thread_state_graph")
        self.assertIsNotNone(spec, "thread-state functional graph is missing")
        from thread_state_graph import build_graph
        metas = metas or [meta(core) for core in sorted({r["core_id"] for r in rows})]
        for m in metas:
            m["event_count"] = sum(r["core_id"]==m["core_id"] for r in rows)
        return build_graph(rows, metas)

    def test_actual_wait_wake_and_run_are_distinct_causal_events(self):
        result = self.graph(blocked_fixture())
        index = {e["event_id"]:i for i,e in enumerate(result["events"])}
        wake, run = index["core1:e5"],index["core0:e7"]
        self.assertIn(index["core0:e2"],result["dependencies"][wake])
        self.assertIn(index["core1:e4"],result["dependencies"][wake])
        self.assertIn(wake,result["dependencies"][run])
        self.assertEqual(result["events"][index["core0:e3"]]["args"][2],2)
        self.assertEqual(result["events"][wake]["kind"],7)
        self.assertNotEqual(result["summary"]["active_status"],"available")

    def test_eagain_attempt_never_implies_blocked(self):
        rows=[initial(0,101),identity(0,1,101),marker(0,2,"wait_attempt"),
              marker(0,3,"wait_return",detail=-11,attempt_event_id="core0:e2")]
        result=self.graph(rows)
        self.assertEqual(result["summary"]["blocked_switches"],0)
        self.assertEqual(result["summary"]["wait_returns"]["eagain"],1)

    def test_preempt_is_runnable_and_raw_state_read_race_is_unknown(self):
        rows=[initial(0,101),switch(0,2,101,0,preempt=True),
              switch(0,3,0,101,raw_state=0),switch(0,4,101,0)]
        result=self.graph(rows)
        self.assertEqual([e["args"][2] for e in result["events"]],[1,1,3])
        self.assertEqual(result["summary"]["unknown_switches"],1)

    def test_unknown_wake_cause_does_not_guess_cross_core_edge(self):
        rows=blocked_fixture()
        wake=next(r for r in rows if r["event_kind"]=="wakeup")
        wake.update(cause_status="unknown",request_id=None,wait_attempt_event_id=None,
                    wake_attempt_event_id=None,wake_request_id=None)
        result=self.graph(rows)
        index={e["event_id"]:i for i,e in enumerate(result["events"])}
        self.assertNotIn(index["core0:e2"],result["dependencies"][index["core1:e5"]])
        self.assertNotIn(index["core1:e5"],result["dependencies"][index["core0:e7"]])
        self.assertEqual(result["summary"]["unknown_wake_causes"],1)

    def test_other_cpu_context_keeps_source_stream_while_waiter_is_out(self):
        rows=blocked_fixture()
        rows.insert(4,switch(0,5,0,303,raw_state=0))
        rows.insert(5,switch(0,6,303,0,preempt=True))
        result=self.graph(rows)
        self.assertEqual(result["cores"][0]["initial_owner"],101)
        self.assertIn(303,{e["args"][1] for e in result["events"] if e["kind"]==6})

    def test_missing_return_at_common_end_stays_pending(self):
        result=self.graph([initial(0,101),identity(0,1,101),marker(0,2,"wait_attempt")])
        self.assertEqual(result["summary"]["pending_wait_requests"],1)
        self.assertNotEqual(result["summary"]["active_status"],"available")

    def test_return_without_attempt_or_forged_wake_pair_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"attempt"):
            self.graph([initial(0,101),identity(0,1,101),marker(0,2,"wait_return",detail=0)])
        rows=blocked_fixture()
        next(r for r in rows if r["event_kind"]=="wakeup")["request_id"]=999
        with self.assertRaisesRegex(ValueError,"request|wait"):
            self.graph(rows)

    def test_initial_owner_tid_reuse_phase_bounds_and_oracle_pollution_rejected(self):
        variants=[]
        rows=blocked_fixture();variants.append([r for r in rows if not(r["core_id"]==0 and r["event_kind"]=="initial_owner")])
        rows=blocked_fixture();rows[1]["task_id"]=888;variants.append(rows)
        rows=blocked_fixture();rows[2]["phase"]="warmup";variants.append(rows)
        rows=blocked_fixture();rows[2]["record_ordinal"]=100;variants.append(rows)
        rows=blocked_fixture();rows[2]["oracle"]={"tick":10};variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows),self.assertRaises(ValueError):self.graph(rows)

    def test_input_log_order_is_not_global_time(self):
        first=self.graph(blocked_fixture())
        second=self.graph(list(reversed(blocked_fixture())))
        self.assertEqual(first,second)

    def test_candidate_runtime_bracket_never_becomes_a_wake_cause(self):
        rows=blocked_fixture()
        wake=next(r for r in rows if r["event_kind"]=="wakeup")
        wake.update(cause_status="unknown",wake_attempt_event_id=None,wake_request_id=None,
                    candidate_wake_attempt_event_id="core1:e4",candidate_wake_request_id=9)
        result=self.graph(rows)
        # The independent exact waiter target remains known; source causality does not.
        self.assertEqual(result["summary"]["known_waiter_wakes"],1)
        self.assertEqual(result["summary"]["known_causal_wakes"],0)
        self.assertEqual(next(e for e in result["events"] if e["event_kind"]=="wakeup")["args"],[101,7,0])

    def test_racing_switch_does_not_assume_switchout_precedes_wakeup(self):
        rows=blocked_fixture()
        next(r for r in rows if r["event_kind"]=="sched_switch")["state_can_race_wakeup"]=True
        result=self.graph(rows)
        index={e["event_id"]:i for i,e in enumerate(result["events"])}
        self.assertNotIn(index["core0:e3"],result["dependencies"][index["core1:e5"]])
        self.assertNotIn(index["core1:e5"],result["dependencies"][index["core0:e7"]])
        self.assertNotIn(index["core0:e2"],result["dependencies"][index["core1:e5"]])

    def test_eagain_with_preemption_does_not_need_wakeup_to_run_again(self):
        rows=blocked_fixture()
        next(r for r in rows if r["event_kind"]=="sched_switch").update(preempt=True)
        next(r for r in rows if r["event_kind"]=="wait_return")["detail"]=-11
        result=self.graph(rows)
        index={e["event_id"]:i for i,e in enumerate(result["events"])}
        self.assertNotIn(index["core1:e5"],result["dependencies"][index["core0:e7"]])
        self.assertNotIn(index["core0:e2"],result["dependencies"][index["core1:e5"]])

    def test_running_wake_after_switchin_cannot_make_prior_switchin_depend_on_it(self):
        for preempt in (True,False):
            rows=[r for r in blocked_fixture() if r["core_id"]==0]
            rows[3].update(preempt=preempt,state_can_race_wakeup=True)
            rows[-1].update(event_id="core0:e10",record_ordinal=10,pc=4136)
            rows.append(event(0,8,"wakeup",target_pid=101,target_task_id=10101,
                              source_pid=101,source_task_id=10101,request_id=7,
                              wait_attempt_event_id="core0:e2",sync_object_id=77,
                              wake_request_id=None,wake_attempt_event_id=None,
                              cause_status="unknown",self_wakeup=True,
                              target_state="RUNNING_OR_RUNNABLE",kernel_state_after_wake=0))
            with self.subTest(preempt=preempt):
                result=self.graph(rows)
                index={e["event_id"]:i for i,e in enumerate(result["events"])}
                wake,run=index["core0:e8"],index["core0:e7"]
                self.assertNotIn(wake,result["dependencies"][run])
                self.assertIn(run,result["dependencies"][wake])
                self.assertNotIn(index["core0:e2"],result["dependencies"][wake])
                self.assertEqual(result["summary"]["known_waiter_wakes"],1)

    def test_proven_block_is_incompatible_with_eagain_and_timeout_is_diagnostic(self):
        rows=blocked_fixture();rows[5]["detail"]=-11
        with self.assertRaisesRegex(ValueError,"EAGAIN"):
            self.graph(rows)
        rows=blocked_fixture();rows[5]["detail"]=-110
        result=self.graph(rows)
        self.assertEqual(result["summary"]["wait_returns"]["unsupported"],1)
        self.assertNotEqual(result["summary"]["active_status"],"available")

    def test_causal_cycle_and_reverse_phase_dependency_fail_closed(self):
        rows=blocked_fixture()
        marker_row=next(r for r in rows if r["event_kind"]=="wake_attempt")
        marker_row.update(record_ordinal=9,pc=4132)
        with self.assertRaisesRegex(ValueError,"cycle|precedes"):
            self.graph(rows)
        rows=blocked_fixture()
        rows[2]["record_ordinal"]=1
        # use identity at ordinal0 after_record; initial uses before_record.
        rows[1].update(record_ordinal=0,pc=4096)
        rows[2].update(phase="warmup")
        wake=next(r for r in rows if r["event_kind"]=="wakeup")
        wake.update(record_ordinal=0,phase="warmup",pc=4096)
        rows=[r for r in rows if not(r["core_id"]==1 and r["event_kind"]=="identity")]
        with self.assertRaisesRegex(ValueError,"identity|phase"):
            self.graph(rows)

    def test_nonowning_runtime_identity_and_metadata_count_mismatch_fail(self):
        rows=blocked_fixture();rows[1]["os_tid"]=555
        with self.assertRaisesRegex(ValueError,"owner"):
            self.graph(rows)
        rows=blocked_fixture();metas=[meta(0),meta(1)];metas[0]["measurement_records"]=100
        with self.assertRaisesRegex(ValueError,"conservation"):
            self.graph(rows,metas)

    def test_owner_errors_identify_source_event_and_expected_actual_pid(self):
        cases=[([initial(0,0),identity(0,1,101)],"core0:e1","record_ordinal=1"),
               ([initial(0,0),switch(0,2,101,0)],"core0:e2","record_ordinal=2")]
        for rows,event_id,ordinal in cases:
            with self.subTest(event_id=event_id),self.assertRaises(ValueError) as raised:
                self.graph(rows)
            message=str(raised.exception)
            for detail in (event_id,"core=0",ordinal,"expected_pid=0","actual_pid=101","owner_event=core0:e0"):
                self.assertIn(detail,message)

    def test_collection_failures_reject_even_an_apparently_consistent_owner_plan(self):
        from compile_thread_state import compile_plan
        for field in ("unretained_hook_events","read_failures","identity_failures"):
            for failure_count in (1,-1,True,1.5,None):
                with self.subTest(field=field,failure_count=failure_count),tempfile.TemporaryDirectory() as directory:
                    trace=Path(directory);rows=[initial(0,101),identity(0,1,101)]
                    m=meta(0);m["event_count"]=2
                    metadata_path=trace/"thread-state-metadata-core0.json"
                    metadata_path.write_text(json.dumps(m))
                    (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
                    # Missing counters mean zero for portable sources. The same
                    # actor sequence compiles before the failure is declared.
                    compile_plan(trace,trace/"valid.plan")
                    m[field]=failure_count;metadata_path.write_text(json.dumps(m))
                    with self.assertRaisesRegex(ValueError,field):
                        compile_plan(trace,trace/"bad.plan")
                    self.assertFalse((trace/"bad.plan").exists())

    def test_migration_of_runtime_and_unmapped_tasks_is_rejected_without_order(self):
        variants=[
            [initial(0,101),initial(1,101)],
            [initial(0,101),switch(0,2,101,303,raw_state=0),
             switch(0,3,303,101,raw_state=0),initial(1,202),
             switch(1,4,202,303,raw_state=0)],
        ]
        for rows in variants:
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,"migration|physical core"):
                self.graph(rows)

    def test_distinct_per_core_idle_contexts_are_not_tid_reuse(self):
        rows=[initial(0,0),initial(1,0)]
        rows[1]["task_id"]=20000
        self.assertEqual(self.graph(rows)["summary"]["cores"],2)

    def test_compiler_plan_format_and_oracle_independence(self):
        spec=importlib.util.find_spec("compile_thread_state")
        self.assertIsNotNone(spec,"thread-state plan compiler is missing")
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory)
            rows=blocked_fixture()
            for core in (0,1):
                m=meta(core);m["event_count"]=sum(r["core_id"]==core for r in rows)
                (trace/f"thread-state-metadata-core{core}.json").write_text(json.dumps(m))
                (trace/f"thread-state-core{core}.jsonl").write_text("\n".join(json.dumps(r) for r in rows if r["core_id"]==core))
            output=trace/"state.plan"
            compile_plan(trace,output)
            before=output.read_text()
            self.assertTrue(before.startswith("FASTSIM_THREAD_STATE_PLAN_V1\nCORES 2 EVENTS 9 TASKS 2\n"))
            self.assertIn("CORE 0 12 2 10 101\n",before)
            self.assertIn("TASK 101 101 1\n",before)
            # identity at ordinal1 is warmup (phase0), wait ordinal2 measurement(1).
            self.assertIn("EVENT 1 0 2 4104 2 1 101 7 549755813888 1 0\n",before)
            (trace/"oracle").mkdir();(trace/"oracle/thread-state.jsonl").write_text('{"tick":999999}')
            compile_plan(trace,output)
            self.assertEqual(output.read_text(),before)

    def test_compiler_accepts_actual_fst_basename_and_checks_present_header(self):
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);rows=[initial(0,101),identity(0,1,101)]
            m=meta(0);m.update(event_count=2,source_file="core0.records.micro.fst")
            (trace/"thread-state-metadata-core0.json").write_text(json.dumps(m))
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            source=trace/m["source_file"]
            source.write_bytes(struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,0,12,0,0,0,0,0)+bytes(64*12))
            summary=compile_plan(trace,trace/"state.plan")
            self.assertEqual(summary["source_header_validated_cores"],1)
            source.write_bytes(struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,0,99,0,0,0,0,0))
            with self.assertRaisesRegex(ValueError,"header|population"):
                compile_plan(trace,trace/"bad.plan")

    def test_cli_require_sources_fails_before_writing_a_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);rows=[initial(0,101),identity(0,1,101)]
            m=meta(0);m["event_count"]=2
            (trace/"thread-state-metadata-core0.json").write_text(json.dumps(m))
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            output=trace/"missing.plan"
            process=subprocess.run([sys.executable,str(ROOT/"tools/compile_thread_state.py"),
                "--trace-dir",str(trace),"--output",str(output),"--require-sources"],
                capture_output=True,text=True)
            self.assertEqual(process.returncode,1,process.stderr)
            self.assertIn("source",process.stderr)
            self.assertFalse(output.exists())

    def test_compiler_accepts_board_producer_basename_using_header_identity(self):
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);rows=[initial(0,101),identity(0,1,101)]
            m=meta(0);m.update(event_count=2,
                source_file="board.processor.switch0.core.tao_trace.tao_trace.records.micro.fst")
            (trace/"thread-state-metadata-core0.json").write_text(json.dumps(m))
            (trace/"thread-state-core0.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            source=trace/m["source_file"]
            source.write_bytes(struct.pack("<8sIIIIQQ4Q",b"FSTRC01\0",7,72,64,0,12,0,0,0,0,0)+bytes(64*12))
            summary=compile_plan(trace,trace/"state.plan",require_sources=True)
            self.assertEqual(summary["source_header_validated_cores"],1)
            # Filename spelling cannot override an incompatible physical core.
            data=bytearray(source.read_bytes());struct.pack_into("<I",data,20,1);source.write_bytes(data)
            with self.assertRaisesRegex(ValueError,"header|population"):
                compile_plan(trace,trace/"bad.plan",require_sources=True)

    def test_source_basename_cannot_escape_trace_directory(self):
        rows=[initial(0,101),identity(0,1,101)]
        for name in ("../core0.fst","directory/core0.fst",r"directory\core0.fst",
                     "..fst","a..b.fst","bad\nname.fst"):
            metas=[meta(0)];metas[0]["source_file"]=name
            with self.subTest(name=name),self.assertRaises(ValueError):self.graph(rows,metas)

    def test_compiler_rejects_empty_runtime_plan(self):
        from compile_thread_state import compile_plan
        with tempfile.TemporaryDirectory() as directory:
            trace=Path(directory);m=meta(0);m["event_count"]=1
            (trace/"thread-state-metadata-core0.json").write_text(json.dumps(m))
            (trace/"thread-state-core0.jsonl").write_text(json.dumps(initial(0,101)))
            with self.assertRaisesRegex(ValueError,"runtime|events|tasks"):
                compile_plan(trace,trace/"empty.plan")
            self.assertFalse((trace/"empty.plan").exists())


if __name__=="__main__":unittest.main()
