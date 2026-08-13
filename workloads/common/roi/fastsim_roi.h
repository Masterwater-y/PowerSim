#ifndef FASTSIM_WORKLOAD_ROI_H
#define FASTSIM_WORKLOAD_ROI_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void cpu_microarch_roi_thread_begin(uint64_t work_id, uint64_t thread_id);
void cpu_microarch_roi_thread_end(uint64_t work_id, uint64_t thread_id);

#ifdef __cplusplus
}
#endif

static inline void
fastsim_roi_thread_begin(uint64_t thread_id)
{
    register uint64_t work_id_reg __asm__("rdi") = 0;
    register uint64_t thread_id_reg __asm__("rsi") = thread_id;
#ifdef FASTSIM_DR_TRACE
    __asm__ __volatile__(
        "call cpu_microarch_roi_thread_begin"
        : "+D"(work_id_reg), "+S"(thread_id_reg)
        :
        : "rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "cc", "memory");
#else
    __asm__ __volatile__(
        "nop; .byte 0x0F, 0x04; .word 0x005a"
        : "+D"(work_id_reg), "+S"(thread_id_reg)
        :
        : "rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "cc", "memory");
#endif
}

static inline void
fastsim_roi_thread_end(uint64_t thread_id)
{
    register uint64_t work_id_reg __asm__("rdi") = 0;
    register uint64_t thread_id_reg __asm__("rsi") = thread_id;
#ifdef FASTSIM_DR_TRACE
    __asm__ __volatile__(
        "call cpu_microarch_roi_thread_end"
        : "+D"(work_id_reg), "+S"(thread_id_reg)
        :
        : "rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "cc", "memory");
#else
    __asm__ __volatile__(
        ".byte 0x0F, 0x04; .word 0x005b; nop"
        : "+D"(work_id_reg), "+S"(thread_id_reg)
        :
        : "rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "cc", "memory");
#endif
}

static inline void
fastsim_roi_quiesce(void)
{
#ifdef FASTSIM_DR_TRACE
    __asm__ __volatile__("nop; nop; nop; nop" : : : "memory");
#else
    __asm__ __volatile__(".byte 0x0F, 0x04; .word 0x0001" : : : "rax", "memory");
#endif
}

#endif
