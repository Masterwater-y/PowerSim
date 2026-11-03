/* Unsupported constructs must be observed, never silently certified. */
#include <omp.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    int cancel = argc > 1 ? atoi(argv[1]) : 0;
    volatile unsigned long total = 0;
    omp_set_num_threads(2);
#pragma omp parallel shared(total)
    {
#pragma omp single
        {
#pragma omp taskloop
            for (unsigned long long i = 0; i < 4; ++i) {
#pragma omp atomic update
                total += i;
            }
        }
#pragma omp cancel parallel if(cancel)
    }
#ifdef FASTSIM_SYNC_HOST_CAPTURE
    extern void fastsim_sync_host_finish(void);
    fastsim_sync_host_finish();
#endif
    return !cancel && total != 6;
}
