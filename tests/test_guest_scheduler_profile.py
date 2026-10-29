"""Protect verified kernel layouts and fail-closed collector enablement."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/generate_guest_scheduler_profile.py"

# A real small ELF gives independently known member layouts, function symbols,
# a build ID and DWARF. This is a layout fixture, never a guest kernel profile.
SOURCE = r"""
struct mm_struct { void *pgd; };
struct task_struct {
    long state;
    int pid, tgid;
    struct mm_struct *mm;
    unsigned long long start_boottime;
};
struct task_struct task;
struct mm_struct mm;
struct task_struct *current_task = &task;
#define HOOK(name) void name(void) { asm volatile ("" ::: "memory"); }
HOOK(__switch_to_asm) HOOK(__switch_to) HOOK(__schedule)
HOOK(finish_task_switch) HOOK(try_to_wake_up)
HOOK(futex_wait_queue_me) HOOK(futex_wake) HOOK(trace_sched_wakeup)
unsigned long __tracepoint_sched_switch;
unsigned long __tracepoint_sched_wakeup;
unsigned long __tracepoint_sched_wakeup_new;
int main(void) { return task.pid + (mm.pgd != 0); }
"""


class ProfileTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("cc") or not shutil.which("strip"):
            self.skipTest("ELF layout test requires cc and strip")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory / "kernel.c"
        self.source.write_text(SOURCE)
        self.kernel = self.directory / "vmlinux"
        subprocess.run(["cc", "-g", "-gdwarf-4", "-O0", "-no-pie",
                        "-Wl,--build-id", str(self.source), "-o", str(self.kernel)],
                       check=True, capture_output=True)

    def run_profile(self, *extra):
        output = self.directory / "profile.json"
        result = subprocess.run(["python3", str(TOOL), "--kernel", str(self.kernel),
                                 "--output", str(output), *extra],
                                capture_output=True, text=True)
        self.assertTrue(output.exists(), result.stderr)
        return result, json.loads(output.read_text())

    def test_debug_members_are_verified_but_do_not_certify_a_collector(self):
        # An offset typo or treating metadata as a running collector breaks this.
        result, profile = self.run_profile("--inventory-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile["metadata_status"], "verified")
        self.assertFalse(profile["can_emit_state_facts"])
        self.assertEqual(profile["collector_status"], "unimplemented")
        fields = profile["types"]["task_struct"]["members"]
        self.assertEqual({k: v["offset_bytes"] for k, v in fields.items()},
                         {"state": 0, "pid": 8, "tgid": 12, "mm": 16, "start_boottime": 24})
        self.assertEqual(fields["pid"]["size_bytes"], 4)
        self.assertEqual(fields["mm"]["size_bytes"], 8)
        self.assertEqual(profile["kernel"]["sha256"], hashlib.sha256(self.kernel.read_bytes()).hexdigest())
        self.assertEqual(profile["hooks"]["__switch_to_asm"]["classification"], "function_symbol_candidate")
        self.assertEqual(profile["hooks"]["__tracepoint_sched_switch"]["classification"], "data_tracepoint_not_executable")
        self.assertEqual(profile["hooks"]["current_task"]["classification"], "per_cpu_data_candidate")
        self.assertTrue(profile["kernel"]["build_id"])

    def test_symbol_only_kernel_cannot_guess_task_offsets(self):
        # Removing the missing-type guard must make this test fail.
        subprocess.run(["strip", "--strip-debug", str(self.kernel)], check=True)
        result, profile = self.run_profile()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(profile["metadata_status"], "unavailable")
        self.assertEqual(profile["types"], {})
        self.assertFalse(profile["can_emit_state_facts"])
        self.assertIn("missing_dwarf_type_information", profile["rejections"])
        self.assertIn("pid", " ".join(profile["rejections"]))

    def test_debug_image_build_identity_mismatch_is_rejected(self):
        # Accepting an unrelated debug image would supply plausible wrong offsets.
        unrelated = self.directory / "unrelated"
        self.source.write_text(SOURCE + "\nint unrelated_build;\n")
        subprocess.run(["cc", "-g", "-gdwarf-4", "-O0", "-no-pie",
                        "-Wl,--build-id", str(self.source), "-o", str(unrelated)],
                       check=True, capture_output=True)
        subprocess.run(["strip", "--strip-debug", str(self.kernel)], check=True)
        result, profile = self.run_profile("--debug-image", str(unrelated), "--inventory-only")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(profile["types"], {})
        self.assertIn("debug_image_build_id_mismatch", profile["rejections"])

    def test_matching_separate_debug_image_provides_verified_offsets(self):
        debug = self.directory / "vmlinux.debug"
        subprocess.run(["objcopy", "--only-keep-debug", str(self.kernel), str(debug)], check=True)
        subprocess.run(["strip", "--strip-debug", str(self.kernel)], check=True)
        result, profile = self.run_profile("--debug-image", str(debug), "--inventory-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile["metadata_status"], "verified")
        self.assertEqual(profile["types"]["task_struct"]["members"]["pid"]["offset_bytes"], 8)

    def test_non_elf_input_has_a_clear_error_and_cannot_create_a_profile(self):
        self.kernel.write_text("not an ELF kernel")
        output = self.directory / "profile.json"
        result = subprocess.run(["python3", str(TOOL), "--kernel", str(self.kernel),
                                 "--output", str(output), "--inventory-only"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("scheduler profile rejected", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(output.exists())

    def test_missing_member_never_becomes_a_default_zero_offset(self):
        self.source.write_text(SOURCE.replace("unsigned long long start_boottime;", "unsigned long long wrong_member;"))
        subprocess.run(["cc", "-g", "-gdwarf-4", "-O0", "-no-pie", "-Wl,--build-id",
                        str(self.source), "-o", str(self.kernel)], check=True, capture_output=True)
        result, profile = self.run_profile("--inventory-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile["metadata_status"], "unavailable")
        self.assertIn("missing_member:task_struct.start_boottime", profile["rejections"])
        self.assertNotIn("start_boottime", profile["types"]["task_struct"]["members"])

    def test_even_verified_metadata_is_fail_closed_by_default(self):
        result, profile = self.run_profile()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(profile["metadata_status"], "verified")
        self.assertFalse(profile["can_emit_state_facts"])
        self.assertIn("committed_scheduler_collector_unimplemented", profile["rejections"])

    def test_supplied_kernel_machine_audit_is_partial_and_byte_verified(self):
        kernel = Path("/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0")
        if not kernel.exists():
            self.skipTest("optional exact guest-kernel audit requires cached gem5 kernel")
        self.kernel = kernel
        result, profile = self.run_profile("--inventory-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        audit = profile["machine_code_evidence"]
        self.assertEqual(audit["status"], "partial_exact_build_audit")
        self.assertEqual(audit["current_task_gs_displacement"], 0x15d00)
        self.assertEqual(audit["members"]["task_struct.pid"]["offset_bytes"], 0x758)
        self.assertTrue(all(proof["bytes_verified"] for proof in audit["proofs"]))
        self.assertIn("state_at_switch_hook_can_race_wakeup", audit["limitations"])
        self.assertFalse(profile["can_emit_state_facts"])

    def test_exact_kernel_proves_sleep_decision_and_futex_queue_target(self):
        kernel = Path("/root/.cache/gem5/x86-linux-kernel-5.4.49-1.0.0")
        if not kernel.exists():
            self.skipTest("cached exact kernel absent")
        self.kernel = kernel
        result, profile = self.run_profile("--inventory-only")
        audit = profile["machine_code_evidence"]
        self.assertIn("dequeue_sleep", audit["boundaries"])
        self.assertEqual(audit["boundaries"]["dequeue_sleep"]["pc"], "0xffffffff81517851")
        self.assertEqual(audit["boundaries"]["futex_enqueue"]["pc"], "0xffffffff810a021a")
        self.assertEqual(audit["boundaries"]["futex_selected"]["pc"], "0xffffffff8109f97b")
        proofs = {p["name"]: p for p in audit["proofs"]}
        self.assertTrue(proofs["dequeue_sleep_call_and_return"]["bytes_verified"])
        self.assertTrue(proofs["futex_plist_enqueue_and_task"]["bytes_verified"])
        self.assertTrue(proofs["futex_selected_unqueue"]["bytes_verified"])


if __name__ == "__main__":
    unittest.main()
