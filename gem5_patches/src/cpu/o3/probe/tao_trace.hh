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

#include <cstdint>
#include <cstdio>
#include <list>
#include <string>
#include <unordered_map>
#include <unordered_set>

#include "cpu/o3/dyn_inst_ptr.hh"
#include "lru_banked.hh"
#include "mem/packet.hh"
#include "params/TaoTrace.hh"
#include "sim/probe/probe_listener_object.hh"
#include "uarch_profile.hh"

// uarch_profile.hh / lru_banked.hh 是 oracle 与 ref_sim 的同源；SConscript
// 已经把 single_core_mvp/shared 加入 CPPPATH。

namespace gem5
{
namespace o3
{

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
        // pipeline-stage tick 缓存（来自 last micro），仅用于 labels.jsonl 拆分
        int64_t  last_issue_tick_delta = -1;
        int64_t  last_complete_tick_delta = -1;
        bool     last_mispredicted = false;
    };

    // ---- Listener entry points ----
    void onCommit(const DynInstPtr &dynInst);
    void onCommitStall(const DynInstPtr &dynInst);
    void onSquash(const DynInstPtr &dynInst);
    void onExecute(const DynInstPtr &dynInst);
    void onDataAccessComplete(const std::pair<DynInstPtr, PacketPtr> &p);
    // V9.5 i-cache probe：通过 cpu->ppInstAccessComplete 钩子收 fetch
    //   的 cache packet，按 (l1i_lru_, l2_lru_, l3_lru_) 推断 i-side 命中层级。
    //   如果 fetch.cc 不发该事件（如 atomic 模式），注册仍然安全（无 callback）。
    void onInstAccessComplete(const PacketPtr &pkt);

    // ---- Helpers ----
    bool isLockedAtomicMicro(const DynInstPtr &inst) const;
    bool isMacroopLocked(const DynInstPtr &inst) const;
    bool isSyscallInst(const DynInstPtr &inst) const;
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
                         const InstSharedAttr &i_oracle);
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
    static void traceWorkBegin(uint32_t core_id, uint64_t workid,
                               uint64_t threadid);
    static void traceWorkEnd  (uint32_t core_id, uint64_t workid,
                               uint64_t threadid);
    // emit 闸门：emit*/write* 函数入口统一调用，集中管理短路逻辑。
    static inline bool emitGateOpen()
    {
        // require_roi_ 关 -> 始终允许 emit（V9.5 行为）
        // require_roi_ 开 -> 仅 ROI 处于 active 状态时允许 emit
        return !require_roi_ || roi_active_;
    }

private:

    // 调度事件触发器（按 syscall NR / commit thread 切换）
    void maybeEmitSchedSwitch(uint32_t core_id, uint32_t thread_id,
                              uint64_t anchor_seq_id);
    void maybeEmitThreadCreate(uint32_t core_id, uint32_t thread_id,
                               uint64_t anchor_seq_id);

    void openOutput();

    // ---- State ----
    std::FILE *out_records_ = nullptr;
    std::FILE *out_sched_   = nullptr;
    std::FILE *out_labels_  = nullptr;
    std::FILE *out_diag_    = nullptr;
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

    bool emit_macro_ = false;
    bool emit_micro_ = true;
    // V9.5：cache 事件流（mem_events.jsonl）独立开关，与指令粒度正交。
    bool emit_mem_events_ = true;
    // V9.6 ROI 模式：true 时启用 always-update + ROI-only-emit 闸门；
    //   false 时退化为 V9.5 全程 emit 行为。SimObject 参数 require_roi。
    //   首个 TaoTrace 实例构造时把它写入静态 require_roi_，进程级共享。
    bool require_roi_param_ = false;
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
    // Fix A: SharedAttr 与 acc 在时间上解耦：
    //   onDataAccessComplete 触发先于 commit/accumulateMicro，
    //   故先用 inst->seqNum 缓存，accumulateMicro 处理 mem-touching micro 时回填。
    static std::unordered_map<uint64_t, SharedAttr> pending_shared_attr_;

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
    static uint64_t roi_active_count_;

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
    };
    struct FutexSiteState { uint64_t wait_count = 0, wake_count = 0; };

    std::unordered_map<uint64_t, PendingSyscall> pending_syscalls_;
    std::unordered_map<uint64_t, FutexSiteState> futex_sites_;
    std::unordered_set<uint64_t> emitted_syscall_seqs_;
};

} // namespace o3
} // namespace gem5

#endif // __CPU_O3_PROBE_TAO_TRACE_HH__
