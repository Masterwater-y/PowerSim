#ifndef FASTSIM_DIV_SIDECAR_H
#define FASTSIM_DIV_SIDECAR_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define FASTSIM_DIV_SIDECAR_MAGIC UINT64_C(0x4653444956455632) /* FSDIVEV2 */
#define FASTSIM_DIV_SIDECAR_VERSION 2U
#define FASTSIM_DIV_KIND_DIV 1U
#define FASTSIM_DIV_KIND_IDIV 2U
#define FASTSIM_DIV_OPERAND_REGISTER 1U
#define FASTSIM_DIV_OPERAND_MEMORY 2U
#define FASTSIM_DIV_OUTCOME_RETIRED 1U
#define FASTSIM_DIV_OUTCOME_FAULTED 2U
#define FASTSIM_DIV_EVIDENCE_DIVISOR_VALID 0x0001U

typedef struct {
    uint64_t magic;
    uint64_t capture_id_hi;
    uint64_t capture_id_lo;
    int64_t pid;
    int64_t tid;
    uint16_t version;
    uint16_t header_size;
    uint16_t record_size;
    uint16_t flags;
} fastsim_div_sidecar_header_t;

typedef struct {
    uint64_t sequence;
    uint64_t pc;
    uint64_t rax;
    uint64_t rdx;
    uint64_t divisor;
    uint64_t post_rax;
    uint64_t post_rdx;
    uint8_t kind;
    uint8_t width;
    uint8_t operand_kind;
    uint8_t outcome;
    uint16_t fault_code;
    uint16_t flags;
} fastsim_div_sidecar_record_t;

#ifdef __cplusplus
}

static_assert(sizeof(fastsim_div_sidecar_header_t) == 48);
static_assert(sizeof(fastsim_div_sidecar_record_t) == 64);
#endif

#endif
