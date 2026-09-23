/*
 * TaoTrace: probe-based per-MACRO-op trace for the multi-core MVP (v2).
 *
 * v2 物理分流：
 *   - <name>.records.jsonl  TAO-Core 训练输入（µarch 无关，禁含任何 tick/cycle）
 *   - <name>.sched.jsonl    调度可观测信号（不进训练；按 anchor_seq_id 锚点）
 *   - <name>.labels.jsonl   µarch 相关训练标签（exposed/macro/branch_pen cycles）
 *   - <name>.diag.jsonl     诊断（commit_tick 等，不进任何 pipeline）
 *
 * 调度事件类型（对齐 single_core_mvp/doc/01_dataset_io_spec.md §2）：
 *   SCHED_IN / SCHED_OUT / THREAD_CREATE / THREAD_EXIT / YIELD
 *
 * SharedAttr 真值：在 onDataAccessComplete 中按 packet 的 cacheResponding /
 * hasSharers / isWriteback / isInvalidate 推导，并维护 attr_cache_ 状态机
 * （MESI proxy）。仅在 macro 内首次 mem-touching micro 处赋值，写入 acc.shared_attr。
 */
#ifndef __CPU_O3_PROBE_TAO_TRACE_HH__
#define __CPU_O3_PROBE_TAO_TRACE_HH__

#include <array>
#include <cstdint>
#include <cstdio>
#include <list>
#include <map>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "cpu/o3/dyn_inst_ptr.hh"
#include "cpu/o3/kernel_event.hh"
#include "lru_banked.hh"
#include "mem/packet.hh"
#include "mem/taotrace_response.hh"
#include "params/TaoTrace.hh"
#include "sim/probe/probe_listener_object.hh"
#include "uarch_profile.hh"

// uarch_profile.hh / lru_banked.hh 是 oracle 与 ref_sim 的同源；SConscript
// 已经把 single_core_mvp/shared 加入 CPPPATH。

namespace gem5
{
class ThreadContext;

namespace o3
{

class CPU;

class TaoTrace : public ProbeListenerObject
{
  public:
    TaoTrace(const TaoTraceParams &params);
    ~TaoTrace() override;

    void regProbeListeners() override;

    std::string name() const override
    {
        return ProbeListenerObject::name() + ".tao_trace";
    }

  private:
    enum class InstrType : uint8_t {
        INT = 0, FP, LD, ST, ATOMIC, FENCE, BR, SYS, OTHER
    };
    enum class MemOp : uint8_t {
        NONE = 0, LOAD, STORE, ATOMIC, FENCE
    };
    enum class SyncType : uint8_t {
        NONE = 0, LOCK_ACQ, LOCK_REL, BARRIER,
        FUTEX_WAIT, FUTEX_WAKE, YIELD, LOCK_ACQ_PROXY
    };
    enum class CoherenceAction : uint8_t {
        // V2 三层 cache：把原 LOCAL_HIT(=L1) 拆出 L2_HIT，
        //   并保留 LLC_HIT/DRAM/REMOTE_HIT_*/WB_REQUIRED 表示远端协议事件。
        //   编号保持向后兼容：1=L1_HIT(原 LOCAL_HIT)，4=LLC_HIT，5=DRAM，
        //   6=WB_REQUIRED；新增 7=L2_HIT。
        UNKNOWN = 0, L1_HIT = 1, REMOTE_HIT_CLEAN = 2, REMOTE_HIT_DIRTY = 3,
        LLC_HIT = 4, DRAM = 5, WB_REQUIRED = 6, L2_HIT = 7
    };
    // 调度事件（写入 sched.jsonl，全部以 anchor_seq_id 锚点，不写 tick）
    enum class SchedEvent : uint8_t {
        SCHED_IN = 0, SCHED_OUT, THREAD_CREATE, THREAD_EXIT, YIELD_EVT
    };

    struct SharedAttr {
        uint8_t mesi_before = 0;
        CoherenceAction coh = CoherenceAction::UNKNOWN;
        uint8_t owner_distance_class = 0;   // 0=SELF/1=SAME_TILE/2=NEAR/3=FAR
        uint8_t sharer_count_bucket = 0;    // 0/1/2/3-7/8+
        bool dirty_owner = false;
        uint8_t path_class = 0;             // 0=L1/1=L2/2=LLC/3=NoC/4=DRAM
        uint8_t inval_fanout_bucket = 0;    // 0/1/2-3/4-7/8+
        uint8_t same_line_recent_bucket = 0;// 0/1/2/3+
        bool valid = false;
        // ============================ P0-A 新增字段 ============================
        // 与 mesi_ref_sim/include/simulator.hpp::DSideOracle 同字段同口径，
        // 确保 oracle ↔ ref_sim 在相同 UarchProfile 下 bit-exact。
        uint8_t d_mshr_depth        = 0;    // l1d_mshr_[c].size() clip 0..15
        uint8_t dtlb_hit            = 0;    // dtlb_[c].translate() 命中 0/1
        uint8_t d_walker_levels     = 0;    // PageWalkSim levels clip 0..7
        uint8_t d_walker_dram_misses = 0;   // walker miss_dram clip 0..7
        uint8_t d_bank_id           = 0;    // l1d_lru_[c].bankIdOf clip 0..15
        // ============================ V10.3 A 字段 ============================
        // 与 mesi_ref_sim DSideOracle::d_llc_set_residency / d_llc_set_lru_pos
        // 同字段同口径：在 LRU touch 之前 peek L3 set 状态，bit-exact。
        uint8_t d_llc_set_residency = 0;    // l3_lru_.peekSetState clip 0..31
        uint8_t d_llc_set_lru_pos   = 0;    // l3_lru_.peekSetState clip 0..31
        // Oracle-only native Ruby response facts. They are written only to a
        // diagnostic sideband and are never serialized into FST.
        uint32_t native_hierarchy_request_count = 0;
        uint32_t native_response_count = 0;
        uint32_t native_external_hits = 0;
        uint32_t native_external_misses = 0;
        uint32_t native_coalesced = 0;
        uint64_t native_responder_machine_mask = 0;
        uint32_t native_responder_machine_unknown = 0;
        bool native_has_admission_tick = false;
        uint64_t native_first_admission_tick = 0;
        uint64_t native_last_admission_tick = 0;
        bool native_has_response_tick = false;
        uint64_t native_last_response_tick = 0;
        TaoTraceNativeHierarchyFacts native_hierarchy;
    };

    // i-side（取指）共享属性，写入 records.micro 的 i_* 字段。
    // 与 SharedAttr 解耦：i-side 走独立的 l1i/itlb 视图，仅暴露 4 个字段。
    struct InstSharedAttr {
        uint8_t path_class = 0;        // 0=L1I/1=L2/2=LLC/3=NoC/4=DRAM
        CoherenceAction coh = CoherenceAction::UNKNOWN;
        uint8_t mesi_before = 0;
        uint8_t oracle_source = 1;     // 0=packet 1=fallback/none
        bool valid = false;
        // ============================ P0-A 新增字段 ============================
        // 与 mesi_ref_sim/include/simulator.hpp::IFetchResult 同字段同口径。
        uint8_t i_mshr_depth        = 0;    // l1i_mshr_[c].size() clip 0..15
        uint8_t itlb_hit            = 0;    // itlb_[c].translate() 命中 0/1
        uint8_t i_walker_levels     = 0;    // i_walker_ levels clip 0..7
        uint8_t i_walker_dram_misses = 0;   // i_walker_ miss_dram clip 0..7
        uint8_t i_bank_id           = 0;    // l1i_lru_[c].bankIdOf clip 0..15
        // ============================ V10.3 A 字段 ============================
        // 与 mesi_ref_sim IFetchResult::i_llc_set_residency / i_llc_set_lru_pos
        // 同字段同口径：在 LRU touch 之前 peek L3-i set 状态，bit-exact。
        uint8_t i_llc_set_residency = 0;    // l3_i_lru_.peekSetState clip 0..31
        uint8_t i_llc_set_lru_pos   = 0;    // l3_i_lru_.peekSetState clip 0..31
    };

    // 每个 cacheline 的状态机（probe 内部 MESI proxy）
    struct LineState {
        uint8_t mesi = 0;                 // I=0/S=1/E=2/M=3
        int32_t owner_core = -1;          // 最近写者
        std::unordered_set<uint32_t> sharers;  // 最近读者集合
    };

    // Macro-op 累积器：micro-op 提交期间逐步填充
    struct MacroAccum {
        bool     valid = false;
        uint32_t core_id = 0;
        uint32_t thread_id = 0;
        uint64_t first_seq = 0;
        uint64_t macro_pc = 0;
        int      op_class = 0;
        bool     any_load = false;
        bool     any_store = false;
        bool     any_locked_rmw = false;
        bool     any_fence = false;
        bool     macro_legacy_lock = false;
        bool     is_branch = false;
        bool     is_syscall = false;
        bool     mem_filled = false;
        uint64_t vaddr = 0;
        uint64_t paddr = 0;
        uint16_t access_size = 0;
        std::string macro_disasm_lower;
        uint64_t first_fetch_tick = 0;
        uint64_t last_commit_tick = 0;
        // v2 新增：mem-touching micro 在 onDataAccessComplete 时回填的 SharedAttr
        SharedAttr shared_attr;
        // V1 multi-core 新增功能侧字段
        uint64_t reg_read_bitmap = 0;
        uint64_t reg_write_bitmap = 0;
        uint8_t  access_distance_bucket = 0;
        // Predictor-independent committed control-flow facts. History is the
        // actual-direction history before this branch retires.
        bool     branch_taken = false;
        uint64_t branch_target = 0;
        uint64_t branch_next_pc = 0;
        uint16_t branch_history_before = 0;
        // pipeline-stage tick 缓存（来自 last micro），仅用于 labels.jsonl 拆分
        int64_t  last_issue_tick_delta = -1;
        int64_t  last_complete_tick_delta = -1;
        bool     last_mispredicted = false;
    };

    // ---- Listener entry points ----
    void onFetch(const DynInstPtr &dynInst);
    void onRename(const DynInstPtr &dynInst);
    void onDispatch(const DynInstPtr &dynInst);
    void onCommit(const DynInstPtr &dynInst);
    void onPreCommit(const DynInstPtr &dynInst);
    void onCommitStall(const DynInstPtr &dynInst);
    void onSquash(const DynInstPtr &dynInst);
    void onExecute(const DynInstPtr &dynInst);
    void onToCommit(const DynInstPtr &dynInst);
    void onDataAccessComplete(const std::pair<DynInstPtr, PacketPtr> &p);
    void onKernelEntry(const KernelEntryEvent &event);
    // V9.5 i-cache probe：通过 cpu->ppInstAccessComplete 钩子收 fetch
    //   的 cache packet，按 (l1i_lru_, l2_lru_, l3_lru_) 推断 i-side 命中层级。
    //   如果 fetch.cc 不发该事件（如 atomic 模式），注册仍然安全（无 callback）。
    void onInstAccessComplete(const PacketPtr &pkt);

    // ---- Helpers ----
    bool isLockedAtomicMicro(const DynInstPtr &inst) const;
    bool isMacroopLocked(const DynInstPtr &inst) const;
    bool isSyscallInst(const DynInstPtr &inst) const;
    bool isSyscallMacro(const DynInstPtr &inst);
    bool isSyscallBoundary(const DynInstPtr &inst);
    bool isInterruptReturn(const DynInstPtr &inst) const;
    bool isIdleInstruction(const DynInstPtr &inst) const;
    bool isPollIdleInstruction(const DynInstPtr &inst) const;
    bool isUserInstruction(const DynInstPtr &inst) const;
    uint8_t instructionCpl(const DynInstPtr &inst) const;
    bool pteAddressSpaceMatches(const DynInstPtr &inst) const;
    void maybeCaptureInitialPteState(const DynInstPtr &inst);
    static void captureMeasurementPteState();
    uint32_t getTraceThreadId(const DynInstPtr &inst) const;
    uint32_t getCoreId(const DynInstPtr &inst) const;
    void captureSyscallState(const DynInstPtr &inst);
    void emitSyscallRecord(const DynInstPtr &inst);

    // Macro-op 流程
    void accumulateMicro(const DynInstPtr &inst);
    void flushMacro(MacroAccum &acc, const DynInstPtr &lastInst);
    SyncType classifySyncFromMacro(const MacroAccum &acc) const;
    SyncType classifySyncFromSyscall(const DynInstPtr &inst,
                                     uint64_t nr, uint64_t op,
                                     uint64_t addr, uint64_t val);

    // 通用工具
    InstrType deriveInstrType(const MacroAccum &acc) const;
    MemOp    deriveMemOp(const MacroAccum &acc) const;
    uint64_t ticksToCycles(uint64_t ticks, const DynInstPtr &inst) const;

    // v2 真值 SharedAttr 推导（基于 packet flags + line state 机）
    SharedAttr deriveSharedAttr(const PacketPtr pkt, uint32_t core_id,
                                bool is_store);
    // Fix A2: store 在 commit 之后才会触发 DataAccessComplete，
    //   故 accumulateMicro 时 pending 表大概率还没到；用纯 line_states_ 推断作 fallback。
    SharedAttr deriveSharedAttrFromLineState(uint64_t vaddr,
                                             uint32_t core_id,
                                             bool is_store);
    static uint8_t bucketCount(size_t n);

    // 输出（v2 分流）
    void writeRecordsLine(const MacroAccum &acc, InstrType it, MemOp mo,
                          uint64_t branch_target, SyncType st,
                          uint16_t branch_history,
                          uint8_t access_distance_bucket);
    void writeRecordsSyscallLine(uint32_t core_id, uint32_t thread_id,
                                 uint64_t seq_id, uint64_t pc, int op_class,
                                 SyncType st);
    void writeLabelsLine(uint32_t core_id, uint32_t thread_id, uint64_t seq_id,
                         uint64_t exposed_cyc, uint64_t macro_cyc,
                         uint64_t branch_pen_cyc,
                         uint64_t fetch_lat_cyc, uint64_t exec_lat_cyc,
                         uint8_t branch_mispred);
    void writeDiagLine(uint32_t core_id, uint32_t thread_id, uint64_t seq_id,
                       uint64_t fetch_tick, uint64_t commit_tick,
                       uint64_t prev_commit_tick, uint64_t last_squash_tick);
    void writeSchedEvent(SchedEvent ev, uint32_t core_id, uint32_t thread_id,
                         uint64_t anchor_seq_id, uint32_t by_thread_id,
                         const char *reason);
    // V3 reference simulator 验证：commit 时每条 mem-event 输出一行
    void writeMemEventLine(const DynInstPtr &inst, const PacketPtr pkt,
                           uint32_t core_id, uint32_t thread_id,
                           uint64_t cacheline_addr, bool is_store,
                           uint16_t size, uint64_t pc,
                           const SharedAttr &oracle);

    // V9 micro 粒度：每条 commit 一行的 records.micro / labels.micro
    void emitMicroRecord(const DynInstPtr &inst,
                         const SharedAttr &oracle,
                         uint64_t vaddr, uint64_t paddr, uint16_t size,
                         bool oracle_filled,
                         const InstSharedAttr &i_oracle,
                         bool branch_taken,
                         uint64_t branch_target,
                         uint64_t branch_next_pc,
                         uint16_t branch_history);
    static inline uint32_t encodeReg(uint8_t cls, uint32_t idx)
    {
        return (uint32_t(cls) << 24) | (idx & 0x00FFFFFFu);
    }

public:
    // V4 静默状态更新事件（eviction / prefetch fill / coherence inval）
    //   由 Ruby 侧静态钩子调用；不属于任何 commit micro，但会改变 cache 视图，
    //   ref_simulator 必须看到才能保持 0-diff。
    //   写入 process-global mem_events.jsonl（来自任意 core），
    //   也会回写 line_states_ / l*_lru_。
    static void traceCacheEvent(const char *event_type,
                                uint32_t core_id, uint64_t cacheline_addr,
                                int cache_level /*0=L1D,1=L2,2=LLC*/);

    // V9.6 ROI 闸门：always-update + ROI-only-emit 范式。
    //   m5_work_begin / m5_work_end pseudo-instruction 在 sim/pseudo_inst.cc 中
    //   通过 install.sh 注入的 hook 调用本函数。ROI 关闭时 probe 内部状态机
    //   （line_states_/LRU/TLB/walker/MSHR/branch_history/last_writer/MacroAccum）
    //   照常更新，但 emitMicroRecord / writeRecordsLine / writeLabelsLine /
    //   writeMemEventLine / writeRecordsSyscallLine / writeSchedEvent /
    //   writeDiagLine 全部短路返回。从而：
    //     (a) ROI 第一条 µop 看到的微架构 / probe 视图均已预热，无冷启动；
    //     (b) ROI 外的启动期 / syscall / scheduler µop 不进入 records.micro。
    //   ROI 状态进程级共享（每核一个 TaoTrace 实例 → 必须共享）。
    //   require_roi_=true 时方启用闸门；require_roi_=false 时退化为
    //   "全程 emit"（默认，向后兼容 V9.5 行为）。
    static void traceSourceRoiCheckpoint(gem5::ThreadContext *tc);
    static void traceWorkBegin(uint32_t core_id, uint64_t workid,
                               uint64_t threadid);
    static void traceWorkEnd  (uint32_t core_id, uint64_t workid,
                               uint64_t threadid);
    static void traceMeasurementBegin();
    static void traceMeasurementEnd();
    // Offline-only exact squash boundary. Commit calls this when it accepts
    // an IEW redirect, before ROB::squash mutates the younger instructions.
    // The resulting sidecar is an attribution oracle and is never consumed
    // by the FST inference path.
    static void traceSquashEpisode(const CPU *cpu,
                                   uint32_t hardware_thread_id,
                                   bool branch_mispredict,
                                   bool include_cause,
                                   uint64_t cause_seq,
                                   uint64_t cutoff_seq,
                                   uint64_t rob_youngest_seq,
                                   uint64_t cause_pc,
                                   uint64_t redirect_pc);
    // emit 闸门：emit*/write* 函数入口统一调用，集中管理短路逻辑。
    static inline bool emitGateOpen(uint32_t core_id)
    {
        // require_roi_ 关 -> 始终允许 emit（V9.5 行为）
        // require_roi_ 开 -> 仅该 core 自己的 ROI 处于 active 时允许 emit。
        if (!require_roi_) return true;
        auto it = roi_active_count_per_core_.find(core_id);
        return it != roi_active_count_per_core_.end() && it->second > 0;
    }

private:

    // 调度事件触发器（按 syscall NR / commit thread 切换）
    void maybeEmitSchedSwitch(uint32_t core_id, uint32_t thread_id,
                              uint64_t anchor_seq_id);
    void maybeEmitThreadCreate(uint32_t core_id, uint32_t thread_id,
                               uint64_t anchor_seq_id);

    void openOutput();
    void accountCplCommit(const DynInstPtr &inst, bool is_syscall);
    bool cplMeasurementActive() const;
    uint64_t cplMeasurementBeginTick() const;
    uint64_t cplMeasurementEndTick() const;
    void noteCplSyscallEntry(const DynInstPtr &inst);
    void initializeCpl(uint64_t tick, uint64_t clock_period_ticks,
                       bool initial_user);
    void settleCpl(uint64_t tick);
    void finalizeCplMeasurement();
    void emitFunctionalSyscallMarker(
        const DynInstPtr &inst, uint64_t syscall_number);
    void maybeCompleteFunctionalSyscall(const DynInstPtr &inst);
    void finalizeFst();
    bool functionalCaptureActive() const;
    bool functionalTargetReached() const;
    void beginFunctionalMeasurement();
    bool functionalTraceEnabled() const;
    void noteFunctionalRecordEmitted(bool completes_instruction,
                                     bool is_user);
    void writeFunctionalBoundary();

    // ---- State ----
    std::FILE *out_records_ = nullptr;
    std::FILE *out_sched_   = nullptr;
    std::FILE *out_labels_  = nullptr;
    std::FILE *out_diag_    = nullptr;
    std::FILE *out_native_response_diag_ = nullptr;
    std::string native_response_summary_path_;
    // V3 reference simulator 验证：每条 commit 的 mem-event 单独输出一行，
    //   包含 simulator 输入 (core/tid/cl/is_store/size) + Ruby oracle 标签
    //   (coh_oracle) + packet 调试信号。逐条 0-diff 验收。
    std::FILE *out_mem_events_ = nullptr;
    uint64_t   mem_event_counter_ = 0;
    // V4：所有 TaoTrace 实例共享一个 mem_events sink（含 evict/prefetch
    //   等静默状态更新事件，必须按全 process 全序输出）。
    static std::FILE  *global_mem_events_;
    static uint64_t    global_mem_event_counter_;

    // V9 micro 粒度新增输出：
    //   records.micro.jsonl 每条 commit 一行（与 atomic_func_trace 对齐）
    //   labels.micro.jsonl  每条 commit 一行（per-micro 延迟标签）
    std::FILE *out_records_micro_ = nullptr;
    std::FILE *out_labels_micro_  = nullptr;

    struct WrongPathPending {
        uint32_t core_id = 0;
        uint32_t hardware_thread_id = 0;
        uint32_t context_id = 0;
        uint64_t seq_num = 0;
        uint64_t macro_pc = 0;
        uint32_t micro_pc = 0;
        int32_t op_class = 0;
        uint8_t cpl = 0xff;
        uint16_t n_src = 0;
        uint16_t n_dst = 0;
        uint64_t fetch_tick = 0;
        uint64_t rename_tick = 0;
        uint64_t dispatch_tick = 0;
        uint64_t execute_tick = 0;
        uint64_t to_commit_tick = 0;
        uint64_t data_complete_tick = 0;
        uint64_t dyn_issue_tick = 0;
        uint64_t dyn_complete_tick = 0;
        uint64_t vaddr = 0;
        uint64_t paddr = 0;
        uint16_t size = 0;
        bool identity_valid = false;
        bool is_microop = false;
        bool is_last_microop = false;
        bool is_load = false;
        bool is_store = false;
        bool is_atomic = false;
        bool is_control = false;
        bool is_conditional = false;
        bool is_indirect = false;
        bool is_call = false;
        bool is_return = false;
        bool fetched = false;
        bool renamed = false;
        bool dispatched = false;
        bool execute_seen = false;
        bool to_commit_seen = false;
        bool data_complete_seen = false;
    };
    struct WrongPathLateMemory {
        uint64_t episode_id = 0;
        uint32_t core_id = 0;
        uint32_t hardware_thread_id = 0;
        uint64_t seq_num = 0;
        uint64_t squash_tick = 0;
        uint8_t cpl = 0xff;
        bool is_load = false;
        bool is_store = false;
        bool is_atomic = false;
    };
    void noteWrongPathStage(const DynInstPtr &inst, const char *stage);
    void noteWrongPathDataComplete(const DynInstPtr &inst);
    void eraseWrongPathCommitted(const DynInstPtr &inst);
    bool wrongPathMeasurementGateOpen() const;
    void emitWrongPathEpisode(uint32_t hardware_thread_id,
                              const char *cause,
                              bool include_cause,
                              uint64_t cause_seq,
                              uint64_t cutoff_seq,
                              uint64_t rob_youngest_seq,
                              uint64_t cause_pc,
                              uint64_t redirect_pc);
    void emitWrongPathFallback(const DynInstPtr &inst);
    void writeWrongPathInstruction(uint64_t episode_id,
                                   const char *cause,
                                   uint64_t squash_tick,
                                   const WrongPathPending &pending);
    void writeWrongPathLateDataComplete(
        const WrongPathLateMemory &late, const DynInstPtr &inst);
    std::map<uint64_t, WrongPathPending> wrong_path_pending_;
    std::map<uint64_t, uint32_t> wrong_path_finalized_;
    std::map<uint64_t, WrongPathLateMemory> wrong_path_late_memory_;
    int32_t wrong_path_core_id_ = -1;
    static std::FILE *global_wrong_path_oracle_;
    static bool wrong_path_header_written_;
    static uint64_t global_wrong_path_episode_id_;
    static std::unordered_map<const CPU *, TaoTrace *> wrong_path_instances_;

    // 记录流物理格式（SimObject 参数 trace_format）：
    //   JSONL = 逐条 JSON 文本（默认）；FST = 直接写 FastSim FST schema-7 二进制。
    // FST 模式下 records.micro 走 out_fst_ 二进制 writer，且不产出 labels.micro。
    enum class TraceFormat : uint8_t { JSONL = 0, FST = 1 };
    TraceFormat trace_format_ = TraceFormat::JSONL;
    // FST 直写状态（仅 trace_format_==FST 时使用）：
    std::FILE *out_fst_ = nullptr;
    std::string fst_path_;
    int32_t     fst_core_id_ = -1;      // header.core_id；首条 emit 时锁定
    uint64_t    fst_record_count_ = 0;  // header.record_count
    uint64_t    fst_feature_flags_ = 0; // header.feature_flags（累积）
    bool        fst_finalized_ = false;
    // virtual-page token interning: the CR3 root is part of the identity, so
    // identical virtual/physical pages in two processes never alias.
    std::map<std::tuple<uint64_t, uint64_t, uint64_t>, uint32_t>
        fst_page_tokens_;
    uint32_t    fst_next_page_token_ = 1;
    struct FstVirtualPageMapping {
        uint32_t token = 0;
        uint32_t flags = 0;
        uint64_t first_record_ordinal = 0;
        uint64_t address_space_id = 0;
        uint64_t virtual_page = 0;
        uint64_t physical_page = 0;
    };
    std::vector<FstVirtualPageMapping> fst_virtual_page_mappings_;
    struct FstAddressSpaceTransition {
        uint64_t record_ordinal = 0;
        uint64_t address_space_id = 0;
    };
    std::vector<FstAddressSpaceTransition>
        fst_address_space_transitions_;
    // Dynamically observed, producer-neutral static instruction facts. The
    // resulting .imap v2 is deliberately partial: committed PCs have complete
    // architectural rename-dependency masks, but unobserved executable PCs
    // are not inferred.
    struct FstStaticInstruction {
        uint64_t pc = 0;
        uint64_t fallthrough_pc = 0;
        uint64_t direct_target = 0;
        uint16_t flags = 0;
        uint8_t size = 0;
        std::array<uint64_t, 2> read_register_mask{};
        std::array<uint64_t, 2> write_register_mask{};
    };
    struct FstStaticInstructionPending : FstStaticInstruction {
        std::array<uint64_t, 2> written_register_mask{};
        bool valid = false;
    };
    std::map<uint64_t, FstStaticInstruction> fst_static_instructions_;
    std::unordered_set<uint64_t> fst_static_unsupported_pcs_;
    std::unordered_map<uint32_t, FstStaticInstructionPending>
        fst_static_pending_;
    // 把当前 records.micro 头部（record_count / feature_flags）刷回文件起始。
    //   open 后（count=0）与析构 close 前（终值）各调一次。
    void writeFstHeader();
    void writeFstVirtualPageMap();
    void noteFstAddressSpace(uint64_t address_space_id);
    void writeFstAddressSpaceMap();
    void observeFstStaticInstruction(const DynInstPtr &inst,
                                     bool completes_instruction);
    void writeFstInstructionMap();

    struct FunctionalSyscallMetadata {
        uint64_t record_ordinal = 0;
        uint64_t syscall_ordinal = 0;
        uint64_t thread_id = 0;
        uint64_t number = 0;
        std::array<uint64_t, 6> arguments{};
        uint64_t return_value_raw = 0;
        uint64_t pre_timestamp_us = 0;
        uint64_t post_timestamp_us = 0;
        uint32_t errno_value = 0;
        uint32_t pre_cpu = 0;
        uint32_t post_cpu = 0;
        uint16_t valid_fields = 0;
        uint8_t argument_count = 0;
        uint8_t flags = 0;
    };
    struct FunctionalReturnKey {
        uint64_t address_space = 0;
        uint64_t user_rsp = 0;
        uint64_t return_pc = 0;
        bool operator<(const FunctionalReturnKey &other) const {
            if (address_space != other.address_space)
                return address_space < other.address_space;
            if (user_rsp != other.user_rsp) return user_rsp < other.user_rsp;
            return return_pc < other.return_pc;
        }
    };
    struct PendingFunctionalReturn {
        TaoTrace *owner = nullptr;
        size_t metadata_index = 0;
    };
    std::vector<FunctionalSyscallMetadata> fst_syscall_metadata_;
    static std::multimap<FunctionalReturnKey, PendingFunctionalReturn>
        pending_functional_returns_;
    uint64_t fst_completed_syscall_returns_ = 0;

    bool emit_macro_ = false;
    bool emit_micro_ = true;
    // V9.5：cache 事件流（mem_events.jsonl）独立开关，与指令粒度正交。
    bool emit_mem_events_ = true;
    bool measure_cpl_ = false;
    bool emit_native_response_jsonl_ = false;
    uint64_t native_response_anomaly_limit_ = 32;
    bool emit_wrong_path_oracle_ = false;
    bool functional_user_only_ = false;
    bool functional_include_kernel_ = false;
    bool functional_warmup_ = false;
    uint64_t functional_user_target_ = 0;
    uint64_t functional_record_count_ = 0;
    uint64_t functional_instruction_count_ = 0;
    uint64_t functional_warmup_record_count_ = 0;
    uint64_t functional_warmup_instruction_count_ = 0;
    uint64_t functional_measurement_record_count_ = 0;
    uint64_t functional_measurement_instruction_count_ = 0;
    uint64_t functional_user_record_count_ = 0;
    uint64_t functional_user_instruction_count_ = 0;
    uint64_t functional_warmup_user_record_count_ = 0;
    uint64_t functional_warmup_user_instruction_count_ = 0;
    uint64_t functional_measurement_user_record_count_ = 0;
    uint64_t functional_measurement_user_instruction_count_ = 0;
    bool functional_measurement_started_ = false;
    bool initial_pte_root_checked_ = false;
    bool initial_pte_root_mismatch_warned_ = false;
    gem5::ThreadContext *pte_snapshot_tc_ = nullptr;
    // A precise user #PF can enter on another core before the process-wide
    // serial marker and retire its retried instruction after that marker.
    // Track that boundary condition directly so FST never has to infer it
    // from record position or adjacency.
    bool premeasurement_page_fault_pending_ = false;
    uint64_t premeasurement_page_fault_virtual_page_ = 0;
    std::unordered_set<uint64_t>
        measurement_boundary_inflight_fault_pages_;
    // V9.6 ROI 模式：true 时启用 always-update + ROI-only-emit 闸门；
    //   false 时退化为 V9.5 全程 emit 行为。SimObject 参数 require_roi。
    //   首个 TaoTrace 实例构造时把它写入静态 require_roi_，进程级共享。
    bool require_roi_param_ = false;
    std::map<uint64_t, uint8_t> syscall_arg_counts_;

    enum class CplCycleClass : uint8_t {
        USER = 0,
        SYSCALL = 1,
        PAGE_FAULT = 2,
        IRQ = 3,
        SCHEDULER = 4,
        IDLE = 5,
        UNKNOWN_KERNEL = 6,
        COUNT = 7,
    };
    static constexpr size_t CplCycleClassCount =
        static_cast<size_t>(CplCycleClass::COUNT);
    struct PendingNativeResponseDiag {
        uint32_t thread_id = 0;
        uint64_t inst_seq_num = 0;
        CplCycleClass cycle_class = CplCycleClass::UNKNOWN_KERNEL;
        uint8_t proxy_path_class = 0;
        uint64_t line_requests = 0;
        bool fallback_source = false;
        std::string inst_name;
        uint64_t pc = 0;
        uint64_t eff_addr = 0;
        uint64_t phys_addr = 0;
        uint64_t mem_req_flags = 0;
        uint32_t eff_size = 0;
        bool is_load = false;
        bool is_store = false;
        bool is_atomic = false;
        bool is_data_prefetch = false;
        bool has_request = false;
        bool read_predicate = false;
        bool mem_access_predicate = false;
        bool strictly_ordered = false;
        bool translation_started = false;
        bool translation_completed = false;
        bool executed = false;
        bool completed = false;
    };
    struct NativeHierarchyAggregate {
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels> accesses{};
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels> hits{};
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels> tag_misses{};
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels>
            permission_upgrades{};
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels>
            merged_misses{};
        std::array<uint64_t, TaoTraceNativeHierarchyFacts::Levels>
            remote_supplies{};
        uint64_t unique_fills = 0;
        uint64_t ruby_memory_fetches = 0;
        uint64_t memory_read_transactions = 0;

        void merge(const TaoTraceNativeHierarchyFacts &facts);
        void merge(const NativeHierarchyAggregate &other);
    };
    struct NativeScopeAggregate {
        uint64_t memory_uops = 0;
        uint64_t architectural_line_requests = 0;
        uint64_t ruby_admission_fragments = 0;
        uint64_t ruby_aliased_admission_fragments = 0;
        uint64_t ruby_hierarchy_request_fragments = 0;
        uint64_t ruby_response_fragments = 0;
        uint64_t sequencer_coalesced_fragments = 0;
        uint64_t no_ruby_uops = 0;
        uint64_t native_outcome_uops = 0;
        uint64_t native_external_hit_fragments = 0;
        uint64_t native_external_miss_fragments = 0;
        uint64_t native_responder_machine_unknown = 0;
        uint64_t hierarchy_complete_uops = 0;
        uint64_t hierarchy_incomplete_uops = 0;
        NativeHierarchyAggregate hierarchy;

        void merge(const NativeScopeAggregate &other);
    };
    struct NativeAnomalySample {
        std::string kind;
        std::string inst_name;
        uint32_t thread_id = 0;
        uint64_t inst_seq_num = 0;
        uint64_t pc = 0;
        uint64_t eff_addr = 0;
        uint64_t phys_addr = 0;
        uint64_t mem_req_flags = 0;
        uint32_t eff_size = 0;
        bool is_load = false;
        bool is_store = false;
        bool is_atomic = false;
        bool is_data_prefetch = false;
        CplCycleClass cycle_class = CplCycleClass::UNKNOWN_KERNEL;
        uint8_t proxy_path_class = 0;
        uint64_t line_requests = 0;
        uint32_t admissions = 0;
        uint32_t aliased_admissions = 0;
        uint32_t hierarchy_requests = 0;
        uint32_t responses = 0;
        uint32_t external_hits = 0;
        uint32_t external_misses = 0;
        uint32_t l1d_accesses = 0;
    };
    void writeNativeResponseCommit(
        const DynInstPtr &inst, const SharedAttr &attr,
        CplCycleClass cycle_class, uint64_t line_requests,
        bool fallback_source);
    void writeNativeResponseLate(
        const DynInstPtr &inst, const SharedAttr &attr);
    void refreshNativeResponsePending();
    bool nativeResponseDrainComplete();
    void logNativeResponsePending(uint64_t poll);
    static void pollFunctionalTargetDrain();
    static void scheduleFunctionalTargetDrain(TaoTrace *owner);
    void finalizeNativeResponseDiagnostic();
    static const char *nativeDiagScopeName(CplCycleClass cycle_class);
    void accountNativeResolved(
        const PendingNativeResponseDiag &pending,
        const TaoTraceNativeAccessRegistry::Snapshot &native);
    void recordNativeAnomaly(
        const char *kind, const PendingNativeResponseDiag &pending,
        const TaoTraceNativeAccessRegistry::Snapshot &native);
    struct PmuScope {
        uint64_t retired_instructions = 0;
        uint64_t retired_uops = 0;
        uint64_t memory_uops = 0;
        uint64_t line_requests = 0;
        uint64_t branches = 0;
        uint64_t branch_misses = 0;
        uint64_t l1d_accesses = 0;
        uint64_t l1d_hits = 0;
        uint64_t l1d_misses = 0;
        uint64_t l2_accesses = 0;
        uint64_t l2_hits = 0;
        uint64_t l2_misses = 0;
        uint64_t llc_accesses = 0;
        uint64_t llc_hits = 0;
        uint64_t llc_misses = 0;
        uint64_t permission_upgrades = 0;
        uint64_t remote_supplies = 0;
        // A path class cannot identify Ruby TBE merges, unique fills, or
        // MemCtrl transactions. Keep them distinct and zero until those
        // native probe points are wired instead of aliasing LLC tag misses.
        uint64_t llc_merged_misses = 0;
        uint64_t llc_unique_fills = 0;
        uint64_t dram_reads = 0;
        uint64_t dram_writes = 0;
        uint64_t dtlb_accesses = 0;
        uint64_t dtlb_hits = 0;
        uint64_t dtlb_misses = 0;
    };
    enum class DtlbOutcome : uint8_t { MISS = 0, HIT = 1, UNKNOWN = 2 };
    void accountCplDataPmu(
        const SharedAttr &attr, CplCycleClass data_class,
        uint64_t syscall_number, uint64_t line_requests,
        DtlbOutcome dtlb_outcome);
    struct CplDataAttribution {
        CplCycleClass cycle_class = CplCycleClass::UNKNOWN_KERNEL;
        uint64_t syscall_number = 0;
        uint64_t line_requests = 0;
        DtlbOutcome dtlb_outcome = DtlbOutcome::UNKNOWN;
    };
    bool cpl_initialized_ = false;
    bool cpl_finalized_ = false;
    bool cpl_syscall_pending_ = false;
    CplCycleClass cpl_cycle_class_ = CplCycleClass::USER;
    std::vector<CplCycleClass> cpl_parent_stack_;
    int32_t cpl_core_id_ = -1;
    uint64_t cpl_first_tick_ = 0;
    uint64_t cpl_last_tick_ = 0;
    uint64_t cpl_clock_period_ticks_ = 1;
    std::array<uint64_t, CplCycleClassCount> cpl_ticks_{};
    std::array<uint64_t, CplCycleClassCount> cpl_commits_{};
    std::array<uint64_t, CplCycleClassCount> cpl_entries_{};
    uint64_t cpl_user_commits_ = 0;
    uint64_t cpl_user_functional_uops_ = 0;
    uint64_t cpl_syscall_boundaries_ = 0;
    uint64_t cpl_pending_sysnum_ = 0;
    uint64_t cpl_active_sysnum_ = 0;
    uint64_t cpl_pending_syscall_pc_ = 0;
    uint32_t cpl_pending_syscall_thread_id_ = 0;
    // ``idle=poll`` is decoded by gem5 as REP-prefixed NOP (F3 90), not as
    // an instruction named PAUSE.  Require a tightly repeated PAUSE site
    // before opening a persistent poll-idle interval so that isolated
    // cpu_relax() calls in active kernel code remain active-kernel work.
    bool cpl_poll_idle_ = false;
    uint64_t cpl_poll_candidate_pc_ = 0;
    uint32_t cpl_poll_candidate_pauses_ = 0;
    uint32_t cpl_poll_candidate_gap_commits_ = 0;
    struct SyscallOracle {
        uint64_t count = 0;
        uint64_t kernel_ticks = 0;
        PmuScope pmu;
    };
    std::map<uint64_t, SyscallOracle> cpl_syscalls_;
    std::map<uint64_t, uint64_t> cpl_irq_vectors_;
    std::map<std::string, uint64_t> cpl_unknown_sources_;
    PmuScope cpl_pmu_user_;
    PmuScope cpl_pmu_user_plus_kernel_;
    std::array<PmuScope, CplCycleClassCount> cpl_pmu_by_class_{};
    uint64_t cpl_committed_memory_uops_ = 0;
    uint64_t cpl_packet_attributed_uops_ = 0;
    uint64_t cpl_fallback_attributed_uops_ = 0;
    uint64_t cpl_explicitly_rejected_uops_ = 0;
    uint64_t cpl_line_requests_ = 0;
    uint64_t cpl_dtlb_unknown_uops_ = 0;
    uint64_t cpl_late_packets_after_fallback_ = 0;
    std::unordered_map<uint64_t, PendingNativeResponseDiag>
        pending_native_response_diag_;
    uint64_t native_diag_committed_uops_ = 0;
    uint64_t native_diag_packet_source_uops_ = 0;
    uint64_t native_diag_fallback_source_uops_ = 0;
    uint64_t native_diag_response_at_commit_uops_ = 0;
    uint64_t native_diag_terminal_at_commit_uops_ = 0;
    uint64_t native_diag_late_response_uops_ = 0;
    uint64_t native_diag_late_terminal_uops_ = 0;
    uint64_t native_diag_response_without_fact_uops_ = 0;
    std::array<NativeScopeAggregate, CplCycleClassCount>
        native_diag_by_scope_{};
    std::array<std::array<uint64_t, 2>, 5>
        native_diag_proxy_confusion_{};
    std::array<uint64_t, 7> native_diag_terminal_reason_counts_{};
    std::vector<NativeAnomalySample> native_diag_anomaly_samples_;
    uint64_t native_diag_anomaly_samples_dropped_ = 0;
    bool native_diag_finalized_ = false;
    static std::FILE *global_cpl_class_;
    // Baseline-only diagnostic stream. This never enters FST or FastSim; it
    // proves which architectural x86 #PF entries back the aggregate oracle.
    static std::FILE *global_page_fault_events_;
    // per-thread micro_seq 计数（从 1 开始，与 atomic micro_seq 对齐）
    std::unordered_map<uint32_t, uint64_t> micro_seq_per_thread_;
    // per-thread last_writer：(cls<<24 | idx) -> 最近写者 micro_seq
    std::unordered_map<uint32_t,
        std::unordered_map<uint32_t, uint64_t>> last_writer_per_thread_;

    std::string output_dir_;
    std::string uarch_profile_path_;

    std::unordered_map<uint32_t, MacroAccum> current_macro_;
    std::unordered_map<uint32_t, uint64_t>   prev_commit_tick_;
    std::unordered_map<uint32_t, uint64_t>   last_squash_tick_;

    // line state 机：cacheline_addr → LineState（替代 v1 的 attr_cache_ stub）
    // Fix B: 改为 process-global static（每核一个 TaoTrace 实例，需要共享视图）
    static std::unordered_map<uint64_t, LineState>  line_states_;
    static std::unordered_map<uint64_t, uint32_t>   recent_line_count_;
    // Fix A: SharedAttr 与 acc 在时间上解耦。 These tables must remain
    // instance-local: ThreadID and InstSeqNum are local to each O3 CPU, so a
    // process-global table would alias equal (tid, seqNum) pairs across cores.
    //   onDataAccessComplete 触发先于 commit/accumulateMicro，
    //   故先用 inst->seqNum 缓存，accumulateMicro 处理 mem-touching micro 时回填。
    std::unordered_map<uint64_t, SharedAttr> pending_shared_attr_;
    // A data access may complete either before or after its instruction
    // commits. PMU attribution is recorded only for committed accesses: an
    // early completion waits in pending_shared_attr_, while a late completion
    // consumes the class captured here at commit.
    std::unordered_map<uint64_t, CplDataAttribution>
        pending_cpl_data_class_;

    // V2 三层 cache：每核一份 L1D / L1I / L2 视图（BankedSetAssocLRU），
    //   shared LLC 一份。所有容量 / assoc / banks 来自 uarch_profile.json。
    static tao_uarch::UarchProfile uarch_;
    static bool                    uarch_loaded_;
    static std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l1d_lru_;
    static std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l1i_lru_;
    static std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l2_lru_;
    static tao_uarch::BankedSetAssocLRU                               l3_lru_;
    static std::unordered_map<uint32_t, tao_uarch::TlbSim>            dtlb_;
    static std::unordered_map<uint32_t, tao_uarch::TlbSim>            itlb_;
    static tao_uarch::PageWalkSim                                     walker_;
    static std::unordered_map<uint32_t, tao_uarch::MshrTracker>       l1d_mshr_;
    static std::unordered_map<uint32_t, tao_uarch::MshrTracker>       l1i_mshr_;

    // i-side 独立视图（vaddr 域）：与 d-side paddr 视图严格隔离。
    //   i-cache 取指 packet 仅从 req->getVaddr() 获取 key，所以 i-side
    //   的 LRU/lines/walker 全部用 vaddr-line 寻址；与 d-side 的 paddr
    //   视图互不污染。l1i_lru_ / itlb_ / l1i_mshr_ 历史上已是 i-side
    //   独占，此处把它们的语义正式收敛到 vaddr 域。
    static std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> l2_i_lru_;
    static tao_uarch::BankedSetAssocLRU                               l3_i_lru_;
    static tao_uarch::PageWalkSim                                     i_walker_;
    static std::unordered_map<uint64_t, LineState>                    i_line_states_;

    // V9.6 ROI 状态（进程级共享）：
    //   require_roi_：是否启用 always-update + ROI-only-emit 闸门；
    //                 由第一个被构造的 TaoTrace 实例的 require_roi_param_ 写入。
    //   roi_active_ ：当前是否处于 ROI 段（任意 thread workbegin -> active；
    //                 当 active_count_ 归零 -> inactive）。
    //   roi_active_count_：嵌套 / 多线程 ROI 计数器（workbegin++/workend--）。
    static bool     require_roi_;
    static bool     roi_active_;
    static bool     cpl_measurement_active_;
    static uint64_t cpl_measurement_begin_tick_;
    static uint64_t cpl_measurement_end_tick_;
    static uint64_t roi_active_count_;
    static std::unordered_map<uint32_t, uint64_t>
        roi_active_count_per_core_;
    static std::unordered_map<uint32_t, uint64_t>
        cpl_measurement_begin_tick_per_core_;
    static std::unordered_map<uint32_t, uint64_t>
        cpl_measurement_end_tick_per_core_;
    static std::FILE *global_roi_boundaries_;
    static uint32_t live_instances_;
    static uint64_t global_functional_user_target_;
    static uint32_t functional_target_instances_;
    static bool functional_target_exit_scheduled_;
    static TaoTrace *functional_target_drain_owner_;
    static uint64_t functional_target_drain_polls_;
    static std::unordered_set<const TaoTrace *> functional_target_reached_;
    static std::unordered_set<TaoTrace *> functional_warmup_instances_;
    // One functional snapshot is shared across per-core probes because the
    // current trace contract is one Linux process/address space.
    static bool initial_pte_state_captured_;
    static uint64_t initial_pte_root_;
    static std::unordered_map<uint64_t, bool> initial_pte_present_;
    static bool measurement_pte_state_captured_;
    static uint64_t measurement_pte_root_;
    static std::unordered_map<uint64_t, bool> measurement_pte_present_;

    // i-side oracle 缓存：core_id -> (i_cl -> InstSharedAttr)
    // 在 accumulateMicro 中按 macro_pc cacheline 取出，喂给 emitMicroRecord。
    std::unordered_map<uint32_t,
        std::unordered_map<uint64_t, InstSharedAttr>> last_i_attr_per_core_;

    // 懒加载：在 regProbeListeners 之前调用，幂等。
    static void ensureUarchLoaded(const std::string& output_dir,
                                  const std::string& explicit_path);
    // 懒配置 helpers（仅在第一次访问该核时按 uarch_ 装配）
    static tao_uarch::BankedSetAssocLRU& getL1d(uint32_t cid);
    static tao_uarch::BankedSetAssocLRU& getL1i(uint32_t cid);
    static tao_uarch::BankedSetAssocLRU& getL2 (uint32_t cid);
    static tao_uarch::BankedSetAssocLRU& getL2i(uint32_t cid);
    static tao_uarch::TlbSim&            getDtlb(uint32_t cid);
    static tao_uarch::TlbSim&            getItlb(uint32_t cid);
    static tao_uarch::MshrTracker&       getL1dMshr(uint32_t cid);
    static tao_uarch::MshrTracker&       getL1iMshr(uint32_t cid);

    // V1 multi-core 新增：per-thread 16-bit branch direction history（最近 16 次分支 taken/not-taken）
    std::unordered_map<uint32_t, uint16_t>   branch_history_;
    // 每 (thread, cacheline_addr) 上次访问的 macro 序号，用于 access_distance 计算
    std::unordered_map<uint64_t, uint64_t>   last_access_seq_;
    // 每 thread 的 macro 计数（用于 access_distance）
    std::unordered_map<uint32_t, uint64_t>   macro_count_per_thread_;

    // 调度事件追踪：每核当前承载的 thread_id（-1 表示未初始化）
    std::unordered_map<uint32_t, int64_t>    last_thread_on_core_;
    // 该核上每个 thread 的最近一次提交 seq_id（用于 SCHED_OUT 锚点）
    std::unordered_map<uint64_t, uint64_t>   last_seq_per_core_thread_;
    // 已经在该核 emit 过 THREAD_CREATE 的 thread 集合
    std::unordered_set<uint64_t>             created_core_thread_;

    struct PendingSyscall {
        uint64_t nr = 0;
        uint64_t arg0 = 0, arg1 = 0, arg2 = 0, arg3 = 0, arg4 = 0, arg5 = 0;
        uint64_t fetch_tick = 0;
        uint64_t user_rsp = 0;
        uint64_t address_space = 0;
        uint64_t return_pc = 0;
        uint64_t pre_timestamp_us = 0;
        uint32_t pre_cpu = 0;
    };
    struct FutexSiteState { uint64_t wait_count = 0, wake_count = 0; };

    std::unordered_map<uint64_t, PendingSyscall> pending_syscalls_;
    std::unordered_map<uint64_t, FutexSiteState> futex_sites_;
    std::unordered_set<uint64_t> emitted_syscall_seqs_;
    std::unordered_map<const void *, bool> syscall_macro_cache_;
};

} // namespace o3
} // namespace gem5

#endif // __CPU_O3_PROBE_TAO_TRACE_HH__
