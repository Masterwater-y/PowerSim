/* Test-only strong transport captures semantic events from the REAL runtime. */
#include <stdio.h>
#include <stdint.h>
#include <pthread.h>
#include <stdlib.h>

static pthread_mutex_t observation_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t observation_ready = PTHREAD_COND_INITIALIZER;
static unsigned final_arrivals;
static unsigned final_departures;

void fastsim_sync_marker(uint64_t kind, uint64_t thread, uint64_t region,
                         uint64_t object, uint64_t generation, uint64_t packed)
{
    pthread_mutex_lock(&observation_lock);
    if (kind == 7) ++final_arrivals;
    if (kind == 8) {
        ++final_departures;
        pthread_cond_broadcast(&observation_ready);
    }
    flockfile(stderr);
    fprintf(stderr, "{\"event_kind\":%llu,\"thread_id\":%llu,"
            "\"region_instance\":%llu,\"sync_object_id\":%llu,"
            "\"generation\":%llu,\"member_index\":%u,\"team_size\":%u}\n",
            (unsigned long long)kind, (unsigned long long)thread,
            (unsigned long long)region, (unsigned long long)object,
            (unsigned long long)generation, (unsigned)packed, (unsigned)(packed >> 32));
    funlockfile(stderr);
    pthread_mutex_unlock(&observation_lock);
}

void fastsim_sync_host_finish(void)
{
    /* Only hold process exit, not each join: the master remains free to reuse
       team storage while an old worker is about to emit its departure. */
    pthread_mutex_lock(&observation_lock);
    while (final_departures < final_arrivals)
        pthread_cond_wait(&observation_ready, &observation_lock);
    pthread_mutex_unlock(&observation_lock);
}
