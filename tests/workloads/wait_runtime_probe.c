/* Host mechanism fixture: execute the production x86 inline futex path.
   Single-process/no-fork pilot. Host timings never become model inputs. */
#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/syscall.h>
#include <sys/prctl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <linux/futex.h>
#include <omp.h>
extern int gomp_futex_wait, gomp_futex_wake;
extern unsigned long long gomp_fastsim_thread_id(void);
#include "futex.h"

struct event { unsigned long long kind, runtime, tid, request, object, detail; };
static struct event events[8192];
static unsigned event_count;
static __thread unsigned long long native_tid;
static int word;
static unsigned long long sync_tid[64];
static unsigned long long identity_tid[64];
static int fixture_mode;
void fastsim_sync_marker(unsigned long long kind, unsigned long long runtime,
                         unsigned long long region, unsigned long long object,
                         unsigned long long generation, unsigned long long members)
{
  (void)kind; (void)region; (void)object; (void)generation; (void)members;
  unsigned long long tid = syscall(SYS_gettid);
  if (runtime >= 64) abort();
  if (fixture_mode == 2 && identity_tid[runtime] != tid) abort();
  unsigned long long previous = __atomic_exchange_n(&sync_tid[runtime], tid, __ATOMIC_RELAXED);
  if (previous && previous != tid) abort();
}
void fastsim_wait_marker(unsigned long long kind, unsigned long long runtime,
                         unsigned long long tid, unsigned long long request,
                         unsigned long long object, unsigned long long detail)
{
  if (!native_tid) native_tid = syscall(SYS_gettid);
  if (tid != native_tid || (kind == 1 && detail != (unsigned long long)getpid())) abort();
  if (runtime >= 64) abort();
  if (kind == 1) {
    identity_tid[runtime] = tid;
    /* The getter may be consulted by an observer without recursive markers. */
    if (fixture_mode == 3 && gomp_fastsim_thread_id() != runtime) abort();
  }
  unsigned i = __atomic_fetch_add(&event_count, 1, __ATOMIC_RELAXED);
  if (i >= sizeof(events)/sizeof(events[0])) abort();
  events[i] = (struct event){kind,runtime,tid,request,object,detail};
}
static void reject_private_futex(void)
{
  struct sock_filter code[] = {
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, arch)),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, AUDIT_ARCH_X86_64, 1, 0),
    BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_KILL_PROCESS),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, nr)),
    BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_futex, 0, 3),
    BPF_STMT(BPF_LD|BPF_W|BPF_ABS, offsetof(struct seccomp_data, args[1])),
    BPF_JUMP(BPF_JMP|BPF_JSET|BPF_K, FUTEX_PRIVATE_FLAG, 0, 1),
    BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|ENOSYS),
    BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ALLOW),
  };
  struct sock_fprog prog = {sizeof(code)/sizeof(code[0]), code};
  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog)) abort();
}
static void *waiter(void *arg)
{
  (void)arg;
  if (__futex_wait(&word, FUTEX_WAIT_PRIVATE, 1) != -EAGAIN) abort();
  if (__futex_wait(&word, FUTEX_WAIT_PRIVATE, 0) != 0) abort();
  return NULL;
}
int main(int argc, char **argv)
{
  if (argc != 2) return 2;
  int mode = atoi(argv[1]);
  fixture_mode = mode;
  if (mode == 3) {
    unsigned long long runtime = gomp_fastsim_thread_id();
    for (unsigned i=0; i<100; ++i) word += (int)i;
    if (runtime != gomp_fastsim_thread_id() || word != 4950) abort();
  } else if (mode == 2) {
    #pragma omp parallel num_threads(4)
    {
      if (omp_get_thread_num() == 0) usleep(10000);
      #pragma omp barrier
    }
    /* Finish worker-pool futex requests before inspecting the capture buffer. */
    if (omp_pause_resource_all(omp_pause_hard)) abort();
  } else if (mode == 1) {
    gomp_futex_wait = FUTEX_WAIT_PRIVATE;
    gomp_futex_wake = FUTEX_WAKE_PRIVATE;
    reject_private_futex();
    futex_wait(&word, 1);
    /* Restore PRIVATE separately to force the independent wake fallback. */
    gomp_futex_wake = FUTEX_WAKE_PRIVATE;
    futex_wake(&word, 1);
  } else {
    pthread_t th;
    if (pthread_create(&th, NULL, waiter, NULL)) abort();
    long count = 0;
    for (unsigned i=0; i<1000 && count == 0; ++i) {
      usleep(1000);
      count = __futex_wake(&word, FUTEX_WAKE_PRIVATE, 1);
    }
    if (count != 1 || pthread_join(th, NULL)) abort();
  }
  for (unsigned i=0; i<event_count; ++i) {
    struct event *e=&events[i];
    if (mode == 2 && sync_tid[e->runtime] != e->tid) abort();
    printf("{\"kind\":%llu,\"runtime\":%llu,\"tid\":%llu,\"request\":%llu,\"object\":%llu,\"detail\":%lld}\n",
           e->kind,e->runtime,e->tid,e->request,e->object,(long long)e->detail);
  }
  return 0;
}
