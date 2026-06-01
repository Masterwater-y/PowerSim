/*
 * TaoTrace implementation — macro-op granularity, v2 (multi-core + scheduler
 * observability + causal SharedAttr).
 *
 * 详见 single_core_mvp/doc/01_dataset_io_spec.md（v2）。
 *
 * v2 输出物理分流为 4 个文件：
 *   <name>.records.jsonl  TAO-Core 训练输入（µarch 无关，禁含 tick/cycle）
 *   <name>.sched.jsonl    调度可观测信号（不进训练；anchor_seq_id 锚点）
 *   <name>.labels.jsonl   µarch 相关训练标签（exposed/macro/branch_pen cycles）
 *   <name>.diag.jsonl     诊断（tick）
 */
#include "cpu/o3/probe/tao_trace.hh"

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cinttypes>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "arch/x86/insts/static_inst.hh"
#include "arch/x86/regs/int.hh"
#include "base/trace.hh"
#include "cpu/base.hh"
#include "cpu/o3/dyn_inst.hh"
#include "cpu/reg_class.hh"
#include "cpu/thread_context.hh"
#include "debug/TaoTrace.hh"
#include "kern/linux/linux.hh"
#include "mem/request.hh"
#include "sim/cur_tick.hh"

namespace gem5
{
namespace o3
{

// Fix B: 跨实例共享的 line 状态机（每核一个 TaoTrace 实例 → 必须共享）
std::unordered_map<uint64_t, TaoTrace::LineState> TaoTrace::line_states_;
std::unordered_map<uint64_t, uint32_t>            TaoTrace::recent_line_count_;
// Fix A: pending SharedAttr 表，key = (tid<<48) | seqNum
std::unordered_map<uint64_t, TaoTrace::SharedAttr> TaoTrace::pending_shared_attr_;

// V2 三层 cache：所有视图（L1D / L1I / L2 / 共享 LLC）+ TLB / page walker /
//   MSHR 全部由 uarch_profile.json (schema v2) 配置，禁止任何 hardcoded 参数。
//   这里仅提供静态成员定义；首次 regProbeListeners() 时调用 ensureUarchLoaded()
//   完成 lazy configure。
tao_uarch::UarchProfile TaoTrace::uarch_;
bool                    TaoTrace::uarch_loaded_ = false;
std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> TaoTrace::l1d_lru_;
std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> TaoTrace::l1i_lru_;
std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> TaoTrace::l2_lru_;
tao_uarch::BankedSetAssocLRU                               TaoTrace::l3_lru_;
std::unordered_map<uint32_t, tao_uarch::TlbSim>            TaoTrace::dtlb_;
std::unordered_map<uint32_t, tao_uarch::TlbSim>            TaoTrace::itlb_;
tao_uarch::PageWalkSim                                     TaoTrace::walker_;
std::unordered_map<uint32_t, tao_uarch::MshrTracker>       TaoTrace::l1d_mshr_;
std::unordered_map<uint32_t, tao_uarch::MshrTracker>       TaoTrace::l1i_mshr_;

// i-side 独立视图（vaddr 域），与 d-side paddr 视图严格隔离。
std::unordered_map<uint32_t, tao_uarch::BankedSetAssocLRU> TaoTrace::l2_i_lru_;
tao_uarch::BankedSetAssocLRU                               TaoTrace::l3_i_lru_;
tao_uarch::PageWalkSim                                     TaoTrace::i_walker_;
std::unordered_map<uint64_t, TaoTrace::LineState>          TaoTrace::i_line_states_;

// V4：进程级共享 mem_events sink。所有静默 cache 事件（evict / prefetch）
//   都写入第一个被打开的 mem_events.jsonl，由 commit_tick + seq 进行全序。
//   commit 事件依然分散写入各自 core 的 mem_events 文件，外部 ref_sim 通过
//   merge-sort by commit_tick 合并多源。
std::FILE *TaoTrace::global_mem_events_      = nullptr;
uint64_t   TaoTrace::global_mem_event_counter_ = 0;

// V9.6 ROI 闸门（进程级共享）：
//   require_roi_=false 时退化为 V9.5 全程 emit；require_roi_=true 时仅
//   active_count_>0 期间放行 emit。所有 TaoTrace 实例共用同一对 (active_,
//   active_count_)，确保多核 m5_work_begin/end 观察一致。
bool     TaoTrace::require_roi_      = false;
bool     TaoTrace::roi_active_       = false;
uint64_t TaoTrace::roi_active_count_ = 0;

namespace
{

constexpr uint64_t X86_64_SYS_sched_yield = 24;
// X86_64_SYS_clone (56) 在此版本里不显式判定：子线程的 THREAD_CREATE 由
// "首次在某 (core, thread) 上 commit" 触发，比追踪父线程 syscall 更鲁棒。
constexpr uint64_t X86_64_SYS_exit_group  = 231;
constexpr uint64_t X86_64_SYS_futex       = 202;
constexpr uint64_t X86_32_SYS_futex       = 240;

inline uint64_t
makeCoreThreadKey(uint32_t core_id, uint32_t thread_id)
{
    return (uint64_t(core_id) << 32) | uint64_t(thread_id);
}

std::string
toLower(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(),
        [](unsigned char c) { return std::tolower(c); });
    return s;
}

bool
containsAny(const std::string &text, std::initializer_list<const char *> needles)
{
    for (const char *n : needles)
        if (text.find(n) != std::string::npos) return true;
    return false;
}

bool
isFutexNr(uint64_t nr)
{
    return nr == X86_64_SYS_futex || nr == X86_32_SYS_futex;
}

uint64_t
normalizeFutexOp(uint64_t op)
{
    op &= ~uint64_t(Linux::TGT_FUTEX_PRIVATE_FLAG);
    op &= ~uint64_t(Linux::TGT_FUTEX_CLOCK_REALTIME_FLAG);
    return op;
}

} // namespace

TaoTrace::TaoTrace(const TaoTraceParams &params)
    : ProbeListenerObject(params),
      emit_macro_(params.emit_macro),
      emit_micro_(params.emit_micro),
      emit_mem_events_(params.emit_mem_events),
      require_roi_param_(params.require_roi),
      output_dir_(params.output_dir),
      uarch_profile_path_(params.uarch_profile_path)
{
    // V9.6：require_roi_ 进程级共享。任意一个 TaoTrace 实例置位即生效；
    //   实践中所有 TaoTrace SimObject 通过同一份 Python 配置生成，参数一致。
    if (require_roi_param_) {
        require_roi_ = true;
    }
    openOutput();
}

TaoTrace::~TaoTrace()
{
    auto closeFile = [](std::FILE *&fp) {
        if (fp) {
            std::fflush(fp);
            std::fclose(fp);
            fp = nullptr;
        }
    };
    closeFile(out_records_);
    closeFile(out_sched_);
    closeFile(out_labels_);
    closeFile(out_diag_);
    // V4：若本实例的 mem_events 正是进程级 sink，先清掉全局指针，避免
    //   teardown 期间 Ruby 析构再次回调 traceCacheEvent 时出现悬空 FILE*。
    if (global_mem_events_ == out_mem_events_) {
        global_mem_events_ = nullptr;
    }
    closeFile(out_mem_events_);
    closeFile(out_records_micro_);
    closeFile(out_labels_micro_);
}

void
TaoTrace::openOutput()
{
    if (output_dir_.empty()) output_dir_ = "tao_trace";
    ::mkdir(output_dir_.c_str(), 0755);
    char path[1024];
    auto open_one = [&](const char *suffix) -> std::FILE * {
        std::snprintf(path, sizeof(path), "%s/%s.%s.jsonl",
                      output_dir_.c_str(), name().c_str(), suffix);
        std::FILE *fp = std::fopen(path, "w");
        if (!fp) warn("TaoTrace: failed to open %s", path);
        return fp;
    };
    if (emit_macro_) {
        out_records_ = open_one("records");
        out_sched_   = open_one("sched");
        out_labels_  = open_one("labels");
        out_diag_    = open_one("diag");
    }
    // V9.5：mem_events.jsonl 与 macro-op 指令粒度正交（cacheline 事件流），
    //   独立开关 emit_mem_events_ 控制；ref_sim replay / 17/17 bit-exact /
    //   5 张 PMU 表全部依赖它，默认 True。
    if (emit_mem_events_) {
        out_mem_events_ = open_one("mem_events");
        // V4：第一个被打开的 mem_events sink 即作为进程级 sink，
        //   后续 Ruby 静态钩子 (traceCacheEvent) 会向它追加 evict / prefetch 行。
        if (!global_mem_events_ && out_mem_events_) {
            global_mem_events_ = out_mem_events_;
        }
    }
    if (emit_micro_) {
        out_records_micro_ = open_one("records.micro");
        out_labels_micro_  = open_one("labels.micro");
    }
}

// V9.6 ROI hook：由 sim/pseudo_inst.cc 中 install.sh 注入的两行代码调用。
//   roi_active_count_ 在多线程 / 多核同时进入 ROI 时计数累加；归零方关闭。
//   状态机故意做成 active_count_ 的引用计数，使得：
//     - 单线程嵌套 work_begin/end（虽然 micro-bench 不会用到）也能正确处理；
//     - 多核 worker 各自调用 m5_work_begin → ROI 在第一次 begin 即开启，
//       全部 worker 完成 m5_work_end 后方关闭。
//   require_roi_=false 时 hook 仍然写状态（无副作用），仅 emit 闸门不参考。
void
TaoTrace::traceWorkBegin(uint32_t /*core_id*/, uint64_t /*workid*/,
                         uint64_t /*threadid*/)
{
    roi_active_count_ += 1;
    roi_active_ = (roi_active_count_ > 0);
}

void
TaoTrace::traceWorkEnd(uint32_t /*core_id*/, uint64_t /*workid*/,
                       uint64_t /*threadid*/)
{
    if (roi_active_count_ > 0) {
        roi_active_count_ -= 1;
    }
    roi_active_ = (roi_active_count_ > 0);
}

// V4：Ruby 侧静态钩子。来自 CacheMemory::deallocate / RubyPrefetcherProxy::ppFill
//   等位置，event_type 取值 "evict" / "prefetch" / "inval"。
//   仅写入 mem_events 流，并不更新 line_states_/LRU 视图（probe 内部视图已经
//   在 commit 路径维护，这里只为 ref_simulator 提供事件输入）。
void
TaoTrace::traceCacheEvent(const char *event_type, uint32_t core_id,
                          uint64_t cacheline_addr, int cache_level)
{
    if (!global_mem_events_) return;
    if (!event_type) event_type = "unknown";
    // V9.6：ROI gate 短路 mem_events 的 evict/prefetch/inval emit；
    //   下方 LRU/line_states_ 状态机仍然 always-update，保证 ROI 打开时
    //   probe 视图与 Ruby 真值已同步预热。
    if (emitGateOpen()) {
        std::fprintf(global_mem_events_,
            "{\"seq\":%lu,\"event_type\":\"%s\",\"core_id\":%u,"
            "\"cacheline_addr\":%lu,\"cache_level\":%d,"
            "\"commit_tick\":%lu}\n",
            (unsigned long)global_mem_event_counter_++, event_type, core_id,
            (unsigned long)cacheline_addr, cache_level,
            (unsigned long)curTick());
    }

    // V4 时序对齐: 把 Ruby 的 evict/prefetch 信号同样应用到 oracle 的 LRU
    // 视图，保持 oracle 与 ref_simulator 看到的 LRU 序列一致。
    // cacheline_addr 已为 paddr cacheline 起始地址。
    if (!uarch_loaded_) return;  // 视图未配置则跳过（理论上不会发生，因为
                                 //  regProbeListeners 已先于 Ruby 事件）
    const uint64_t cl = cacheline_addr & ~uint64_t(63);
    if (std::strcmp(event_type, "evict") == 0) {
        switch (cache_level) {
        case 0: {
            auto it = l1d_lru_.find(core_id);
            if (it != l1d_lru_.end()) it->second.invalidate(cl);
            break;
        }
        case 1: {
            auto it = l2_lru_.find(core_id);
            if (it != l2_lru_.end()) it->second.invalidate(cl);
            break;
        }
        case 2:
            l3_lru_.invalidate(cl);
            break;
        case 4: {
            // i-cache eviction
            auto it = l1i_lru_.find(core_id);
            if (it != l1i_lru_.end()) it->second.invalidate(cl);
            break;
        }
        default: break;
        }
    } else if (std::strcmp(event_type, "prefetch") == 0) {
        switch (cache_level) {
        case 0:
            getL1d(core_id).touch(cl);
            break;
        case 1:
            getL2(core_id).touch(cl);
            break;
        case 2:
            l3_lru_.touch(cl);
            break;
        case 4:
            getL1i(core_id).touch(cl);
            break;
        default: break;
        }
    }
}

// ---------- uarch_profile.json 懒加载 + per-core lazy configure ----------

void
TaoTrace::ensureUarchLoaded(const std::string& output_dir,
                            const std::string& explicit_path)
{
    if (uarch_loaded_) return;
    auto exists = [](const std::string& path) {
        std::ifstream f(path);
        return f.good();
    };
    std::string p = explicit_path;
    if (p.empty()) {
        std::string c1 = output_dir + "/../uarch_profile.json";
        std::string c2 = output_dir + "/uarch_profile.json";
        if (exists(c1)) p = c1;
        else if (exists(c2)) p = c2;
    }
    if (p.empty() || !exists(p)) {
        throw std::runtime_error(
            "TaoTrace: uarch_profile.json not found "
            "(set TaoTrace.uarch_profile_path or place "
            "at <outdir>/uarch_profile.json)");
    }
    uarch_ = tao_uarch::UarchProfile::load(p);  // 验证 / fail-fast
    walker_.configure(uarch_.walker);
    l3_lru_.configure(uarch_.l3);
    // i-side 独立视图（vaddr 域）
    i_walker_.configure(uarch_.walker);
    l3_i_lru_.configure(uarch_.l3);
    uarch_loaded_ = true;
}

tao_uarch::BankedSetAssocLRU&
TaoTrace::getL1d(uint32_t cid)
{
    auto& s = l1d_lru_[cid];
    if (!s.configured()) s.configure(uarch_.l1d);
    return s;
}

tao_uarch::BankedSetAssocLRU&
TaoTrace::getL1i(uint32_t cid)
{
    auto& s = l1i_lru_[cid];
    if (!s.configured()) s.configure(uarch_.l1i);
    return s;
}

tao_uarch::BankedSetAssocLRU&
TaoTrace::getL2(uint32_t cid)
{
    auto& s = l2_lru_[cid];
    if (!s.configured()) s.configure(uarch_.l2);
    return s;
}

tao_uarch::BankedSetAssocLRU&
TaoTrace::getL2i(uint32_t cid)
{
    auto& s = l2_i_lru_[cid];
    if (!s.configured()) s.configure(uarch_.l2);
    return s;
}

tao_uarch::TlbSim&
TaoTrace::getDtlb(uint32_t cid)
{
    auto& s = dtlb_[cid];
    if (s.entries() == 0) s.configure(uarch_.dtlb, uarch_.walker.page_size_bits);
    return s;
}

tao_uarch::TlbSim&
TaoTrace::getItlb(uint32_t cid)
{
    auto& s = itlb_[cid];
    if (s.entries() == 0) s.configure(uarch_.itlb, uarch_.walker.page_size_bits);
    return s;
}

tao_uarch::MshrTracker&
TaoTrace::getL1dMshr(uint32_t cid)
{
    static std::unordered_set<uint32_t> configured;
    auto& s = l1d_mshr_[cid];
    if (!configured.count(cid)) {
        s.configure(uarch_.mshr.l1d, /*window_seq=*/0);
        configured.insert(cid);
    }
    return s;
}

tao_uarch::MshrTracker&
TaoTrace::getL1iMshr(uint32_t cid)
{
    static std::unordered_set<uint32_t> configured;
    auto& s = l1i_mshr_[cid];
    if (!configured.count(cid)) {
        s.configure(uarch_.mshr.l1d, /*window_seq=*/0);
        configured.insert(cid);
    }
    return s;
}

void
TaoTrace::regProbeListeners()
{
    // 在第一个 callback 触发之前装载 uarch_profile.json：保证后续 LRU/TLB/walker
    // 视图均按 schema v2 配置。
    ensureUarchLoaded(output_dir_, uarch_profile_path_);

    using DynInstListener = ProbeListenerArg<TaoTrace, DynInstPtr>;
    using PktListener = ProbeListenerArg<
        TaoTrace, std::pair<DynInstPtr, PacketPtr>>;
    using PktOnly = ProbeListenerArg<TaoTrace, PacketPtr>;

    connectListener<DynInstListener>(this, "Commit",      &TaoTrace::onCommit);
    connectListener<DynInstListener>(this, "CommitStall", &TaoTrace::onCommitStall);
    connectListener<DynInstListener>(this, "Squash",      &TaoTrace::onSquash);
    connectListener<DynInstListener>(this, "Execute",     &TaoTrace::onExecute);
    connectListener<PktListener>(this, "DataAccessComplete",
                                 &TaoTrace::onDataAccessComplete);
    // i-cache 访问完成事件（如未实现则该 connect 不触发任何 callback）
    connectListener<PktOnly>(this, "InstAccessComplete",
                             &TaoTrace::onInstAccessComplete);
}

// -------------------------------------------------------------------------
// 基础辅助
// -------------------------------------------------------------------------

bool
TaoTrace::isMacroopLocked(const DynInstPtr &inst) const
{
    auto macro = inst->macroop;
    if (!macro) return false;
    auto x86_inst = dynamic_cast<const X86ISA::X86StaticInst *>(macro.get());
    return x86_inst && x86_inst->machInst.legacy.lock;
}

bool
TaoTrace::isLockedAtomicMicro(const DynInstPtr &inst) const
{
    if (inst->memReqFlags & Request::LOCKED_RMW) return true;
    if (auto si = inst->staticInst) {
        const std::string mn = toLower(si->getName());
        if (mn == "ldstl" || mn == "stul" ||
            mn == "ldsplitl" || mn == "stsplitl")
            return true;
    }
    return false;
}

bool
TaoTrace::isSyscallInst(const DynInstPtr &inst) const
{
    auto si = inst->staticInst;
    if (si && si->isSyscall()) return true;
    if (si) {
        const std::string mn = toLower(si->getName());
        if (mn == "syscall" || mn == "sysenter" || mn == "int80")
            return true;
    }
    return false;
}

uint32_t
TaoTrace::getTraceThreadId(const DynInstPtr &inst) const
{
    auto tc = inst->tcBase();
    return tc ? tc->contextId() : inst->threadNumber;
}

uint32_t
TaoTrace::getCoreId(const DynInstPtr &inst) const
{
    return inst->cpu ? inst->cpu->cpuId() : 0u;
}

uint64_t
TaoTrace::ticksToCycles(uint64_t ticks, const DynInstPtr &inst) const
{
    if (!inst->cpu) return ticks;
    Tick period = inst->cpu->clockPeriod();
    return period ? ticks / period : ticks;
}

uint8_t
TaoTrace::bucketCount(size_t n)
{
    if (n == 0) return 0;
    if (n == 1) return 1;
    if (n == 2) return 2;
    if (n <= 7) return 3;
    return 4;  // 8+
}

// -------------------------------------------------------------------------
// SharedAttr 真值推导（基于 packet flags + line state 机）
// -------------------------------------------------------------------------

TaoTrace::SharedAttr
TaoTrace::deriveSharedAttr(const PacketPtr pkt, uint32_t core_id, bool is_store)
{
    SharedAttr a;
    a.valid = true;
    if (!pkt) return a;

    const Addr addr = pkt->getAddr();
    const uint64_t cl = uint64_t(addr) & ~uint64_t{63};
    LineState &ls = line_states_[cl];

    // mesi_before：本核访问该 line 之前的状态（按 probe 自身 proxy 推断）
    // proxy 规则（可观测信号）：
    //   - cacheResponding() == true → 远端 cache 持有 dirty/clean 拷贝（M/E）
    //   - hasSharers()      == true → 多核共享（S）
    //   - 否则 → I（首次访问或被 invalidate）
    uint8_t mesi_before = 0;
    bool from_remote_dirty = false;
    if (pkt->cacheResponding()) {
        // 有远端 cache 提供数据；hasSharers 则是 S，否则是 M/E。
        if (pkt->hasSharers()) {
            mesi_before = 1; // S
        } else {
            // 远端独占 → 当前为 I（这条 access 之前），但远端 owner 为 M/E
            mesi_before = 0; // I（本核视角）
            from_remote_dirty = true;
        }
    } else if (pkt->hasSharers()) {
        mesi_before = 1; // S
    } else if (ls.mesi != 0 && (ls.owner_core == int32_t(core_id) ||
                                ls.sharers.count(core_id))) {
        // 本核 ld/st cache 命中（无远端响应、无 sharers）→ M/E/S 取决于 ls.mesi
        mesi_before = ls.mesi;
    } else {
        mesi_before = 0; // I：从未见过 / 被驱逐
    }
    a.mesi_before = mesi_before;

    // coh_action：基于 packet 的来源 + V2 三层 LRU 视图
    // 1) 跨核响应（cacheResponding）→ REMOTE_HIT_*；不查 LRU
    // 2) 否则按 (l1_lru → l2_lru → l3_lru) 顺序判定命中层级，
    //    并在所属层级及更高层级 touch；不命中三层 → DRAM。
    if (pkt->cacheResponding()) {
        if (from_remote_dirty || pkt->isWriteback() || pkt->hasSharers() == false)
            a.coh = CoherenceAction::REMOTE_HIT_DIRTY;
        else
            a.coh = CoherenceAction::REMOTE_HIT_CLEAN;
        a.path_class = 3; // NoC（跨核）
        // 远端响应：把 line 装入本核 L1/L2 + 全局 L3
        getL1d(core_id).touch(cl);
        getL2(core_id).touch(cl);
        l3_lru_.touch(cl);
    } else {
        bool l1_hit = getL1d(core_id).touch(cl);
        bool l2_hit = getL2(core_id).touch(cl);
        bool l3_hit = l3_lru_.touch(cl);
        if (l1_hit) {
            a.coh = CoherenceAction::L1_HIT;
            a.path_class = 0;
        } else if (l2_hit) {
            a.coh = CoherenceAction::L2_HIT;
            a.path_class = 1;
        } else if (l3_hit) {
            a.coh = CoherenceAction::LLC_HIT;
            a.path_class = 2;
        } else {
            a.coh = CoherenceAction::DRAM;
            a.path_class = 4;
        }
    }

    // store 而 line 当前由其他核持有 → 需要写回（WB_REQUIRED 优先级最高）
    if (is_store && (mesi_before == 1 ||
                     (ls.owner_core >= 0 && ls.owner_core != int32_t(core_id))))
    {
        a.coh = CoherenceAction::WB_REQUIRED;
    }

    // sharer_count_bucket：基于 line state 中已记录的 sharers 数（排除 self）
    // Fix C: read/store 路径下都需要排除 self；之前 read 路径直接 bucketCount(size)
    // 会让"只有自己 read"的情形显示为 1，导致跨核 sharer_bucket 永远 ≤1。
    {
        size_t sc = ls.sharers.size();
        if (ls.sharers.count(core_id)) sc = (sc > 0) ? sc - 1 : 0;
        a.sharer_count_bucket = bucketCount(sc);
    }

    // dirty_owner：远端有 M 拷贝
    a.dirty_owner = (ls.mesi == 3 && ls.owner_core >= 0 &&
                     ls.owner_core != int32_t(core_id));

    // owner_distance_class：粗粒度 SAME_TILE/NEAR/FAR 占位（4 核单 tile → 全 NEAR）
    if (ls.owner_core < 0)                   a.owner_distance_class = 0; // SELF/none
    else if (ls.owner_core == int32_t(core_id)) a.owner_distance_class = 0; // SELF
    else                                      a.owner_distance_class = 2; // NEAR

    // inval_fanout_bucket：store 时 = 当前 sharer 数（这些 sharer 都会被 invalidate）
    if (is_store) {
        size_t fanout = ls.sharers.size();
        // 排除自己
        if (ls.sharers.count(core_id)) fanout = (fanout > 0) ? fanout - 1 : 0;
        a.inval_fanout_bucket = bucketCount(fanout);
    } else {
        a.inval_fanout_bucket = 0;
    }

    // same_line_recent_bucket：从 recent_line_count_ 取，并 +1 计入本次
    uint32_t rc = recent_line_count_[cl];
    a.same_line_recent_bucket = (rc >= 3) ? 3 : uint8_t(rc);
    recent_line_count_[cl] = rc + 1;

    // ---- 更新 line_states_（本次访问后） ----
    if (is_store) {
        ls.mesi = 3; // M
        ls.owner_core = int32_t(core_id);
        ls.sharers.clear();
        ls.sharers.insert(core_id);
    } else {
        if (ls.mesi == 0) {
            ls.mesi = 2; // E（首次读，假设独占）
            ls.owner_core = int32_t(core_id);
            ls.sharers.clear();
            ls.sharers.insert(core_id);
        } else {
            ls.sharers.insert(core_id);
            if (ls.sharers.size() >= 2) {
                ls.mesi = 1; // S
                ls.owner_core = -1;
            }
        }
    }

    return a;
}

// Fix A2: 纯 line-state 派生（不依赖 packet）
// 用法：当 acc 即将 flush 但 shared_attr.valid 还是 false 时（典型：store
// commit 前 pending 表还没到，或 ld/st 走快路径根本没触发 DataAccessComplete），
// 用本函数从 line_states_ 直接推断 SharedAttr，并按 is_store 更新 line state。
TaoTrace::SharedAttr
TaoTrace::deriveSharedAttrFromLineState(uint64_t vaddr, uint32_t core_id,
                                        bool is_store)
{
    SharedAttr a;
    a.valid = true;

    const uint64_t cl = vaddr & ~uint64_t{63};
    LineState &ls = line_states_[cl];

    // mesi_before：本核视角下访问之前的状态
    uint8_t mesi_before;
    if (ls.mesi == 0) {
        mesi_before = 0;
    } else if (ls.owner_core == int32_t(core_id)) {
        mesi_before = ls.mesi; // 本核拥有
    } else if (ls.sharers.count(core_id)) {
        mesi_before = 1; // S（本核共享）
    } else {
        // 其他核拥有；对本核而言为 I
        mesi_before = 0;
    }
    a.mesi_before = mesi_before;

    // sharer 计数（排除 self）
    size_t sc = ls.sharers.size();
    if (ls.sharers.count(core_id)) sc = (sc > 0) ? sc - 1 : 0;
    a.sharer_count_bucket = bucketCount(sc);

    // dirty_owner / owner_distance
    bool other_owns = (ls.owner_core >= 0 &&
                       ls.owner_core != int32_t(core_id));
    a.dirty_owner = (ls.mesi == 3 && other_owns);
    a.owner_distance_class = (ls.owner_core < 0)
                                ? 0
                                : (other_owns ? 2 : 0);

    // coh_action：基于 line state + V2 三层 LRU 视图
    //   优先：跨核拥有/共享冲突 → REMOTE_HIT_*/WB_REQUIRED；
    //   否则按 (l1 → l2 → l3) LRU 命中层级。
    bool resolved = false;
    if (is_store && (other_owns || sc > 0)) {
        a.coh = CoherenceAction::WB_REQUIRED;
        a.path_class = 3;
        resolved = true;
    } else if (!is_store && other_owns) {
        a.coh = (ls.mesi == 3) ? CoherenceAction::REMOTE_HIT_DIRTY
                                : CoherenceAction::REMOTE_HIT_CLEAN;
        a.path_class = 3;
        resolved = true;
    }

    {
        bool l1_hit = getL1d(core_id).touch(cl);
        bool l2_hit = getL2(core_id).touch(cl);
        bool l3_hit = l3_lru_.touch(cl);
        if (!resolved) {
            if (l1_hit) {
                a.coh = CoherenceAction::L1_HIT;
                a.path_class = 0;
            } else if (l2_hit) {
                a.coh = CoherenceAction::L2_HIT;
                a.path_class = 1;
            } else if (l3_hit) {
                a.coh = CoherenceAction::LLC_HIT;
                a.path_class = 2;
            } else {
                a.coh = CoherenceAction::DRAM;
                a.path_class = 4;
            }
        }
    }

    // inval_fanout：store 时 = 当前 sharer 数（排除 self）
    a.inval_fanout_bucket = is_store ? bucketCount(sc) : 0;

    // same_line_recent
    uint32_t rc = recent_line_count_[cl];
    a.same_line_recent_bucket = (rc >= 3) ? 3 : uint8_t(rc);
    recent_line_count_[cl] = rc + 1;

    // 更新 line state（与 deriveSharedAttr 保持一致）
    if (is_store) {
        ls.mesi = 3;
        ls.owner_core = int32_t(core_id);
        ls.sharers.clear();
        ls.sharers.insert(core_id);
    } else {
        if (ls.mesi == 0) {
            ls.mesi = 2;
            ls.owner_core = int32_t(core_id);
            ls.sharers.clear();
            ls.sharers.insert(core_id);
        } else {
            ls.sharers.insert(core_id);
            if (ls.sharers.size() >= 2) {
                ls.mesi = 1;
                ls.owner_core = -1;
            }
        }
    }
    return a;
}

// -------------------------------------------------------------------------
// Syscall 路径
// -------------------------------------------------------------------------

void
TaoTrace::captureSyscallState(const DynInstPtr &inst)
{
    auto tc = inst->tcBase();
    if (!isSyscallInst(inst) || !tc) return;
    PendingSyscall sc;
    sc.nr   = tc->getReg(X86ISA::int_reg::Rax);
    sc.arg0 = tc->getReg(X86ISA::int_reg::Rdi);
    sc.arg1 = tc->getReg(X86ISA::int_reg::Rsi);
    sc.arg2 = tc->getReg(X86ISA::int_reg::Rdx);
    sc.arg3 = tc->getReg(X86ISA::int_reg::R10);
    sc.arg4 = tc->getReg(X86ISA::int_reg::R8);
    sc.arg5 = tc->getReg(X86ISA::int_reg::R9);
    sc.fetch_tick = (inst->fetchTick != Tick(-1))
                        ? uint64_t(inst->fetchTick) : curTick();
    pending_syscalls_[inst->seqNum] = sc;
}

TaoTrace::SyncType
TaoTrace::classifySyncFromSyscall(const DynInstPtr &inst,
                                  uint64_t nr, uint64_t op,
                                  uint64_t addr, uint64_t val)
{
    (void)inst; (void)addr;
    if (nr == X86_64_SYS_sched_yield) return SyncType::YIELD;
    if (!isFutexNr(nr)) return SyncType::NONE;

    // v2 边界：workload 不会触发 futex；保留分类逻辑兼容旧 workload，但
    // records.jsonl 写入端会把 LOCK/FUTEX/BARRIER 一律替换为 NONE
    // （见 emitSyscallRecord 内的 v2 收敛）。
    op = normalizeFutexOp(op);
    auto &site = futex_sites_[addr];
    switch (op) {
      case Linux::TGT_FUTEX_WAIT:
      case Linux::TGT_FUTEX_WAIT_BITSET:
        site.wait_count++;
        if (val > 2 || site.wake_count > 0) return SyncType::BARRIER;
        return SyncType::FUTEX_WAIT;
      case Linux::TGT_FUTEX_WAKE:
      case Linux::TGT_FUTEX_WAKE_BITSET:
        site.wake_count++;
        if (val > 1 || site.wait_count > 1) return SyncType::BARRIER;
        return SyncType::FUTEX_WAKE;
      case Linux::TGT_FUTEX_REQUEUE:
      case Linux::TGT_FUTEX_CMP_REQUEUE:
      case Linux::TGT_FUTEX_WAKE_OP:
        return SyncType::BARRIER;
      default:
        return SyncType::FUTEX_WAKE;
    }
}

void
TaoTrace::emitSyscallRecord(const DynInstPtr &inst)
{
    if (!out_records_) return;
    if (emitted_syscall_seqs_.count(inst->seqNum)) return;
    emitted_syscall_seqs_.insert(inst->seqNum);

    auto it = pending_syscalls_.find(inst->seqNum);
    PendingSyscall sc;
    if (it != pending_syscalls_.end()) sc = it->second;

    const uint32_t core_id   = getCoreId(inst);
    const uint32_t thread_id = getTraceThreadId(inst);
    const uint64_t seq_id    = inst->seqNum;
    const uint64_t pc        = inst->pcState().instAddr();

    SyncType raw_st = classifySyncFromSyscall(inst, sc.nr, sc.arg1, sc.arg0, sc.arg2);
    // v2：records.jsonl 中 sync_type 仅允许 NONE / YIELD。
    SyncType st = (raw_st == SyncType::YIELD) ? SyncType::YIELD : SyncType::NONE;

    // 调度事件（写入 sched.jsonl）：
    //   - sched_yield → YIELD
    //   - exit_group  → THREAD_EXIT
    //   - clone       → 父线程发的 syscall；子线程的 THREAD_CREATE 由首次 commit 触发
    if (sc.nr == X86_64_SYS_sched_yield) {
        writeSchedEvent(SchedEvent::YIELD_EVT, core_id, thread_id, seq_id,
                        thread_id, "yield");
    } else if (sc.nr == X86_64_SYS_exit_group) {
        writeSchedEvent(SchedEvent::THREAD_EXIT, core_id, thread_id, seq_id,
                        thread_id, "exit");
    }

    // 切换感知（在 syscall 行处也算一次 commit）
    maybeEmitSchedSwitch(core_id, thread_id, seq_id);
    maybeEmitThreadCreate(core_id, thread_id, seq_id);

    // 记录 last_seq_per_core_thread_
    last_seq_per_core_thread_[makeCoreThreadKey(core_id, thread_id)] = seq_id;
    last_thread_on_core_[core_id] = int64_t(thread_id);

    // labels / diag 仍然写
    uint64_t prev = prev_commit_tick_[thread_id];
    uint64_t exposed_t = (prev == 0) ? 0 : (curTick() - prev);
    uint64_t exposed_cyc = ticksToCycles(exposed_t, inst);
    uint64_t macro_t = (sc.fetch_tick && curTick() > sc.fetch_tick)
                            ? (curTick() - sc.fetch_tick) : 0;
    uint64_t macro_cyc = ticksToCycles(macro_t, inst);

    int op_class = inst->staticInst ? int(inst->staticInst->opClass()) : 0;

    writeRecordsSyscallLine(core_id, thread_id, seq_id, pc, op_class, st);
    writeLabelsLine(core_id, thread_id, seq_id, exposed_cyc, macro_cyc, 0,
                    /*fetch_lat_cyc*/ 0, /*exec_lat_cyc*/ 0,
                    /*branch_mispred*/ 0u);
    writeDiagLine(core_id, thread_id, seq_id,
                  sc.fetch_tick, curTick(), prev, last_squash_tick_[thread_id]);

    prev_commit_tick_[thread_id] = curTick();
    DPRINTF(TaoTrace, "syscall emit core=%u tid=%u seq=%llu nr=%llu sync=%u\n",
            core_id, thread_id, (unsigned long long)seq_id,
            (unsigned long long)sc.nr, unsigned(st));
}

// -------------------------------------------------------------------------
// 调度事件（sched.jsonl）
// -------------------------------------------------------------------------

void
TaoTrace::maybeEmitSchedSwitch(uint32_t core_id, uint32_t thread_id,
                               uint64_t anchor_seq_id)
{
    auto it = last_thread_on_core_.find(core_id);
    if (it == last_thread_on_core_.end() || it->second < 0) return;
    if (uint32_t(it->second) == thread_id) return;
    // 切换：先 SCHED_OUT 旧线程，anchor 用旧线程在该核上的最后一次 seq_id
    const uint32_t old_tid = uint32_t(it->second);
    auto last_it = last_seq_per_core_thread_.find(
        makeCoreThreadKey(core_id, old_tid));
    uint64_t out_anchor = (last_it != last_seq_per_core_thread_.end())
                            ? last_it->second : anchor_seq_id;
    writeSchedEvent(SchedEvent::SCHED_OUT, core_id, old_tid, out_anchor,
                    /*by*/ thread_id, "switch");
    writeSchedEvent(SchedEvent::SCHED_IN,  core_id, thread_id, anchor_seq_id,
                    /*by*/ old_tid, "switch");
}

void
TaoTrace::maybeEmitThreadCreate(uint32_t core_id, uint32_t thread_id,
                                uint64_t anchor_seq_id)
{
    uint64_t k = makeCoreThreadKey(core_id, thread_id);
    if (created_core_thread_.count(k)) return;
    created_core_thread_.insert(k);
    writeSchedEvent(SchedEvent::THREAD_CREATE, core_id, thread_id,
                    anchor_seq_id, /*by*/ 0u, "new");
    // 首次出现也立刻生成一个 SCHED_IN（除非这是该核第一次有任何 thread）。
    auto it = last_thread_on_core_.find(core_id);
    if (it == last_thread_on_core_.end() || it->second < 0) {
        writeSchedEvent(SchedEvent::SCHED_IN, core_id, thread_id,
                        anchor_seq_id, /*by*/ 0u, "new");
    }
}

void
TaoTrace::writeSchedEvent(SchedEvent ev, uint32_t core_id, uint32_t thread_id,
                          uint64_t anchor_seq_id, uint32_t by_thread_id,
                          const char *reason)
{
    if (!out_sched_) return;
    if (!emitGateOpen()) return;  // V9.6 ROI 闸门
    const char *name = "UNKNOWN";
    switch (ev) {
        case SchedEvent::SCHED_IN:      name = "SCHED_IN"; break;
        case SchedEvent::SCHED_OUT:     name = "SCHED_OUT"; break;
        case SchedEvent::THREAD_CREATE: name = "THREAD_CREATE"; break;
        case SchedEvent::THREAD_EXIT:   name = "THREAD_EXIT"; break;
        case SchedEvent::YIELD_EVT:     name = "YIELD"; break;
    }
    std::fprintf(out_sched_,
        "{\"event\":\"%s\",\"core_id\":%u,\"thread_id\":%u,"
        "\"anchor_seq_id\":%" PRIu64 ",\"by_thread_id\":%u,"
        "\"reason\":\"%s\"}\n",
        name, core_id, thread_id,
        anchor_seq_id, by_thread_id, reason ? reason : "");
}

// -------------------------------------------------------------------------
// Macro 累积主流程
// -------------------------------------------------------------------------

TaoTrace::InstrType
TaoTrace::deriveInstrType(const MacroAccum &acc) const
{
    if (acc.is_syscall)                               return InstrType::SYS;
    if (acc.any_locked_rmw || acc.macro_legacy_lock)  return InstrType::ATOMIC;
    if (acc.is_branch)                                return InstrType::BR;
    if (acc.any_fence)                                return InstrType::FENCE;
    if (acc.any_load)                                 return InstrType::LD;
    if (acc.any_store)                                return InstrType::ST;
    return InstrType::OTHER;
}

TaoTrace::MemOp
TaoTrace::deriveMemOp(const MacroAccum &acc) const
{
    if (acc.any_locked_rmw || acc.macro_legacy_lock)  return MemOp::ATOMIC;
    if (acc.any_fence)                                return MemOp::FENCE;
    if (acc.any_load && !acc.any_store)               return MemOp::LOAD;
    if (acc.any_store)                                return MemOp::STORE;
    return MemOp::NONE;
}

TaoTrace::SyncType
TaoTrace::classifySyncFromMacro(const MacroAccum &acc) const
{
    // v2 workload 不会产生 atomic/fence；defensive：保留 v1 分类逻辑但实际不会触发。
    if (acc.any_locked_rmw || acc.macro_legacy_lock) {
        if (containsAny(acc.macro_disasm_lower, {"cmpxchg", "xchg"}))
            return SyncType::LOCK_ACQ;
        return SyncType::LOCK_ACQ_PROXY;
    }
    if (acc.any_fence) return SyncType::BARRIER;
    return SyncType::NONE;
}

void
TaoTrace::accumulateMicro(const DynInstPtr &inst)
{
    const uint32_t tid = getTraceThreadId(inst);
    MacroAccum &acc = current_macro_[tid];
    auto si = inst->staticInst;
    if (!si) return;

    if (!acc.valid) {
        acc.valid = true;
        acc.core_id = getCoreId(inst);
        acc.thread_id = tid;
        acc.first_seq = inst->seqNum;
        acc.macro_pc = inst->pcState().instAddr();
        acc.op_class = int(si->opClass());
        acc.first_fetch_tick = (inst->fetchTick != Tick(-1))
                                ? uint64_t(inst->fetchTick) : curTick();
        if (inst->macroop) {
            acc.macro_disasm_lower = toLower(
                inst->macroop->disassemble(acc.macro_pc));
        } else {
            acc.macro_disasm_lower = toLower(si->disassemble(acc.macro_pc));
        }
        acc.macro_legacy_lock = isMacroopLocked(inst);
    }

    if (si->isLoad())          acc.any_load = true;
    if (si->isStore())         acc.any_store = true;
    if (isLockedAtomicMicro(inst)) acc.any_locked_rmw = true;
    if (si->isReadBarrier() || si->isWriteBarrier() || si->isAtomic())
        acc.any_fence = acc.any_fence ||
                        (si->isReadBarrier() || si->isWriteBarrier());
    if (isSyscallInst(inst))   acc.is_syscall = true;

    if (!acc.mem_filled && (si->isLoad() || si->isStore() ||
                            isLockedAtomicMicro(inst))) {
        acc.vaddr = inst->effAddr;
        acc.paddr = inst->physEffAddr;
        acc.access_size = uint16_t(inst->effSize);
        acc.mem_filled = true;
    }

    // Fix A: 回填 SharedAttr — 该 micro 的 onDataAccessComplete 早已发生，
    //   现在按 (tid<<48 | seqNum) 查 pending 表；macro 内首次 mem-touching
    //   micro 命中即回填到 acc，并清掉 pending。
    bool mem_touching = (si->isLoad() || si->isStore() ||
                         isLockedAtomicMicro(inst));
    SharedAttr emit_oracle;        // V3 mem_events.jsonl 输出用
    int        emit_oracle_src = 1; // 0=packet 1=fallback
    if (mem_touching) {
        const uint32_t core_id_local = acc.core_id;
        uint64_t key = (uint64_t(tid) << 48) | uint64_t(inst->seqNum);
        auto it_pa = pending_shared_attr_.find(key);
        if (it_pa != pending_shared_attr_.end()) {
            emit_oracle = it_pa->second;
            emit_oracle_src = 0;
            if (!acc.shared_attr.valid)
                acc.shared_attr = it_pa->second;
            pending_shared_attr_.erase(it_pa);
        } else if (!acc.shared_attr.valid) {
            // store / 慢路径：用 line-state fallback 推断作 oracle
            // V4 方案 A：与 packet 路径同走 paddr，避免 vaddr-keyed entry
            // 污染共用的 l*_lru_/line_states_。
            const bool store_now = si->isStore() || isLockedAtomicMicro(inst);
            emit_oracle = deriveSharedAttrFromLineState(
                inst->physEffAddr, core_id_local, store_now);
        } else {
            emit_oracle = acc.shared_attr;
        }
        // V3：commit 时为每条 mem-touching micro 写一行 mem_event
        // V9.6：ROI gate 短路 mem_events 的两条 emit；line_states_/LRU
        //   等内部状态机由本函数其它路径 always-update，不受闸门影响。
        if (out_mem_events_ && emitGateOpen()) {
            const bool store_now = si->isStore() || isLockedAtomicMicro(inst);
            // V4 方案 A：cacheline_addr 改用 physEffAddr 与 Ruby evict/prefetch 对齐
            uint64_t cl = inst->physEffAddr & ~uint64_t(63);

            // V5 方案 A: 先 emit "request" 行携带 packet 视角真值 coh，作为
            //   ref_simulator 的 pred 标签源。ref_sim 在 request 事件时输出
            //   coh_pred=oracle.coh，commit 事件仅负责 state/LRU 更新。
            //   这样可消除 packet 时刻与 commit 时刻的协议级时序差。
            std::fprintf(out_mem_events_,
                "{\"seq\":%lu,\"event_type\":\"request\",\"core_id\":%u,"
                "\"cacheline_addr\":%lu,\"is_store\":%d,"
                "\"coh_oracle\":%u,\"oracle_source\":%d,"
                "\"commit_tick\":%lu}\n",
                (unsigned long)mem_event_counter_++,
                core_id_local,
                (unsigned long)cl, store_now ? 1 : 0,
                (unsigned)emit_oracle.coh, emit_oracle_src,
                (unsigned long)curTick());

            std::fprintf(out_mem_events_,
                "{\"seq\":%lu,\"event_type\":\"commit\",\"core_id\":%u,"
                "\"thread_id\":%u,"
                "\"vaddr\":%lu,\"cacheline_addr\":%lu,\"is_store\":%d,"
                "\"size\":%u,\"pc\":%lu,\"commit_tick\":%lu,"
                "\"coh_oracle\":%u,\"oracle_source\":%d,"
                "\"mesi_before\":%u,\"sharer_bucket\":%u,\"owner_dist\":%u,"
                "\"dirty_owner\":%u,\"path_class\":%u,\"inval_fanout\":%u,"
                "\"same_line_recent\":%u}\n",
                (unsigned long)mem_event_counter_++,
                core_id_local, tid,
                (unsigned long)inst->effAddr, (unsigned long)cl,
                store_now ? 1 : 0,
                (unsigned)inst->effSize,
                (unsigned long)inst->pcState().instAddr(),
                (unsigned long)curTick(),
                (unsigned)emit_oracle.coh,
                emit_oracle_src,
                (unsigned)emit_oracle.mesi_before,
                (unsigned)emit_oracle.sharer_count_bucket,
                (unsigned)emit_oracle.owner_distance_class,
                (unsigned)(emit_oracle.dirty_owner ? 1 : 0),
                (unsigned)emit_oracle.path_class,
                (unsigned)emit_oracle.inval_fanout_bucket,
                (unsigned)emit_oracle.same_line_recent_bucket);
        }
    }

    // V1 多核新增：累积 reg read/write bitmap（按 IntRegClass index 折叠 mod 64）
    {
        const size_t ns = si->numSrcRegs();
        for (size_t i = 0; i < ns; ++i) {
            const RegId &r = si->srcRegIdx(i);
            if (r.is(IntRegClass)) {
                acc.reg_read_bitmap |= (uint64_t(1) << (r.index() & 63));
            }
        }
        const size_t nd = si->numDestRegs();
        for (size_t i = 0; i < nd; ++i) {
            const RegId &r = si->destRegIdx(i);
            if (r.is(IntRegClass)) {
                acc.reg_write_bitmap |= (uint64_t(1) << (r.index() & 63));
            }
        }
    }

    // V1 多核新增：缓存最后 micro 的 issue/complete tick offset 与 mispredict
    if (inst->issueTick != -1) {
        acc.last_issue_tick_delta = inst->issueTick;
    }
    if (inst->completeTick != -1) {
        acc.last_complete_tick_delta = inst->completeTick;
    }
    if (si->isControl() && inst->mispredicted()) {
        acc.last_mispredicted = true;
    }

    acc.last_commit_tick = curTick();

    // V9 micro 粒度：每条 commit 一行 records.micro / labels.micro。
    //   - 在累积 macro 状态后、boundary 触发 flushMacro 之前 emit；
    //   - emitMicroRecord 自带 emit_micro_ 守门；
    //   - mem_touching 时使用上方已计算的 emit_oracle（packet/fallback 真值），
    //     非 mem_touching 时输出 0 默认值。
    {
        SharedAttr oracle_for_micro;
        bool oracle_filled_micro = false;
        if (mem_touching) {
            oracle_for_micro = emit_oracle;
            oracle_filled_micro = (emit_oracle_src == 0); // 0=packet 真值
            // MSHR：commit 时把这条 mem-touching micro 视为 outstanding 终结。
            //   d-side 不改 coh 分类（packet 是 ground truth），仅维护 outstanding
            //   表，让上层 dataset builder 在 Step 3.6 能基于 MSHR 视图做合并。
            uint64_t mshr_cl = inst->physEffAddr & ~uint64_t(63);
            getL1dMshr(acc.core_id).retire(mshr_cl);
        }

        // i-side oracle：按 macro_pc cacheline 在 last_i_attr_per_core_ 查最近一次
        //   onInstAccessComplete 写入的真值；缺失走 fallback：直接 peek
        //   oracle 的 L1I/L2/L3 LRU 视图推断 path_class / coh，并复用
        //   line_states_ 推 mesi_before。fallback 不修改 LRU 顺序，避免污染
        //   后续真 i-cache miss 的真值视图；oracle_source 仍标 1 表示推断值。
        InstSharedAttr i_attr;
        const uint64_t i_cl = inst->pcState().instAddr() & ~uint64_t(63);
        bool i_attr_filled = false;
        auto it_core = last_i_attr_per_core_.find(acc.core_id);
        if (it_core != last_i_attr_per_core_.end()) {
            auto it_cl = it_core->second.find(i_cl);
            if (it_cl != it_core->second.end()) {
                i_attr = it_cl->second;
                i_attr_filled = true;
            }
        }
        if (!i_attr_filled) {
            // fallback：oracle i-side LRU peek（contains 是 const，不动 LRU 顺序）
            //   全部走 i-side 独立视图（vaddr 域），与 d-side line_states_/L*_lru
            //   严格隔离，不会被 d-side paddr 状态机污染。
            i_attr.valid = true;
            i_attr.oracle_source = 1; // 推断值
            auto it_ls = i_line_states_.find(i_cl);
            if (it_ls != i_line_states_.end()) {
                const LineState &ls = it_ls->second;
                if (ls.owner_core == int32_t(acc.core_id)) {
                    i_attr.mesi_before = ls.mesi;
                } else if (ls.sharers.count(acc.core_id)) {
                    i_attr.mesi_before = 1; // S
                } else {
                    i_attr.mesi_before = 0; // I（远端拥有）
                }
            } else {
                i_attr.mesi_before = 0;
            }
            // path_class / coh_oracle：L1I → L2I → L3I 顺序 peek 命中层级。
            // 注意 fallback 路径不区分 NoC（path_class=3 仅 packet 真值能拿到）。
            const bool l1i_hit = getL1i(acc.core_id).contains(i_cl);
            const bool l2_hit  = !l1i_hit && getL2i(acc.core_id).contains(i_cl);
            const bool l3_hit  = !l1i_hit && !l2_hit && l3_i_lru_.contains(i_cl);
            if (l1i_hit) {
                i_attr.coh = CoherenceAction::L1_HIT;
                i_attr.path_class = 0;
            } else if (l2_hit) {
                i_attr.coh = CoherenceAction::L2_HIT;
                i_attr.path_class = 1;
            } else if (l3_hit) {
                i_attr.coh = CoherenceAction::LLC_HIT;
                i_attr.path_class = 2;
            } else {
                i_attr.coh = CoherenceAction::DRAM;
                i_attr.path_class = 4;
            }
        }
        uint16_t cur_size  = mem_touching ? uint16_t(inst->effSize) : 0;
        uint64_t cur_vaddr = mem_touching ? uint64_t(inst->effAddr) : 0;
        uint64_t cur_paddr = mem_touching ? uint64_t(inst->physEffAddr) : 0;
        emitMicroRecord(inst, oracle_for_micro,
                        cur_vaddr, cur_paddr, cur_size, oracle_filled_micro,
                        i_attr);
    }

    const bool boundary = !si->isMicroop() || si->isLastMicroop();
    if (boundary) {
        acc.is_branch = si->isControl();
        if (emit_macro_) {
            flushMacro(acc, inst);
        }
        acc = MacroAccum{};
    }
}

void
TaoTrace::flushMacro(MacroAccum &acc, const DynInstPtr &lastInst)
{
    if (!acc.valid) return;

    InstrType it = deriveInstrType(acc);
    MemOp     mo = deriveMemOp(acc);
    SyncType  st = classifySyncFromMacro(acc);
    // v2：records.jsonl 中 sync_type 限制在 {NONE, YIELD}（YIELD 走 syscall 路径，
    // 普通 macro 行恒为 NONE）。
    if (st != SyncType::NONE && st != SyncType::YIELD) {
        st = SyncType::NONE;
    }

    // Fix A2: 如果到 flush 时 SharedAttr 仍未填充（典型：x86 store 在 commit
    //   之后才下发 DataAccessComplete；或 micro 走快路径直接 forward），
    //   就用纯 line_states_ 推断。注意 store 的 line state 更新仍要发生，
    //   否则跨核 sharer/owner 视图永远是空的。
    if (mo == MemOp::LOAD || mo == MemOp::STORE || mo == MemOp::ATOMIC) {
        if (!acc.shared_attr.valid && acc.mem_filled) {
            bool is_store = (mo == MemOp::STORE || mo == MemOp::ATOMIC);
            // V4 方案 A：fallback 与 packet 路径统一用 paddr，避免污染共享 LRU
            acc.shared_attr = deriveSharedAttrFromLineState(
                acc.paddr ? acc.paddr : acc.vaddr, acc.core_id, is_store);
        }
    }

    uint64_t branch_target = acc.is_branch ? acc.macro_pc : 0;

    uint64_t macro_t = (acc.last_commit_tick > acc.first_fetch_tick)
                            ? (acc.last_commit_tick - acc.first_fetch_tick) : 0;
    uint64_t macro_cyc = ticksToCycles(macro_t, lastInst);

    uint64_t prev = prev_commit_tick_[acc.thread_id];
    uint64_t exposed_t = (prev == 0) ? 0 : (acc.last_commit_tick - prev);
    uint64_t exposed_cyc = ticksToCycles(exposed_t, lastInst);

    uint64_t branch_pen_cyc = 0;
    auto it_sq = last_squash_tick_.find(acc.thread_id);
    if (acc.is_branch && it_sq != last_squash_tick_.end() &&
        it_sq->second > prev) {
        uint64_t pen_t = (acc.last_commit_tick > it_sq->second)
                            ? (acc.last_commit_tick - it_sq->second) : 0;
        branch_pen_cyc = ticksToCycles(pen_t, lastInst);
        last_squash_tick_.erase(it_sq);
    }

    // V1 多核新增：fetch / execution latency 拆分
    //   exec_t   = completeTick − issueTick （宽口径，ROB head 到 writeback）
    //   fetch_t  = macro_t − exec_t （retire 之前的所有暴露时间归 fetch）
    uint64_t exec_t = 0;
    if (acc.last_issue_tick_delta >= 0 &&
        acc.last_complete_tick_delta >= 0 &&
        acc.last_complete_tick_delta >= acc.last_issue_tick_delta) {
        exec_t = uint64_t(acc.last_complete_tick_delta -
                          acc.last_issue_tick_delta);
    }
    if (exec_t > macro_t) exec_t = macro_t;
    uint64_t fetch_t = macro_t - exec_t;
    uint64_t exec_cyc  = ticksToCycles(exec_t, lastInst);
    uint64_t fetch_cyc = ticksToCycles(fetch_t, lastInst);
    uint8_t  branch_mispred = acc.last_mispredicted ? 1u : 0u;

    // V1 多核新增：access_distance（同一 (thread, cacheline) 上次出现到现在的 macro 间距）
    //   桶：0=未见过, 1=<=4, 2=<=16, 3=<=64, 4=<=256, 5=>256
    uint64_t cur_macro_idx = ++macro_count_per_thread_[acc.thread_id];
    if (mo == MemOp::LOAD || mo == MemOp::STORE || mo == MemOp::ATOMIC) {
        uint64_t cl = acc.vaddr & ~uint64_t{63};
        uint64_t key = (uint64_t(acc.thread_id) << 48) ^ cl;
        auto la = last_access_seq_.find(key);
        if (la == last_access_seq_.end()) {
            acc.access_distance_bucket = 0;
        } else {
            uint64_t d = cur_macro_idx - la->second;
            if      (d <=   4) acc.access_distance_bucket = 1;
            else if (d <=  16) acc.access_distance_bucket = 2;
            else if (d <=  64) acc.access_distance_bucket = 3;
            else if (d <= 256) acc.access_distance_bucket = 4;
            else               acc.access_distance_bucket = 5;
        }
        last_access_seq_[key] = cur_macro_idx;
    }

    // 调度事件（基于 (core, thread) 切换 / 首次出现）
    maybeEmitSchedSwitch(acc.core_id, acc.thread_id, acc.first_seq);
    maybeEmitThreadCreate(acc.core_id, acc.thread_id, acc.first_seq);

    last_seq_per_core_thread_[makeCoreThreadKey(acc.core_id, acc.thread_id)]
        = acc.first_seq;
    last_thread_on_core_[acc.core_id] = int64_t(acc.thread_id);

    // V1 多核新增：分支后旋转 branch_history（taken 近似为 branch_target != fall-through）
    // 注意：必须先读取旋转前的 branch_history，写入 records 后再旋转，
    // 这样当前 macro 的 branch_history 表示"此分支决定之前看到的 16 次历史"
    uint16_t branch_history_before = branch_history_[acc.thread_id];

    writeRecordsLine(acc, it, mo, branch_target, st,
                     branch_history_before, acc.access_distance_bucket);
    writeLabelsLine(acc.core_id, acc.thread_id, acc.first_seq,
                    exposed_cyc, macro_cyc, branch_pen_cyc,
                    fetch_cyc, exec_cyc, branch_mispred);
    writeDiagLine(acc.core_id, acc.thread_id, acc.first_seq,
                  acc.first_fetch_tick, acc.last_commit_tick, prev,
                  last_squash_tick_[acc.thread_id]);

    // V1 多核新增：分支后旋转 branch_history（taken 近似为 branch_target != fall-through）
    if (acc.is_branch) {
        uint16_t hist = branch_history_[acc.thread_id];
        // 用 mispredicted 与否做不到严格 taken/not-taken，使用 branch_target!=0
        // 作为 taken 近似（acc.is_branch 时 branch_target 一定为 macro_pc）
        uint8_t bit = 1; // 当前实现里所有分支都给 1，将来可在 fetch.cc 接入 taken
        hist = uint16_t((hist << 1) | bit);
        branch_history_[acc.thread_id] = hist;
    }

    prev_commit_tick_[acc.thread_id] = acc.last_commit_tick;
}

// -------------------------------------------------------------------------
// JSON 写入（v2 分流）
// -------------------------------------------------------------------------

void
TaoTrace::writeRecordsLine(const MacroAccum &acc, InstrType it, MemOp mo,
                           uint64_t branch_target, SyncType st,
                           uint16_t branch_history,
                           uint8_t access_distance_bucket)
{
    if (!out_records_) return;
    if (!emitGateOpen()) return;  // V9.6 ROI 闸门
    uint64_t cacheline = acc.vaddr & ~uint64_t{63};

    const SharedAttr &a = acc.shared_attr;
    bool mem_event_required = (mo != MemOp::NONE);
    bool sync_required = (st != SyncType::NONE);

    std::fprintf(out_records_,
        "{"
        "\"core_id\":%u,\"thread_id\":%u,\"seq_id\":%" PRIu64 ","
        "\"pc\":%" PRIu64 ",\"opcode\":%d,"
        "\"instr_type\":%u,\"instr_flags\":%u,"
        "\"mem_op\":%u,\"vaddr\":%" PRIu64 ",\"cacheline_addr\":%" PRIu64 ","
        "\"access_size\":%u,"
        "\"is_branch\":%u,\"branch_target\":%" PRIu64 ","
        "\"reg_read_bitmap\":%" PRIu64 ",\"reg_write_bitmap\":%" PRIu64 ","
        "\"branch_history\":%u,\"access_distance\":%u,"
        "\"mesi_before\":%u,\"coh_action\":%u,\"owner_dist\":%u,"
        "\"sharer_bucket\":%u,\"dirty_owner\":%u,\"path_class\":%u,"
        "\"inval_fanout\":%u,\"same_line_recent\":%u,"
        "\"mem_event_required\":%u,\"sync_event_required\":%u,"
        "\"sync_type\":%u"
        "}\n",
        acc.core_id, acc.thread_id, acc.first_seq,
        acc.macro_pc, acc.op_class,
        unsigned(it), 0u,
        unsigned(mo), acc.vaddr, cacheline,
        acc.access_size,
        acc.is_branch ? 1u : 0u, branch_target,
        acc.reg_read_bitmap, acc.reg_write_bitmap,
        unsigned(branch_history), unsigned(access_distance_bucket),
        a.mesi_before, unsigned(a.coh), a.owner_distance_class,
        a.sharer_count_bucket, a.dirty_owner ? 1u : 0u, a.path_class,
        a.inval_fanout_bucket, a.same_line_recent_bucket,
        mem_event_required ? 1u : 0u, sync_required ? 1u : 0u,
        unsigned(st));
}

void
TaoTrace::writeRecordsSyscallLine(uint32_t core_id, uint32_t thread_id,
                                  uint64_t seq_id, uint64_t pc, int op_class,
                                  SyncType st)
{
    if (!out_records_) return;
    if (!emitGateOpen()) return;  // V9.6 ROI 闸门
    bool sync_required = (st != SyncType::NONE);
    std::fprintf(out_records_,
        "{"
        "\"core_id\":%u,\"thread_id\":%u,\"seq_id\":%" PRIu64 ","
        "\"pc\":%" PRIu64 ",\"opcode\":%d,"
        "\"instr_type\":%u,\"instr_flags\":%u,"
        "\"mem_op\":%u,\"vaddr\":0,\"cacheline_addr\":0,\"access_size\":0,"
        "\"is_branch\":0,\"branch_target\":0,"
        "\"reg_read_bitmap\":0,\"reg_write_bitmap\":0,"
        "\"branch_history\":0,\"access_distance\":0,"
        "\"mesi_before\":0,\"coh_action\":0,\"owner_dist\":0,"
        "\"sharer_bucket\":0,\"dirty_owner\":0,\"path_class\":0,"
        "\"inval_fanout\":0,\"same_line_recent\":0,"
        "\"mem_event_required\":0,\"sync_event_required\":%u,"
        "\"sync_type\":%u"
        "}\n",
        core_id, thread_id, seq_id, pc, op_class,
        unsigned(InstrType::SYS), 0u,
        unsigned(MemOp::NONE),
        sync_required ? 1u : 0u, unsigned(st));
}

void
TaoTrace::writeLabelsLine(uint32_t core_id, uint32_t thread_id, uint64_t seq_id,
                          uint64_t exposed_cyc, uint64_t macro_cyc,
                          uint64_t branch_pen_cyc,
                          uint64_t fetch_lat_cyc, uint64_t exec_lat_cyc,
                          uint8_t branch_mispred)
{
    if (!out_labels_) return;
    if (!emitGateOpen()) return;  // V9.6 ROI 闸门
    std::fprintf(out_labels_,
        "{\"core_id\":%u,\"thread_id\":%u,\"seq_id\":%" PRIu64 ","
        "\"exposed_stall_cycles\":%" PRIu64 ","
        "\"macro_cycles\":%" PRIu64 ","
        "\"branch_penalty_cycles\":%" PRIu64 ","
        "\"fetch_latency_cyc\":%" PRIu64 ","
        "\"execution_latency_cyc\":%" PRIu64 ","
        "\"branch_mispred\":%u}\n",
        core_id, thread_id, seq_id,
        exposed_cyc, macro_cyc, branch_pen_cyc,
        fetch_lat_cyc, exec_lat_cyc, unsigned(branch_mispred));
}

void
TaoTrace::writeDiagLine(uint32_t core_id, uint32_t thread_id, uint64_t seq_id,
                        uint64_t fetch_tick, uint64_t commit_tick,
                        uint64_t prev_commit_tick, uint64_t last_squash_tick)
{
    if (!out_diag_) return;
    if (!emitGateOpen()) return;  // V9.6 ROI 闸门
    std::fprintf(out_diag_,
        "{\"core_id\":%u,\"thread_id\":%u,\"seq_id\":%" PRIu64 ","
        "\"fetch_tick\":%" PRIu64 ",\"commit_tick\":%" PRIu64 ","
        "\"prev_commit_tick\":%" PRIu64 ",\"last_squash_tick\":%" PRIu64 "}\n",
        core_id, thread_id, seq_id,
        fetch_tick, commit_tick, prev_commit_tick, last_squash_tick);
}

// -------------------------------------------------------------------------
// V9 micro 粒度：每 commit 一行 records.micro + labels.micro
// -------------------------------------------------------------------------
//
// 字段编排与 atomic_func_trace 对齐（共有字段：core_id/thread_id/micro_seq/
// macro_pc/micro_pc/vaddr/size/is_*/n_src/n_dst/producer_dists/producer_classes），
// 并附加 detailed 独有的 µarch label/oracle 字段：
//   - fetch_tick / issue_tick / complete_tick / commit_tick
//   - mispredicted
//   - mesi_before / coh_oracle / sharer_bucket / owner_dist / dirty_owner /
//     path_class / inval_fanout / same_line_recent / oracle_source
//
// labels.micro.jsonl 仅写关键周期标签，便于训练侧直接 join。

namespace {

inline uint8_t
encodeRegClassValueMicro(int cls_value)
{
    switch (cls_value) {
      case IntRegClass:    return 0;
      case FloatRegClass:  return 1;
      case VecRegClass:    return 2;
      case CCRegClass:     return 3;
      default:             return 255;
    }
}

inline bool
isTrackableRegClassMicro(int cls_value)
{
    switch (cls_value) {
      case IntRegClass:
      case FloatRegClass:
      case VecRegClass:
      case CCRegClass:
        return true;
      default:
        return false;
    }
}

} // anonymous namespace

void
TaoTrace::emitMicroRecord(const DynInstPtr &inst,
                          const SharedAttr &oracle,
                          uint64_t vaddr, uint64_t paddr, uint16_t size,
                          bool oracle_filled,
                          const InstSharedAttr &i_oracle)
{
    if (!emit_micro_) return;
    if (!out_records_micro_ && !out_labels_micro_) return;
    // V9.6 ROI 闸门：require_roi_=true 且 ROI 关闭时短路。
    //   注意：last_writer_per_thread_ / micro_seq_per_thread_ 也都在 emit 侧
    //   维护——ROI 关闭期间不递增；从而 ROI 内首条 µop 仍然得到 micro_seq=1，
    //   producer-distance 在 ROI 段内严格按段内顺序；ROI 跨段衔接由
    //   last_writer 自动处理（跨段的写者 micro_seq 可能 < 当前段，
    //   计算出的 dist 仍然合法且单调）。
    if (!emitGateOpen()) return;

    auto si = inst->staticInst;
    if (!si) return;

    uint32_t core_id   = getCoreId(inst);
    uint32_t thread_id = getTraceThreadId(inst);
    uint64_t micro_seq = ++micro_seq_per_thread_[thread_id];

    // 收集 src/dst regs（仅 trackable class）
    std::vector<std::pair<uint8_t, uint32_t>> srcs, dsts;
    const size_t ns = si->numSrcRegs();
    for (size_t i = 0; i < ns; ++i) {
        const RegId &r = si->srcRegIdx(i);
        int cls = static_cast<int>(r.classValue());
        if (!isTrackableRegClassMicro(cls)) continue;
        srcs.emplace_back(encodeRegClassValueMicro(cls),
                          static_cast<uint32_t>(r.index()));
    }
    const size_t nd = si->numDestRegs();
    for (size_t i = 0; i < nd; ++i) {
        const RegId &r = si->destRegIdx(i);
        int cls = static_cast<int>(r.classValue());
        if (!isTrackableRegClassMicro(cls)) continue;
        dsts.emplace_back(encodeRegClassValueMicro(cls),
                          static_cast<uint32_t>(r.index()));
    }
    std::sort(srcs.begin(), srcs.end());
    srcs.erase(std::unique(srcs.begin(), srcs.end()), srcs.end());
    std::sort(dsts.begin(), dsts.end());
    dsts.erase(std::unique(dsts.begin(), dsts.end()), dsts.end());

    // producer-distance（按距离升序，截断 4 个）
    auto &lw = last_writer_per_thread_[thread_id];
    std::vector<std::pair<uint64_t, uint8_t>> prods;
    prods.reserve(srcs.size());
    for (auto &kv : srcs) {
        uint32_t key = encodeReg(kv.first, kv.second);
        auto it = lw.find(key);
        if (it != lw.end() && it->second > 0 && it->second < micro_seq) {
            uint64_t dist = micro_seq - it->second;
            prods.emplace_back(dist, kv.first);
        }
    }
    std::sort(prods.begin(), prods.end());
    uint32_t p_dists[4] = {0u, 0u, 0u, 0u};
    uint8_t  p_classes[4] = {255u, 255u, 255u, 255u};
    for (size_t k = 0; k < prods.size() && k < 4; ++k) {
        p_dists[k]   = uint32_t(std::min<uint64_t>(prods[k].first, UINT32_MAX));
        p_classes[k] = prods[k].second;
    }

    int is_microop      = si->isMicroop()     ? 1 : 0;
    int is_last_microop = si->isLastMicroop() ? 1 : 0;

    uint64_t macro_pc = inst->pcState().instAddr();
    uint32_t micro_pc = uint32_t(inst->pcState().microPC());
    uint64_t cacheline = (vaddr & ~uint64_t(63));
    // V10 paddr-line：与 mem_events.commit.cacheline_addr (paddr) 同口径，
    //   方便下游用 paddr-key 与 mem_events / Ruby evict 流做 join。
    uint64_t cacheline_paddr = (paddr & ~uint64_t(63));

    uint64_t fetch_tick    = inst->fetchTick != Tick(-1)
                                ? uint64_t(inst->fetchTick) : 0;
    uint64_t issue_tick    = inst->issueTick != -1
                                ? uint64_t(inst->issueTick) : 0;
    uint64_t complete_tick = inst->completeTick != -1
                                ? uint64_t(inst->completeTick) : 0;
    uint64_t commit_tick   = uint64_t(curTick());
    int mispredicted = (si->isControl() && inst->mispredicted()) ? 1 : 0;

    if (out_records_micro_) {
        std::fprintf(out_records_micro_,
            "{\"core_id\":%u,\"thread_id\":%u,\"micro_seq\":%" PRIu64 ","
            "\"seq_num\":%" PRIu64 ","
            "\"macro_pc\":%" PRIu64 ",\"micro_pc\":%u,"
            "\"vaddr\":%" PRIu64 ",\"paddr\":%" PRIu64 ","
            "\"cacheline_addr\":%" PRIu64 ",\"cacheline_paddr\":%" PRIu64 ","
            "\"size\":%u,"
            "\"is_load\":%d,\"is_store\":%d,\"is_atomic\":%d,"
            "\"is_branch\":%d,\"is_branch_cond\":%d,\"is_branch_indirect\":%d,"
            "\"is_call\":%d,\"is_return\":%d,"
            "\"is_int\":%d,\"is_fp\":%d,\"is_simd\":%d,\"is_serialize\":%d,"
            "\"is_microop\":%d,\"is_last_microop\":%d,"
            "\"n_src\":%u,\"n_dst\":%u,"
            "\"producer_dists\":[%u,%u,%u,%u],"
            "\"producer_classes\":[%u,%u,%u,%u],"
            "\"mesi_before\":%u,\"coh_oracle\":%u,"
            "\"sharer_bucket\":%u,\"owner_dist\":%u,\"dirty_owner\":%u,"
            "\"path_class\":%u,\"inval_fanout\":%u,\"same_line_recent\":%u,"
            "\"oracle_source\":%u,"
            "\"i_path_class\":%u,\"i_coh_oracle\":%u,"
            "\"i_mesi_before\":%u,\"i_oracle_source\":%u}\n",
            core_id, thread_id, micro_seq,
            uint64_t(inst->seqNum),
            macro_pc, micro_pc,
            vaddr, paddr, cacheline, cacheline_paddr, unsigned(size),
            si->isLoad() ? 1 : 0,
            si->isStore() ? 1 : 0,
            si->isAtomic() ? 1 : 0,
            si->isControl() ? 1 : 0,
            si->isCondCtrl() ? 1 : 0,
            si->isIndirectCtrl() ? 1 : 0,
            si->isCall() ? 1 : 0,
            si->isReturn() ? 1 : 0,
            si->isInteger() ? 1 : 0,
            si->isFloating() ? 1 : 0,
            si->isVector() ? 1 : 0,
            si->isSerializing() ? 1 : 0,
            is_microop, is_last_microop,
            unsigned(srcs.size()), unsigned(dsts.size()),
            p_dists[0], p_dists[1], p_dists[2], p_dists[3],
            unsigned(p_classes[0]), unsigned(p_classes[1]),
            unsigned(p_classes[2]), unsigned(p_classes[3]),
            unsigned(oracle.mesi_before), unsigned(oracle.coh),
            unsigned(oracle.sharer_count_bucket),
            unsigned(oracle.owner_distance_class),
            unsigned(oracle.dirty_owner ? 1 : 0),
            unsigned(oracle.path_class),
            unsigned(oracle.inval_fanout_bucket),
            unsigned(oracle.same_line_recent_bucket),
            oracle_filled ? 0u : 1u, // 0=packet 1=fallback/none
            unsigned(i_oracle.path_class),
            unsigned(i_oracle.coh),
            unsigned(i_oracle.mesi_before),
            unsigned(i_oracle.oracle_source));
    }

    if (out_labels_micro_) {
        // V9.4：新增 ready_tick 绝对时刻
        //   load/atomic：LSQUnit::writeback 已覆盖 completeTick = cache 数据返回时刻
        //   ALU/branch/store：iew.cc updateExeInstStats 设置 completeTick = execute 完成
        //   兜底（completeTick=0/-1）：退化为 commit_tick
        uint64_t ready_tick = (complete_tick > 0)
                                ? (fetch_tick + complete_tick)
                                : commit_tick;
        int ready_source = (complete_tick > 0) ? 0 : 1; // 0=complete 1=fallback
        std::fprintf(out_labels_micro_,
            "{\"core_id\":%u,\"thread_id\":%u,\"micro_seq\":%" PRIu64 ","
            "\"fetch_tick\":%" PRIu64 ",\"issue_tick\":%" PRIu64 ","
            "\"complete_tick\":%" PRIu64 ",\"commit_tick\":%" PRIu64 ","
            "\"ready_tick\":%" PRIu64 ",\"ready_source\":%d,"
            "\"mispredicted\":%d}\n",
            core_id, thread_id, micro_seq,
            fetch_tick, issue_tick, complete_tick, commit_tick,
            ready_tick, ready_source, mispredicted);
    }

    // 更新 last_writer（micro 粒度）
    for (auto &kv : dsts) {
        uint32_t key = encodeReg(kv.first, kv.second);
        lw[key] = micro_seq;
    }
}

// -------------------------------------------------------------------------
// Probe callbacks
// -------------------------------------------------------------------------

void
TaoTrace::onExecute(const DynInstPtr &inst)
{
    if (isSyscallInst(inst)) {
        captureSyscallState(inst);
    }
}

void
TaoTrace::onCommitStall(const DynInstPtr & /*inst*/)
{
}

void
TaoTrace::onSquash(const DynInstPtr &inst)
{
    const uint32_t tid = getTraceThreadId(inst);
    last_squash_tick_[tid] = curTick();

    if (isSyscallInst(inst) && pending_syscalls_.count(inst->seqNum)) {
        emitSyscallRecord(inst);
    }
    pending_syscalls_.erase(inst->seqNum);
    // Fix A: 防 pending_shared_attr_ 在 squash 路径上泄漏
    pending_shared_attr_.erase((uint64_t(tid) << 48) | uint64_t(inst->seqNum));

    auto it = current_macro_.find(tid);
    if (it != current_macro_.end() &&
        it->second.valid &&
        it->second.first_seq >= inst->seqNum) {
        it->second = MacroAccum{};
    }
}

void
TaoTrace::onDataAccessComplete(
    const std::pair<DynInstPtr, PacketPtr> &p)
{
    const DynInstPtr &inst = p.first;
    const PacketPtr   pkt  = p.second;
    if (!inst || !pkt) return;
    auto si = inst->staticInst;
    if (!si) return;
    if (!(si->isLoad() || si->isStore() || isLockedAtomicMicro(inst))) return;

    const uint32_t tid     = getTraceThreadId(inst);
    const uint32_t core_id = getCoreId(inst);
    const bool is_store    = si->isStore() || isLockedAtomicMicro(inst);

    SharedAttr a = deriveSharedAttr(pkt, core_id, is_store);

    // Fix A: onDataAccessComplete 早于 commit / accumulateMicro，
    //   用 (tid<<48 | seqNum) 缓存到 pending_shared_attr_，
    //   等 accumulateMicro 在 commit 阶段处理同一 micro 时再回填到 acc。
    uint64_t key = (uint64_t(tid) << 48) | uint64_t(inst->seqNum);
    pending_shared_attr_[key] = a;

    // V9.5 d-side：MSHR coalescing 视图（仅记录 outstanding；retire 时
    //   dispatcher 在 accumulateMicro 中调用）。
    const uint64_t cl = uint64_t(pkt->getAddr()) & ~uint64_t(63);
    getL1dMshr(core_id).insert(cl, /*seq=*/inst->seqNum);

    // V9.5 dTLB + page walker：把 paddr 视图也喂给 walker，让 oracle 与
    //   ref_sim 看到同一 LRU 序列。这里仅 touch；fail 时不影响 SharedAttr 推断。
    if (!getDtlb(core_id).translate(uint64_t(pkt->getAddr()))) {
        // miss → walker 走多级页表，结果会同时 touch L1d/L2/L3 LRU
        walker_.walk(uint64_t(pkt->getAddr()),
                     getL1d(core_id), getL2(core_id), l3_lru_);
    }
}

// V9.5 i-cache probe：fetch 完成事件。pkt 已是从 i-cache 返回的 ResponsePkt，
//   payload 可能已经填好；此处仅根据 packet flags + l1i/l2/l3 LRU 推断
//   InstSharedAttr，并写入 last_i_attr_per_core_，等 accumulateMicro 取出。
void
TaoTrace::onInstAccessComplete(const PacketPtr &pkt)
{
    if (!pkt) return;
    if (!uarch_loaded_) return;

    // i-cache packet 的 owner CPU 通过 SenderState 不一定能拿到；这里走
    //   pkt->req->contextId() 取 core_id（与 d-side 一致：每核一个 TaoTrace）。
    uint32_t core_id = 0;
    if (pkt->req && pkt->req->hasContextId()) {
        core_id = uint32_t(pkt->req->contextId());
    }
    // 关键：accumulateMicro 端 i_cl = inst->pcState().instAddr() & ~63，是 vaddr；
    //   而 pkt->getAddr() 是 paddr。两者 key 不一致会导致 fast-path 永远查不到。
    //   优先用 req 上的 vaddr 作为 oracle key；如果 req 没有 vaddr（极少数早期 fault
    //   或 prefetch），退回 paddr 以保留旧行为。
    uint64_t key_addr = uint64_t(pkt->getAddr());
    if (pkt->req && pkt->req->hasVaddr()) {
        key_addr = uint64_t(pkt->req->getVaddr());
    }
    const uint64_t i_cl = key_addr & ~uint64_t(63);

    InstSharedAttr ia;
    ia.valid = true;
    ia.oracle_source = 0; // packet 真值

    // i-side paddr（来自 pkt->getAddr()），仅用于 mem_events.ifetch 输出 cacheline_addr_p。
    const uint64_t i_cl_paddr = uint64_t(pkt->getAddr()) & ~uint64_t(63);

    // mesi_before：i-side 独立 line state（vaddr 域，与 d-side line_states_ 严格隔离）。
    auto it_ls = i_line_states_.find(i_cl);
    if (it_ls != i_line_states_.end()) {
        const LineState &ls = it_ls->second;
        if (ls.owner_core == int32_t(core_id)) {
            ia.mesi_before = ls.mesi;
        } else if (ls.sharers.count(core_id)) {
            ia.mesi_before = 1; // S
        } else {
            ia.mesi_before = 0; // I（远端拥有）
        }
    } else {
        ia.mesi_before = 0;
    }

    // path_class / coh_oracle：l1i → l2_i → l3_i LRU 命中层级（全部 vaddr 域）
    if (pkt->cacheResponding()) {
        ia.coh = pkt->hasSharers() ? CoherenceAction::REMOTE_HIT_CLEAN
                                    : CoherenceAction::REMOTE_HIT_DIRTY;
        ia.path_class = 3; // NoC
        getL1i(core_id).touch(i_cl);
        getL2i(core_id).touch(i_cl);
        l3_i_lru_.touch(i_cl);
    } else {
        bool l1i_hit = getL1i(core_id).touch(i_cl);
        bool l2_hit  = getL2i(core_id).touch(i_cl);
        bool l3_hit  = l3_i_lru_.touch(i_cl);
        if (l1i_hit) {
            ia.coh = CoherenceAction::L1_HIT;
            ia.path_class = 0;
        } else if (l2_hit) {
            ia.coh = CoherenceAction::L2_HIT;
            ia.path_class = 1;
        } else if (l3_hit) {
            ia.coh = CoherenceAction::LLC_HIT;
            ia.path_class = 2;
        } else {
            ia.coh = CoherenceAction::DRAM;
            ia.path_class = 4;
        }
    }

    // i-side TLB + walker（vaddr 域；与 d-side dtlb_/walker_ 严格隔离）。
    //   ITLB 的 translate 输入是 vaddr，与 d-side dtlb_(paddr) 不同源。
    if (!getItlb(core_id).translate(i_cl)) {
        i_walker_.walk(i_cl,
                       getL1i(core_id), getL2i(core_id), l3_i_lru_);
    }

    // i-side MSHR：记录 outstanding，retire 由本函数自身负责
    //   （fetch 不像 commit 有显式 retire 锚点；将本次访问视作单次 insert+retire）
    getL1iMshr(core_id).insert(i_cl, /*seq=*/global_mem_event_counter_);
    getL1iMshr(core_id).retire(i_cl);

    // i-side line state 状态机（fetch=只读 → I→E）
    {
        LineState &ls = i_line_states_[i_cl];
        if (ls.mesi == 0) {
            ls.mesi = 2; // E
            ls.owner_core = int32_t(core_id);
            ls.sharers.clear();
            ls.sharers.insert(core_id);
        } else {
            ls.sharers.insert(core_id);
            if (ls.sharers.size() >= 2) {
                ls.mesi = 1; // S
                ls.owner_core = -1;
            }
        }
    }

    last_i_attr_per_core_[core_id][i_cl] = ia;

    // V4 mem_events 流：i-side 也输出 inst-fetch 行，便于 ref_sim 同步 LRU。
    // V9.6：ROI gate 短路 ifetch emit；上面 LRU/TLB/walker/MSHR 已 always-update。
    if (global_mem_events_ && emitGateOpen()) {
        // V10：cacheline_addr 保留为 vaddr-line（兼容旧 ref_sim/compare_ifetch 逻辑），
        //   同时新增 cacheline_addr_v / cacheline_addr_p 双字段，让下游可双口径 join。
        std::fprintf(global_mem_events_,
            "{\"seq\":%lu,\"event_type\":\"ifetch\",\"core_id\":%u,"
            "\"cacheline_addr\":%lu,"
            "\"cacheline_addr_v\":%lu,\"cacheline_addr_p\":%lu,"
            "\"cache_level\":4,"
            "\"i_path_class\":%u,\"i_coh_oracle\":%u,"
            "\"i_mesi_before\":%u,"
            "\"commit_tick\":%lu}\n",
            (unsigned long)global_mem_event_counter_++, core_id,
            (unsigned long)i_cl,
            (unsigned long)i_cl, (unsigned long)i_cl_paddr,
            unsigned(ia.path_class), unsigned(ia.coh),
            unsigned(ia.mesi_before),
            (unsigned long)curTick());
    }
}

void
TaoTrace::onCommit(const DynInstPtr &inst)
{
    if (isSyscallInst(inst) && pending_syscalls_.count(inst->seqNum)) {
        emitSyscallRecord(inst);
        pending_syscalls_.erase(inst->seqNum);
        return;
    }
    accumulateMicro(inst);
    pending_syscalls_.erase(inst->seqNum);
}

} // namespace o3
} // namespace gem5
