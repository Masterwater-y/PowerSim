/* Four-core MOVNTDQ debug-prefix/source-boundary probe, not an accuracy workload. */
#define _GNU_SOURCE
#include <omp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <time.h>
#include <emmintrin.h>
#include "gem5_roi_marker.h"

static uint64_t lanes[4][8] __attribute__((aligned(64)));
static void compute(unsigned member,unsigned count)
{
    for (unsigned i=0;i<count;++i) {
        __m128i value=_mm_set1_epi64x((long long)(i+member));
        _mm_stream_si128((__m128i*)&lanes[member][0],value);
        _mm_sfence();
        __asm__ volatile("" ::: "memory");
    }
}
int main(void)
{
    omp_set_dynamic(0);omp_set_num_threads(4);
    if (!getenv("GEM5_DISABLE_ROI_MARKER")) {
        gem5_roi_checkpoint_once("perf-macro-boundary-checkpoint",1);
        struct timespec pause={0,5000000};
        while(nanosleep(&pause,&pause))if(errno!=EINTR)abort();
    }
#pragma omp parallel
    compute((unsigned)omp_get_thread_num(),16);
    if (!getenv("GEM5_DISABLE_ROI_MARKER"))
        gem5_roi_begin_once("perf-macro-boundary-stat",1);
#pragma omp parallel
    compute((unsigned)omp_get_thread_num(),200000);
    for(unsigned t=0;t<4;++t)
        if(lanes[t][0]!=199999+t || lanes[t][1]!=199999+t)return 1;
    puts("perf-macro-boundary four-core host check passed");return 0;
}
