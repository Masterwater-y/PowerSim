/* Record retired scalar x86 DIV/IDIV inputs beside an unmodified drmemtrace. */
#include "dr_api.h"
#include "drmgr.h"
#include "drreg.h"
#include "drutil.h"
#include "drmemtrace/drmemtrace.h"
#include "fastsim/div_sidecar.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PENDING_DEPTH 64U
#define OUTPUT_RECORDS 1024U

typedef struct {
    fastsim_div_sidecar_record_t pending[PENDING_DEPTH];
    fastsim_div_sidecar_record_t output[OUTPUT_RECORDS];
    uint32_t pending_depth;
    uint32_t output_count;
    uint64_t next_sequence;
    file_t file;
} thread_state_t;

static int tls_idx;
static char sidecar_dir[MAXIMUM_PATH];
static uint64_t capture_id_hi;
static uint64_t capture_id_lo;
static reg_id_t raw_tls_reg;
static uint raw_tls_offs;

static uint64_t
width_mask(uint32_t width)
{
    return width == 8 ? UINT64_MAX : ((UINT64_C(1) << (width * 8)) - 1);
}

static thread_state_t *
state_for(void *drcontext)
{
    return (thread_state_t *)drmgr_get_tls_field(drcontext, tls_idx);
}

static void
flush_output(thread_state_t *state)
{
    if (state->output_count == 0)
        return;
    const size_t bytes = state->output_count * sizeof(state->output[0]);
    DR_ASSERT(dr_write_file(state->file, state->output, bytes) == (ssize_t)bytes);
    state->output_count = 0;
}

static void
append_record(thread_state_t *state, const fastsim_div_sidecar_record_t *record)
{
    state->output[state->output_count++] = *record;
    ++state->next_sequence;
    if (state->output_count == OUTPUT_RECORDS)
        flush_output(state);
}

static void
push_candidate(uint64_t divisor, bool divisor_valid, app_pc pc, uint32_t kind,
               uint32_t width, uint32_t operand_kind)
{
    void *drcontext = dr_get_current_drcontext();
    thread_state_t *state = state_for(drcontext);
    dr_mcontext_t mc = {
        sizeof(mc),
        (dr_mcontext_flags_t)(DR_MC_CONTROL | DR_MC_INTEGER),
    };
    DR_ASSERT(state != NULL && state->pending_depth < PENDING_DEPTH);
    DR_ASSERT(dr_get_mcontext(drcontext, &mc));
    fastsim_div_sidecar_record_t *record =
        &state->pending[state->pending_depth++];
    memset(record, 0, sizeof(*record));
    record->sequence = state->next_sequence;
    record->pc = (uint64_t)(ptr_uint_t)pc;
    record->rax = (uint64_t)mc.xax;
    record->rdx = (uint64_t)mc.xdx;
    record->divisor = divisor & width_mask(width);
    record->kind = (uint8_t)kind;
    record->width = (uint8_t)width;
    record->operand_kind = (uint8_t)operand_kind;
    record->outcome = FASTSIM_DIV_OUTCOME_RETIRED;
    if (divisor_valid)
        record->flags |= FASTSIM_DIV_EVIDENCE_DIVISOR_VALID;
}

static void
record_pre_register(uint32_t reg, app_pc pc, uint32_t kind, uint32_t width)
{
    void *drcontext = dr_get_current_drcontext();
    dr_mcontext_t mc = {
        sizeof(mc),
        (dr_mcontext_flags_t)(DR_MC_CONTROL | DR_MC_INTEGER),
    };
    DR_ASSERT(dr_get_mcontext(drcontext, &mc));
    push_candidate((uint64_t)reg_get_value((reg_id_t)reg, &mc), true, pc,
                   kind, width, FASTSIM_DIV_OPERAND_REGISTER);
}

static void
record_pre_memory(app_pc pc, uint32_t kind, uint32_t width)
{
    uint64_t divisor = 0;
    size_t bytes_read = 0;
    const uintptr_t address = *(const uintptr_t *)
        ((const byte *)dr_get_dr_segment_base(raw_tls_reg) + raw_tls_offs);
    const bool valid = dr_safe_read((const void *)address, width, &divisor,
                                    &bytes_read) && bytes_read == width;
    push_candidate(divisor, valid, pc, kind, width,
                   FASTSIM_DIV_OPERAND_MEMORY);
}

/* This executes only after the application instruction retired. */
static void
record_post(void)
{
    thread_state_t *state = state_for(dr_get_current_drcontext());
    dr_mcontext_t mc = {
        sizeof(mc),
        (dr_mcontext_flags_t)(DR_MC_CONTROL | DR_MC_INTEGER),
    };
    DR_ASSERT(state != NULL);
    DR_ASSERT(state->pending_depth != 0);
    DR_ASSERT(dr_get_mcontext(dr_get_current_drcontext(), &mc));
    fastsim_div_sidecar_record_t record =
        state->pending[--state->pending_depth];
    DR_ASSERT(record.sequence == state->next_sequence);
    record.post_rax = (uint64_t)mc.xax;
    record.post_rdx = (uint64_t)mc.xdx;
    append_record(state, &record);
}

#ifdef UNIX
static dr_signal_action_t
event_signal(void *drcontext, dr_siginfo_t *info)
{
    thread_state_t *state = state_for(drcontext);
    if (state != NULL && state->pending_depth != 0 && info->mcontext != NULL) {
        const fastsim_div_sidecar_record_t *top =
            &state->pending[state->pending_depth - 1];
        if ((uint64_t)(ptr_uint_t)info->mcontext->pc == top->pc) {
            fastsim_div_sidecar_record_t record =
                state->pending[--state->pending_depth];
            record.outcome = FASTSIM_DIV_OUTCOME_FAULTED;
            record.fault_code = (uint16_t)info->sig;
            append_record(state, &record);
        }
    }
    return DR_SIGNAL_DELIVER;
}
#endif

static dr_emit_flags_t
instrument(void *drcontext, void *tag, instrlist_t *bb, instr_t *instr,
           bool for_trace, bool translating, void *user_data)
{
    (void)tag; (void)for_trace; (void)translating; (void)user_data;
    const int opcode = instr_get_opcode(instr);
    if (opcode != OP_div && opcode != OP_idiv)
        return DR_EMIT_DEFAULT;

    opnd_t divisor = instr_get_src(instr, 0);
    const uint32_t width = (uint32_t)opnd_size_in_bytes(opnd_get_size(divisor));
    DR_ASSERT(width == 1 || width == 2 || width == 4 || width == 8);
    const opnd_t pc = OPND_CREATE_INTPTR(instr_get_app_pc(instr));
    const opnd_t op = OPND_CREATE_INT32(
        opcode == OP_div ? FASTSIM_DIV_KIND_DIV : FASTSIM_DIV_KIND_IDIV);
    const opnd_t size = OPND_CREATE_INT32(width);

    if (opnd_is_reg(divisor)) {
        dr_insert_clean_call_ex(
            drcontext, bb, instr, (void *)record_pre_register,
            DR_CLEANCALL_READS_APP_CONTEXT, 4,
            OPND_CREATE_INT32((uint32_t)opnd_get_reg(divisor)), pc, op, size);
    } else {
        reg_id_t address_reg, scratch_reg;
        DR_ASSERT(opnd_is_memory_reference(divisor));
        DR_ASSERT(drreg_reserve_register(drcontext, bb, instr, NULL,
                                         &address_reg) == DRREG_SUCCESS);
        DR_ASSERT(drreg_reserve_register(drcontext, bb, instr, NULL,
                                         &scratch_reg) == DRREG_SUCCESS);
        DR_ASSERT(drutil_insert_get_mem_addr(drcontext, bb, instr, divisor,
                                             address_reg, scratch_reg));
        dr_insert_write_raw_tls(drcontext, bb, instr, raw_tls_reg,
                                raw_tls_offs, address_reg);
        dr_insert_clean_call_ex(
            drcontext, bb, instr, (void *)record_pre_memory,
            DR_CLEANCALL_READS_APP_CONTEXT, 3, pc, op, size);
        DR_ASSERT(drreg_unreserve_register(drcontext, bb, instr, scratch_reg) ==
                  DRREG_SUCCESS);
        DR_ASSERT(drreg_unreserve_register(drcontext, bb, instr, address_reg) ==
                  DRREG_SUCCESS);
    }
    dr_insert_clean_call_ex(
        drcontext, bb, instr_get_next(instr), (void *)record_post,
        DR_CLEANCALL_READS_APP_CONTEXT, 0);
    return DR_EMIT_DEFAULT;
}

static void
event_thread_init(void *drcontext)
{
    thread_state_t *state =
        (thread_state_t *)dr_thread_alloc(drcontext, sizeof(*state));
    char path[MAXIMUM_PATH];
    DR_ASSERT(state != NULL);
    memset(state, 0, sizeof(*state));
    const int64_t pid = (int64_t)dr_get_process_id();
    const int64_t tid = (int64_t)dr_get_thread_id(drcontext);
    dr_snprintf(path, sizeof(path), "%s/div.%lld.%lld.bin", sidecar_dir,
                (long long)pid, (long long)tid);
    state->file = dr_open_file(path, DR_FILE_WRITE_OVERWRITE |
                                    DR_FILE_ALLOW_LARGE);
    DR_ASSERT(state->file != INVALID_FILE);
    const fastsim_div_sidecar_header_t header = {
        FASTSIM_DIV_SIDECAR_MAGIC,
        capture_id_hi, capture_id_lo, pid, tid,
        FASTSIM_DIV_SIDECAR_VERSION,
        sizeof(fastsim_div_sidecar_header_t),
        sizeof(fastsim_div_sidecar_record_t), 0,
    };
    DR_ASSERT(dr_write_file(state->file, &header, sizeof(header)) ==
              sizeof(header));
    DR_ASSERT(drmgr_set_tls_field(drcontext, tls_idx, state));
}

static void
event_thread_exit(void *drcontext)
{
    thread_state_t *state = state_for(drcontext);
    DR_ASSERT(state != NULL && state->pending_depth == 0);
    flush_output(state);
    dr_close_file(state->file);
    dr_thread_free(drcontext, state, sizeof(*state));
}

static void
event_exit(void)
{
    DR_ASSERT(drmgr_unregister_bb_insertion_event(instrument));
#ifdef UNIX
    DR_ASSERT(drmgr_unregister_signal_event(event_signal));
#endif
    DR_ASSERT(drmgr_unregister_thread_init_event(event_thread_init));
    DR_ASSERT(drmgr_unregister_thread_exit_event(event_thread_exit));
    DR_ASSERT(drmgr_unregister_tls_field(tls_idx));
    DR_ASSERT(dr_raw_tls_cfree(raw_tls_offs, 1));
    drreg_exit();
    drutil_exit();
    drmgr_exit();
}

DR_EXPORT void
dr_client_main(client_id_t id, int argc, const char *argv[])
{
    const char *trace_args[128];
    int trace_argc = 0;
    sidecar_dir[0] = '\0';
    capture_id_hi = capture_id_lo = 0;
    for (int i = 0; i < argc; ++i) {
        if (strcmp(argv[i], "-fastsim_div_sidecar_dir") == 0) {
            DR_ASSERT(++i < argc);
            dr_snprintf(sidecar_dir, sizeof(sidecar_dir), "%s", argv[i]);
        } else if (strcmp(argv[i], "-fastsim_div_capture_id_hi") == 0) {
            DR_ASSERT(++i < argc);
            capture_id_hi = (uint64_t)strtoull(argv[i], NULL, 0);
        } else if (strcmp(argv[i], "-fastsim_div_capture_id_lo") == 0) {
            DR_ASSERT(++i < argc);
            capture_id_lo = (uint64_t)strtoull(argv[i], NULL, 0);
        } else {
            DR_ASSERT(trace_argc < (int)(sizeof(trace_args) / sizeof(trace_args[0])));
            trace_args[trace_argc++] = argv[i];
        }
    }
    DR_ASSERT(sidecar_dir[0] != '\0' && dr_directory_exists(sidecar_dir));
    DR_ASSERT(capture_id_hi != 0 || capture_id_lo != 0);
    dynamorio::drmemtrace::drmemtrace_client_main(id, trace_argc, trace_args);
    DR_ASSERT(drmgr_init());
    DR_ASSERT(drutil_init());
    drreg_options_t reg_options = {sizeof(reg_options), 3, true, NULL, false};
    DR_ASSERT(drreg_init(&reg_options) == DRREG_SUCCESS);
    DR_ASSERT(dr_raw_tls_calloc(&raw_tls_reg, &raw_tls_offs, 1, 0));
    tls_idx = drmgr_register_tls_field();
    DR_ASSERT(tls_idx != -1);
    DR_ASSERT(drmgr_register_thread_init_event(event_thread_init));
    DR_ASSERT(drmgr_register_thread_exit_event(event_thread_exit));
#ifdef UNIX
    DR_ASSERT(drmgr_register_signal_event(event_signal));
#endif
    DR_ASSERT(drmgr_register_bb_instrumentation_event(NULL, instrument, NULL));
    dr_register_exit_event(event_exit);
}
