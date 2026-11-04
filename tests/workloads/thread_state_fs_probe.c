/* Fixed-process C4 scheduler/wait mechanism probe, not an accuracy oracle.
   Runtime identities are emitted only after pinning and serial workbegin. */
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <sched.h>
#include <linux/futex.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include "gem5_roi_marker.h"
extern int gomp_futex_wait, gomp_futex_wake;
#include "futex.h"

static int wait_word[4];
static unsigned ready_round[4], complete_round[4];
static unsigned initialized[4], start_workers;
static unsigned rounds=1000, work=2000;
static unsigned long long result[4];
static int cpu_ids[4]={0,1,2,3};

static void pin(unsigned member)
{
  cpu_set_t mask;
  CPU_ZERO(&mask);
  CPU_SET(cpu_ids[member], &mask);
  if (sched_setaffinity(0, sizeof(mask), &mask)) { perror("sched_setaffinity"); abort(); }
}
static void compute(unsigned member, unsigned round)
{
  unsigned long long sum=result[member]+round;
  for (unsigned i=0; i<work; ++i) {
    sum=(sum*1664525ULL+1013904223ULL) ^ (i+member);
    __asm__ volatile ("" : "+r"(sum));
  }
  result[member]=sum;
}
static void *worker(void *argument)
{
  unsigned member=(unsigned)(uintptr_t)argument;
  pin(member);
  __atomic_store_n(&initialized[member], 1, __ATOMIC_RELEASE);
  while (!__atomic_load_n(&start_workers, __ATOMIC_ACQUIRE)) {
    // Startup uses an uninstrumented OS wait: no runtime identity/request is
    // emitted before the collection switch has actually taken effect.
    if (syscall(SYS_futex,&start_workers,FUTEX_WAIT_PRIVATE,0,NULL,NULL,0) &&
        errno!=EAGAIN && errno!=EINTR) abort();
  }
  gomp_fastsim_thread_id();
  if (__futex_wait(&wait_word[member], FUTEX_WAIT_PRIVATE, 1) != -EAGAIN) abort();
  for (unsigned round=1; round<=rounds; ++round) {
    __atomic_store_n(&ready_round[member], round, __ATOMIC_RELEASE);
    /* Each address has one waiter. Main retries wake until kernel enrollment,
       while the futex comparison remains 0, avoiding a lost-wake race. */
    if (__futex_wait(&wait_word[member], FUTEX_WAIT_PRIVATE, 0) != 0) abort();
    compute(member, round);
    __atomic_store_n(&complete_round[member], round, __ATOMIC_RELEASE);
  }
  return NULL;
}
int main(int argc, char **argv)
{
  if (argc>3) return 2;
  if (argc>1) rounds=(unsigned)strtoul(argv[1], NULL, 10);
  if (argc>2) work=(unsigned)strtoul(argv[2], NULL, 10);
  if (!rounds || rounds>100000 || !work || work>100000000) return 2;
  /* Only for a host smoke test with restricted CPU sets; FS defaults are 0..3. */
  const char *cpus=getenv("FASTSIM_PROBE_CPUS");
  if (cpus && sscanf(cpus,"%d,%d,%d,%d", &cpu_ids[0], &cpu_ids[1], &cpu_ids[2], &cpu_ids[3])!=4) return 2;
  for (unsigned i=0;i<4;++i) {
    if (cpu_ids[i]<0 || cpu_ids[i]>=CPU_SETSIZE) return 2;
    for (unsigned j=0;j<i;++j) if (cpu_ids[i]==cpu_ids[j]) return 2;
  }
  pin(0);
  pthread_t threads[3];
  for (unsigned member=1;member<4;++member)
    if (pthread_create(&threads[member-1],NULL,worker,(void *)(uintptr_t)member)) abort();
  for (unsigned member=1;member<4;++member)
    while (!__atomic_load_n(&initialized[member],__ATOMIC_ACQUIRE)) __asm__ volatile("pause");
  // Finish creation/migration under KVM. The captured slice begins with four
  // fixed owners; runtime identities are deliberately emitted after begin.
  gem5_roi_checkpoint_once("thread-state-probe-checkpoint", 1);
  // A checkpoint exit is asynchronous under KVM. Keep all runtime markers
  // after its 1ms global quantum, with other cores asleep during the prefix.
  struct timespec prefix={0,5000000};
  while (nanosleep(&prefix,&prefix)) if (errno!=EINTR) abort();
#ifdef FASTSIM_TASK_CLOCK_WARMUP
  /* Exercise all worker contexts under O3 before measuring. This provides
     observed lifecycle cuts instead of assuming a kernel context is enabled. */
  gomp_fastsim_thread_id();
  __atomic_store_n(&start_workers, 1, __ATOMIC_RELEASE);
  if (syscall(SYS_futex,&start_workers,FUTEX_WAKE_PRIVATE,3,NULL,NULL,0)<0) abort();
  for (unsigned member=1;member<4;++member)
    while (__atomic_load_n(&ready_round[member],__ATOMIC_ACQUIRE)<1)
      __asm__ volatile ("pause");
#endif
  gem5_roi_begin_once("thread-state-probe-measurement", 1);
#ifndef FASTSIM_TASK_CLOCK_WARMUP
  gomp_fastsim_thread_id();
  __atomic_store_n(&start_workers, 1, __ATOMIC_RELEASE);
  if (syscall(SYS_futex,&start_workers,FUTEX_WAKE_PRIVATE,3,NULL,NULL,0)<0) abort();
#endif
  for (unsigned round=1;round<=rounds;++round) {
    for (unsigned member=1;member<4;++member) {
      while (__atomic_load_n(&ready_round[member],__ATOMIC_ACQUIRE)<round)
        __asm__ volatile ("pause");
      long count;
      do {
        count=__futex_wake(&wait_word[member], FUTEX_WAKE_PRIVATE, 1);
        if (count<0) abort();
        if (!count) sched_yield();
      } while (!count);
    }
    compute(0, round);
    for (unsigned member=1;member<4;++member)
      while (__atomic_load_n(&complete_round[member],__ATOMIC_ACQUIRE)<round)
        __asm__ volatile ("pause");
  }
  for (unsigned member=1;member<4;++member)
    if (pthread_join(threads[member-1],NULL)) abort();
  printf("thread-state-probe rounds=%u work=%u completed=%u checksum=%llu\n",
         rounds,work,complete_round[3],result[0]^result[1]^result[2]^result[3]);
  return 0;
}
