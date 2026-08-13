#include <stdint.h>

#if defined(__GNUC__) || defined(__clang__)
#define FASTSIM_MARKER \
    __attribute__(( \
        noinline, used, visibility("default"), \
        section(".fastsim_dynamoRIO_marker_text")))
#else
#define FASTSIM_MARKER
#endif

FASTSIM_MARKER void
cpu_microarch_roi_thread_begin(uint64_t work_id, uint64_t thread_id)
{
    (void)work_id;
    (void)thread_id;
}

FASTSIM_MARKER void
cpu_microarch_roi_thread_end(uint64_t work_id, uint64_t thread_id)
{
    (void)work_id;
    (void)thread_id;
}
