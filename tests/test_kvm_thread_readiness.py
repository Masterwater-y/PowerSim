"""Exercise the producer's real kick/wakeup bodies without building gem5.

The harness supplies only the event-queue/thread context interfaces; the signal
decision and wakeup control flow are read directly from the maintained source.
"""
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ROOT / "gem5-fs/src/cpu/kvm/base.hh"
SOURCE = ROOT / "gem5-fs/src/cpu/kvm/base.cc"


def body(text, signature):
    start = text.index("{", text.index(signature))
    depth = 1
    end = start + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


class KvmThreadReadinessTest(unittest.TestCase):
    def test_preinit_kick_preserves_wakeup_and_ready_kick_signals(self):
        kick = body(HEADER.read_text(), "void kick() const")
        wakeup = body(SOURCE.read_text(), "BaseKvmCPU::wakeup(ThreadID tid)")
        program = r'''
#include <atomic>
#include <cassert>
#include <pthread.h>
#include <csignal>
#define KVM_KICK_SIGNAL SIGRTMIN
#define DPRINTF(...) ((void)0)
static int signals = 0;
static int fakeKill(pthread_t, int) { ++signals; return 0; }
#define pthread_kill fakeKill
using ThreadID = short;
struct ThreadContext { enum Status { Suspended, Active }; };
struct GuestThread {
    ThreadContext::Status state = ThreadContext::Suspended;
    int activations = 0;
    auto status() const { return state; }
    void activate() { state = ThreadContext::Active; ++activations; }
};
struct EventQueue {
    struct ScopedMigration { explicit ScopedMigration(void *) {} };
};
struct BaseKvmCPU {
    std::atomic<bool> vcpuThreadReady{false};
    pthread_t vcpuThread{};
    GuestThread guest;
    GuestThread *thread = &guest;
    void *eventQueue() { return nullptr; }
    void kick() const KICK_BODY
    void wakeup(ThreadID tid) WAKEUP_BODY
};
int main() {
    BaseKvmCPU cpu;
    cpu.wakeup(0);
    assert(signals == 0);
    assert(cpu.guest.state == ThreadContext::Active);
    assert(cpu.guest.activations == 1);
    cpu.vcpuThread = pthread_self();
    cpu.vcpuThreadReady.store(true, std::memory_order_release);
    cpu.wakeup(0);
    assert(signals == 1);
    assert(cpu.guest.activations == 1);
    cpu.vcpuThreadReady.store(false, std::memory_order_release);
    cpu.guest.state = ThreadContext::Suspended;
    cpu.wakeup(0);
    assert(signals == 1);
    assert(cpu.guest.activations == 2);
}
'''.replace("KICK_BODY", kick).replace("WAKEUP_BODY", wakeup)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            source = pathlib.Path(directory) / "kvm_readiness.cc"
            executable = pathlib.Path(directory) / "kvm_readiness"
            source.write_text(program)
            subprocess.run(["g++", "-std=c++17", "-pthread", str(source),
                            "-o", str(executable)], check=True,
                           capture_output=True, text=True)
            completed = subprocess.run([str(executable)], capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_readiness_publication_and_lifecycle_invalidation(self):
        header, source = HEADER.read_text(), SOURCE.read_text()
        self.assertIn("std::atomic<bool> vcpuThreadReady{false}", header)
        restart = body(source, "BaseKvmCPU::restartEqThread()")
        publication = "vcpuThreadReady.store(true, std::memory_order_release)"
        self.assertGreater(restart.index(publication), restart.rindex("runTimer.reset"))
        for signature in ("BaseKvmCPU::startup()", "BaseKvmCPU::drainResume()",
                          "BaseKvmCPU::notifyFork()", "BaseKvmCPU::switchOut()"):
            self.assertIn("vcpuThreadReady.store(false, std::memory_order_release)",
                          body(source, signature), signature)


if __name__ == "__main__":
    unittest.main()
