/* External independent-process or existing-native-worker collection driver. */
#define _GNU_SOURCE
#include "gem5_roi_marker.h"
#include <sched.h>
#include <signal.h>
#include <poll.h>
#include <sys/wait.h>
#include <time.h>
#include <dirent.h>
#include <sys/stat.h>
#include <termios.h>

static void private_copy_directory(int index) {
    char directory[64];
    snprintf(directory, sizeof(directory), "copy-%d", index);
    if (mkdir(directory, 0700)) { perror("rate private directory"); exit(EXIT_FAILURE); }
    DIR *input = opendir(".");
    if (!input) { perror("rate input directory"); exit(EXIT_FAILURE); }
    struct dirent *entry;
    while ((entry = readdir(input))) {
        if (entry->d_name[0] == '.' || !strncmp(entry->d_name, "copy-", 5)) continue;
        char *target = NULL, *link = NULL;
        if (asprintf(&target, "../%s", entry->d_name) < 0 ||
            asprintf(&link, "%s/%s", directory, entry->d_name) < 0 ||
            symlink(target, link)) {
            perror("rate shared input link"); exit(EXIT_FAILURE);
        }
        free(target); free(link);
    }
    closedir(input);
    if (chdir(directory)) { perror("rate private cwd"); exit(EXIT_FAILURE); }
}

static void fail(const char *message) { perror(message); exit(EXIT_FAILURE); }
static void wait_tokens(int descriptor, int tokens, char expected,
                        pid_t *children, int copies) {
    for (int i = 0; i < tokens; ++i) {
        struct pollfd observed = {descriptor, POLLIN, 0};
        int events;
        do {
            events = poll(&observed, 1, 100);
            if (events < 0 && errno != EINTR) fail("rate poll");
            for (int j = 0; j < copies; ++j) {
                int status;
                if (waitpid(children[j], &status, WNOHANG) == children[j]) {
                    for (int k = 0; k < copies; ++k) if (k != j) kill(children[k], SIGTERM);
                    for (int k = 0; k < copies; ++k) if (k != j) waitpid(children[k], NULL, 0);
                    fprintf(stderr, "rate copy exited before all copies were ready\n");
                    exit(EXIT_FAILURE);
                }
            }
        } while (events <= 0);
        char token; ssize_t n;
        do { n = read(descriptor, &token, 1); } while (n < 0 && errno == EINTR);
        if (n != 1 || token != expected) {
            for (int j = 0; j < copies; ++j) kill(children[j], SIGTERM);
            fprintf(stderr, "rate copy exited before ready\n"); exit(EXIT_FAILURE);
        }
    }
}
static __attribute__((noinline)) void native_identity_checkpoint(void) {
    if (getenv("GEM5_DISABLE_ROI_MARKER")) return;
    fputs("[gem5-roi] marker=native-identities-ready operation=checkpoint workid=0\n", stderr);
    fflush(stderr);
    /* Official util/m5/src/abi/x86/m5op.S instruction ABI, O3 only. A bare
     * MMIO store retires before its local-access callback and can therefore
     * sample younger committed RDI/RSI values as checkpoint delay/period. */
    __asm__ volatile(".byte 0x0f, 0x04\n\t.word 0x43"
                     : : "D"((uint64_t)0), "S"((uint64_t)0) : "rax", "memory");
}

static void handoff(const char *name) {
    /* O3's instruction checkpoint is precise and needs no KVM grace sleep. */
    if (!strcmp(name, "native-identities-ready")) {
        native_identity_checkpoint();
        return;
    }
    if (!getenv("GEM5_DISABLE_ROI_MARKER")) gem5_roi_emit(name, "checkpoint", 0);
    /* KVM's MMIO exit is asynchronous. Children stay blocked until O3 owns CPUs. */
    struct timespec pause = {0, 5000000};
    while (nanosleep(&pause, &pause)) if (errno != EINTR) fail("handoff sleep");
}

int main(int argc, char **argv) {
    if (argc < 5 || strcmp(argv[1], "--cpus")) return 2;
    int option = 3, native_workers = 0, private_cwd = 0;
    if (!strcmp(argv[option], "--native-workers")) {
        if (argc <= option + 2) return 2;
        char *last; long workers = strtol(argv[option + 1], &last, 10);
        if (*last || workers <= 0 || workers > CPU_SETSIZE) return 2;
        native_workers = (int)workers; option += 2;
    }
    if (!strcmp(argv[option], "--private-cwd")) { private_cwd = 1; ++option; }
    int binary_index = option + 1;
    if (argc <= binary_index || strcmp(argv[binary_index - 1], "--")) return 2;
    int cpus[CPU_SETSIZE], count = 0;
    char *end, *text = argv[2];
    while (*text) {
        long cpu = strtol(text, &end, 10);
        if (end == text || cpu < 0 || cpu >= CPU_SETSIZE || count == CPU_SETSIZE) return 2;
        for (int i = 0; i < count; ++i) if (cpus[i] == cpu) return 2;
        cpus[count++] = (int)cpu;
        if (*end && *end != ',') return 2;
        text = *end ? end + 1 : end;
    }
    if (!count) return 2;
    if (native_workers && native_workers < count) return 2;
    int copies = native_workers ? 1 : count;
    int ready_count = native_workers ? native_workers : copies;
    int serial_init = getenv("FASTSIM_RATE_SERIAL_INIT") != NULL;
    int ready_checkpoint = getenv("FASTSIM_RATE_READY_CHECKPOINT") != NULL;
    int warmup_after_identities = getenv("FASTSIM_NATIVE_WARMUP_AFTER_IDENTITIES") != NULL;
    if (warmup_after_identities && !native_workers) return 2;
    if (native_workers && (serial_init || ready_checkpoint)) return 2;
    /* This first marker saves real RAM while the launcher has no children. */
    puts("[fastsim-rate] staged-before-fork"); fflush(stdout);
    handoff("rate-staged-before-fork");
    int ready[2], release[2];
    if (pipe(ready) || pipe(release)) fail("pipe");
    int continuation[2] = {-1, -1};
    if (native_workers && pipe(continuation)) fail("native continuation pipe");
    pid_t children[CPU_SETSIZE];
    for (int i = 0; i < copies; ++i) {
        children[i] = fork();
        if (children[i] < 0) fail("fork");
        if (!children[i]) {
            close(ready[0]); close(release[1]);
            cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(cpus[i], &mask);
            if (sched_setaffinity(0, sizeof(mask), &mask)) fail("rate affinity");
            if (native_workers) {
                close(continuation[1]);
                if (setenv("FASTSIM_NATIVE_CPUS", argv[2], 1)) fail("setenv");
                char workers[32]; snprintf(workers, sizeof(workers), "%d", native_workers);
                if (setenv("FASTSIM_NATIVE_WORKERS", workers, 1)) fail("setenv");
                snprintf(workers, sizeof(workers), "%d", continuation[0]);
                if (setenv("FASTSIM_NATIVE_CONTINUE_FD", workers, 1)) fail("setenv");
            }
            char value[32];
            snprintf(value, sizeof(value), "%d", ready[1]);
            if (setenv("FASTSIM_RATE_READY_FD", value, 1)) fail("setenv");
            snprintf(value, sizeof(value), "%d", release[0]);
            if (setenv("FASTSIM_RATE_RELEASE_FD", value, 1)) fail("setenv");
            snprintf(value, sizeof(value), "%d", i);
            if (setenv("FASTSIM_RATE_COPY", value, 1)) fail("setenv");
            char file[64]; snprintf(file, sizeof(file), "copy-%d.stdout", i);
            if (!freopen(file, "w", stdout)) fail("rate stdout");
            snprintf(file, sizeof(file), "copy-%d.stderr", i);
            if (!freopen(file, "w", stderr)) fail("rate stderr");
            if (private_cwd) private_copy_directory(i);
            execv(argv[binary_index], argv + binary_index); fail("rate exec");
        }
        /* Keep transient setup memory bounded; all copies still compute together. */
        if (serial_init) wait_tokens(ready[0], 1, 'R', children, i + 1);
    }
    close(ready[1]); close(release[0]);
    if (native_workers) close(continuation[0]);
    if (!serial_init) wait_tokens(ready[0], ready_count, 'R', children, copies);
    if (!native_workers) close(ready[0]);
    if (native_workers) printf("[fastsim-rate] all-ready copies=1 workers=%d\n", native_workers);
    else printf("[fastsim-rate] all-ready copies=%d\n", copies);
    fflush(stdout);
    if (!ready_checkpoint && !native_workers) handoff("rate-all-copies-compute-ready");
    /* Native workers are already blocked at their real computation gates.
     * Read their full roster under KVM, before the detailed warmup starts.
     * Keep the historical census labels for existing population auditors. */
    if (native_workers) puts("[fastsim-native] census-phase=pre-switch");
    /* Ready mode likewise reads /proc under KVM with every child blocked. */
    pid_t *task_ids[CPU_SETSIZE] = {0};
    int task_counts[CPU_SETSIZE] = {0};
    long rss_kb[CPU_SETSIZE] = {0}, swap_kb[CPU_SETSIZE] = {0};
    puts(ready_checkpoint ? "[fastsim-rate] pre-checkpoint-census begin" :
                           "[fastsim-rate] post-switch-census begin");
    for (int i = 0; i < copies; ++i) {
        char path[64], line[256];
        snprintf(path, sizeof(path), "/proc/%d/status", children[i]);
        FILE *status = fopen(path, "r");
        if (!status) fail("rate task status");
        printf("[fastsim-rate] copy=%d pid=%d cpu=%d", i, children[i], cpus[i]);
        while (fgets(line, sizeof(line), status)) {
            sscanf(line, "VmRSS: %ld", &rss_kb[i]);
            sscanf(line, "VmSwap: %ld", &swap_kb[i]);
            if (!strncmp(line, "VmRSS:", 6) || !strncmp(line, "VmSwap:", 7) ||
                !strncmp(line, "Threads:", 8)) {
                line[strcspn(line, "\n")] = 0; printf(" %s", line);
            }
        }
        fclose(status); putchar('\n');
        snprintf(path, sizeof(path), "/proc/%d/task", children[i]);
        DIR *tasks = opendir(path);
        if (!tasks) fail("rate task population");
        struct dirent *task;
        task_ids[i] = calloc(CPU_SETSIZE, sizeof(pid_t));
        if (!task_ids[i]) fail("rate census allocation");
        while ((task = readdir(tasks))) {
            char *last;
            long tid = strtol(task->d_name, &last, 10);
            if (tid > 0 && !*last) {
                if (task_counts[i] == CPU_SETSIZE) fail("rate census capacity");
                task_ids[i][task_counts[i]++] = (pid_t)tid;
                printf("[fastsim-rate] copy=%d task_tid=%ld task_tgid=%d\n",
                       i, tid, children[i]);
            }
        }
        closedir(tasks);
        if (ready_checkpoint && (task_counts[i] != 1 || task_ids[i][0] != children[i])) {
            fprintf(stderr, "compute-ready checkpoint requires single-threaded rate copies\n");
            return EXIT_FAILURE;
        }
    }
    puts(ready_checkpoint ? "[fastsim-rate] pre-checkpoint-census end" :
                           "[fastsim-rate] post-switch-census end");
    fflush(stdout);
    if (native_workers) {
        /* fflush alone leaves bytes queued in the guest TTY. Finish the
         * census transmission before switching; identities still execute
         * under O3 so their committed marker PCs remain trace evidence. */
        if (isatty(STDOUT_FILENO)) {
            while (tcdrain(STDOUT_FILENO)) {
                if (errno != EINTR) fail("native pre-switch serial drain");
            }
        }
        handoff("rate-all-copies-compute-ready");
    }
    if (ready_checkpoint) {
        puts("[fastsim-rate] compute-ready-checkpoint"); fflush(stdout);
        /* fflush only fills the kernel's TTY queue. Keep pending census bytes
         * out of the restored O3 warmup, and retain the full roster in UART. */
        if (isatty(STDOUT_FILENO)) {
            while (tcdrain(STDOUT_FILENO)) {
                if (errno != EINTR) fail("rate pre-checkpoint serial drain");
            }
        }
        handoff("rate-compute-ready-checkpoint");
        /* A distinct marker executes after both fresh save and checkpoint restore. */
        puts("[fastsim-rate] compute-ready-resume"); fflush(stdout);
        handoff("rate-compute-ready-resume");
        puts("[fastsim-rate] post-switch-census begin");
        for (int i = 0; i < copies; ++i) {
            /* Same blocked children and inherited pipe: confirm liveness before release. */
            if (kill(children[i], 0)) fail("rate restored child identity");
            printf("[fastsim-rate] copy=%d pid=%d cpu=%d VmRSS: %ld kB VmSwap: %ld kB Threads: %d\n",
                   i, children[i], cpus[i], rss_kb[i], swap_kb[i], task_counts[i]);
            for (int j = 0; j < task_counts[i]; ++j)
                printf("[fastsim-rate] copy=%d task_tid=%d task_tgid=%d\n",
                       i, task_ids[i][j], children[i]);
        }
        puts("[fastsim-rate] post-switch-census end"); fflush(stdout);
    }
    for (int i = 0; i < copies; ++i) free(task_ids[i]);
    puts("[fastsim-rate] compute-gate-release"); fflush(stdout);
    for (int i = 0; i < ready_count; ++i) if (write(release[1], "G", 1) != 1) fail("release");
    close(release[1]);
    if (native_workers) {
        wait_tokens(ready[0], ready_count, 'A', children, copies);
        close(ready[0]);
        printf("[fastsim-native] identities-ready workers=%d\n", native_workers); fflush(stdout);
        /* All committed task identities now exist under O3. Arm the bounded
         * compute warmup before releasing any native worker to computation. */
        if (warmup_after_identities) handoff("native-identities-ready");
        for (int i = 0; i < ready_count; ++i)
            if (write(continuation[1], "C", 1) != 1) fail("native admission");
        close(continuation[1]);
    }
    int failed = 0;
    for (int i = 0; i < copies; ++i) {
        int status; pid_t result;
        do { result = waitpid(children[i], &status, 0); } while (result < 0 && errno == EINTR);
        if (result < 0 || !WIFEXITED(status) || WEXITSTATUS(status)) failed = 1;
    }
    return failed;
}
