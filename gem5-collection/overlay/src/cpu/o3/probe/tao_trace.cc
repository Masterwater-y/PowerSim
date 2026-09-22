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
#include <iterator>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "arch/x86/insts/static_inst.hh"
#include "arch/x86/faults.hh"
#include "arch/x86/pagetable.hh"
#include "arch/x86/regs/ccr.hh"
#include "arch/x86/regs/float.hh"
#include "arch/x86/regs/int.hh"
#include "arch/x86/regs/misc.hh"
#include "base/trace.hh"
#include "cpu/base.hh"
#include "cpu/o3/dyn_inst.hh"
#include "cpu/o3/lsq.hh"
#include "cpu/reg_class.hh"
#include "cpu/thread_context.hh"
#include "debug/TaoTrace.hh"
#include "kern/linux/linux.hh"
#include "mem/request.hh"
#include "mem/taotrace_response.hh"
#include "sim/core.hh"
#include "sim/cur_tick.hh"
#include "sim/eventq.hh"
#include "sim/sim_exit.hh"
#include "sim/system.hh"

namespace gem5
{
namespace o3
{

// Fix B: 跨实例共享的 line 状态机（每核一个 TaoTrace 实例 → 必须共享）
std::unordered_map<uint64_t, TaoTrace::LineState> TaoTrace::line_states_;
std::unordered_map<uint64_t, uint32_t>            TaoTrace::recent_line_count_;
std::multimap<TaoTrace::FunctionalReturnKey,
              TaoTrace::PendingFunctionalReturn>
    TaoTrace::pending_functional_returns_;

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
std::FILE *TaoTrace::global_wrong_path_oracle_ = nullptr;
bool TaoTrace::wrong_path_header_written_ = false;
uint64_t TaoTrace::global_wrong_path_episode_id_ = 0;
std::unordered_map<const CPU *, TaoTrace *> TaoTrace::wrong_path_instances_;

// V9.6 ROI 闸门（进程级共享）：
//   require_roi_=false 时退化为 V9.5 全程 emit；require_roi_=true 时仅
//   active_count_>0 期间放行 emit。所有 TaoTrace 实例共用同一对 (active_,
//   active_count_)，确保多核 m5_work_begin/end 观察一致。
bool     TaoTrace::require_roi_      = false;
bool     TaoTrace::roi_active_       = false;
bool     TaoTrace::cpl_measurement_active_ = false;
uint64_t TaoTrace::cpl_measurement_begin_tick_ = 0;
uint64_t TaoTrace::cpl_measurement_end_tick_ = 0;
uint64_t TaoTrace::roi_active_count_ = 0;
std::unordered_map<uint32_t, uint64_t>
    TaoTrace::roi_active_count_per_core_;
std::unordered_map<uint32_t, uint64_t>
    TaoTrace::cpl_measurement_begin_tick_per_core_;
std::unordered_map<uint32_t, uint64_t>
    TaoTrace::cpl_measurement_end_tick_per_core_;
std::FILE *TaoTrace::global_roi_boundaries_ = nullptr;
std::FILE *TaoTrace::global_cpl_class_ = nullptr;
std::FILE *TaoTrace::global_page_fault_events_ = nullptr;
uint32_t TaoTrace::live_instances_ = 0;
uint64_t TaoTrace::global_functional_user_target_ = 0;
uint32_t TaoTrace::functional_target_instances_ = 0;
bool TaoTrace::functional_target_exit_scheduled_ = false;
TaoTrace *TaoTrace::functional_target_drain_owner_ = nullptr;
uint64_t TaoTrace::functional_target_drain_polls_ = 0;
std::unordered_set<const TaoTrace *> TaoTrace::functional_target_reached_;
std::unordered_set<TaoTrace *> TaoTrace::functional_warmup_instances_;
bool TaoTrace::initial_pte_state_captured_ = false;
uint64_t TaoTrace::initial_pte_root_ = 0;
std::unordered_map<uint64_t, bool> TaoTrace::initial_pte_present_;
bool TaoTrace::measurement_pte_state_captured_ = false;
uint64_t TaoTrace::measurement_pte_root_ = 0;
std::unordered_map<uint64_t, bool> TaoTrace::measurement_pte_present_;

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

// ---------------------------------------------------------------------------
// FST schema-7 直写：64-byte hot stream 后追加 one-row-per-syscall 的
// 128-byte sparse metadata table。布局与 FastSim BinarySyscallMetadataV1
// 逐字节一致。
//   probe 在 records.micro 走 FST 模式时用它替代文本 JSONL，省去 ~10x 文本
//   中间文件与离线转换 pass。任何字段/常量改动都必须与该转换器保持一致，
//   否则会破坏与已采集 FST 的兼容性。
// ---------------------------------------------------------------------------
constexpr int16_t  kFstSyscallOpClass = -1;
constexpr uint32_t kFstDestClassMarker = 1u << 31;
constexpr uint32_t kFstVpageBits  = 12;
constexpr uint64_t kFstVpageBytes = uint64_t(1) << kFstVpageBits;
constexpr uint32_t kFstVersion = 7;
constexpr uint64_t kFstFeatureVpageTokens = uint64_t(1) << 0;
constexpr uint64_t kFstFeatureSyscall     = uint64_t(1) << 1;
constexpr uint64_t kFstFeatureDestClass   = uint64_t(1) << 2;
constexpr uint64_t kFstFeatureSyscallMetadata = uint64_t(1) << 3;
constexpr uint64_t kFstFeaturePrivilege       = uint64_t(1) << 4;
constexpr uint64_t kFstLinuxX86_64 = 1;
constexpr uint32_t kFstVirtualPageMapVersion = 1;
constexpr uint32_t kFstAddressSpaceMapVersion = 1;
constexpr uint32_t kFstVirtualPageMapPhysicalValid = 1u << 0;
constexpr uint32_t kFstVirtualPageMapInitialPteStateValid = 1u << 1;
constexpr uint32_t kFstVirtualPageMapInitialPtePresent = 1u << 2;
constexpr uint32_t kFstVirtualPageMapMeasurementPteStateValid = 1u << 3;
constexpr uint32_t kFstVirtualPageMapMeasurementPtePresent = 1u << 4;
constexpr uint32_t kFstVirtualPageMapMeasurementBoundaryInflightFault =
    1u << 5;
constexpr uint32_t kFstInstructionMapVersion = 2;
constexpr uint32_t kFstInstructionOperandsComplete = 1u << 1;
constexpr uint8_t kFstInstructionOperandsValid = 1u << 0;
constexpr uint32_t kFstInstructionIsaX86_64 = 1;
constexpr uint16_t kSyscallArgumentsValid = 1u << 0;
constexpr uint16_t kSyscallReturnValueValid = 1u << 1;
constexpr uint16_t kSyscallFailureValid = 1u << 2;
constexpr uint16_t kSyscallErrnoValid = 1u << 3;
constexpr uint16_t kSyscallPreTimestampValid = 1u << 4;
constexpr uint16_t kSyscallPostTimestampValid = 1u << 5;
constexpr uint16_t kSyscallPreCpuValid = 1u << 6;
constexpr uint16_t kSyscallPostCpuValid = 1u << 7;
constexpr uint16_t kSyscallMaybeBlockingValid = 1u << 8;
constexpr uint8_t kSyscallFailed = 1u << 0;
constexpr uint8_t kSyscallMaybeBlocking = 1u << 1;

enum FstFlag : uint16_t {
    kFstRetires        = 1u << 0,
    kFstLoad           = 1u << 1,
    kFstStore          = 1u << 2,
    kFstAtomic         = 1u << 3,
    kFstBranch         = 1u << 4,
    kFstConditional    = 1u << 5,
    kFstIndirect       = 1u << 6,
    kFstCall           = 1u << 7,
    kFstReturn         = 1u << 8,
    kFstTaken          = 1u << 9,
    kFstMicroOp        = 1u << 10,
    kFstLastMicroOp    = 1u << 11,
    kFstPhysicalAddr   = 1u << 12,
    kFstSerialize      = 1u << 13,
    kFstBranchOutcome  = 1u << 14,
    kFstVpageToken     = 1u << 15,
};

struct FstRecord {
    uint64_t pc = 0;
    uint64_t address = 0;
    uint64_t target = 0;
    uint64_t next_pc = 0;
    uint32_t producer_dists[4] = {0, 0, 0, 0};
    uint16_t size = 0;
    uint16_t flags = kFstRetires;
    int16_t  op_class = 0;
    uint8_t  n_src = 0;
    uint8_t  n_dst = 0;
    uint8_t  producer_classes[4] = {255, 255, 255, 255};
    uint32_t reserved = 0;
};
static_assert(sizeof(FstRecord) == 64, "FST record must be 64 bytes");

struct FstSyscallMetadataV1 {
    uint64_t record_ordinal = 0;
    uint64_t syscall_ordinal = 0;
    uint64_t thread_id = 0;
    uint64_t syscall_number = 0;
    uint64_t arguments[6] = {0, 0, 0, 0, 0, 0};
    uint64_t return_value_raw = 0;
    uint64_t pre_timestamp_us = 0;
    uint64_t post_timestamp_us = 0;
    uint32_t errno_value = 0;
    uint32_t pre_cpu = 0;
    uint32_t post_cpu = 0;
    uint16_t valid_fields = 0;
    uint8_t argument_count = 0;
    uint8_t flags = 0;
    uint64_t reserved = 0;
};
static_assert(sizeof(FstSyscallMetadataV1) == 128,
              "FST syscall metadata row must be 128 bytes");

struct FstHeader {
    char     magic[8] = {'F', 'S', 'T', 'R', 'C', '0', '1', '\0'};
    uint32_t version = kFstVersion;
    uint32_t header_size = 72;
    uint32_t record_size = 64;
    uint32_t core_id = 0;
    uint64_t record_count = 0;
    uint64_t feature_flags = 0;
    uint64_t reserved[4] = {0, 0, 0, 0};
};
static_assert(sizeof(FstHeader) == 72, "FST header must be 72 bytes");

struct FstVirtualPageMapHeaderV1 {
    char magic[8] = {'F', 'S', 'T', 'V', 'M', 'P', '1', '\0'};
    uint32_t version = kFstVirtualPageMapVersion;
    uint32_t header_size = 48;
    uint32_t entry_size = 32;
    uint32_t core_id = 0;
    uint64_t source_record_count = 0;
    uint64_t entry_count = 0;
    uint32_t page_offset_bits = kFstVpageBits;
    uint32_t reserved = 0;
};
static_assert(sizeof(FstVirtualPageMapHeaderV1) == 48,
              "FST virtual-page map header must be 48 bytes");

struct FstVirtualPageMapEntryV1 {
    uint32_t token = 0;
    uint32_t flags = 0;
    uint64_t first_record_ordinal = 0;
    uint64_t virtual_page = 0;
    uint64_t physical_page = 0;
};
static_assert(sizeof(FstVirtualPageMapEntryV1) == 32,
              "FST virtual-page map entry must be 32 bytes");

struct FstAddressSpaceMapHeaderV1 {
    char magic[8] = {'F', 'S', 'T', 'A', 'S', 'M', '1', '\0'};
    uint32_t version = kFstAddressSpaceMapVersion;
    uint32_t header_size = 48;
    uint32_t entry_size = 16;
    uint32_t core_id = 0;
    uint64_t source_record_count = 0;
    uint64_t entry_count = 0;
    uint64_t reserved = 0;
};
static_assert(sizeof(FstAddressSpaceMapHeaderV1) == 48,
              "FST address-space map header must be 48 bytes");

struct FstAddressSpaceMapEntryV1 {
    uint64_t record_ordinal = 0;
    uint64_t address_space_id = 0;
};
static_assert(sizeof(FstAddressSpaceMapEntryV1) == 16,
              "FST address-space map entry must be 16 bytes");

enum FstStaticFlag : uint16_t {
    kFstStaticBranch = 1u << 0,
    kFstStaticConditional = 1u << 1,
    kFstStaticIndirect = 1u << 2,
    kFstStaticCall = 1u << 3,
    kFstStaticReturn = 1u << 4,
    kFstStaticDirectTargetValid = 1u << 5,
    kFstStaticMemory = 1u << 6,
};

struct FstInstructionMapHeaderV2 {
    char magic[8] = {'F', 'S', 'T', 'I', 'M', 'P', '2', '\0'};
    uint32_t version = kFstInstructionMapVersion;
    uint32_t header_size = 48;
    uint32_t entry_size = 64;
    uint32_t core_id = 0;
    uint64_t source_record_count = 0;
    uint64_t entry_count = 0;
    // Bit 0 (executable-map complete) intentionally remains clear because
    // TaoTrace observes committed PCs rather than decoding the full image.
    uint32_t flags = kFstInstructionOperandsComplete;
    uint32_t isa = kFstInstructionIsaX86_64;
};
static_assert(sizeof(FstInstructionMapHeaderV2) == 48,
              "FST instruction-map header must be 48 bytes");

struct FstInstructionMapEntryV2 {
    uint64_t pc = 0;
    uint64_t fallthrough_pc = 0;
    uint64_t direct_target = 0;
    uint16_t flags = 0;
    uint8_t size = 0;
    uint8_t semantic_flags = kFstInstructionOperandsValid;
    uint8_t reserved[4] = {0, 0, 0, 0};
    uint64_t read_register_mask[2] = {0, 0};
    uint64_t write_register_mask[2] = {0, 0};
};
static_assert(sizeof(FstInstructionMapEntryV2) == 64,
              "FST instruction-map v2 entry must be 64 bytes");

// Map gem5's x86 renameable register identities into the shared .imap v2
// namespace. Micro-temporaries and non-renameable misc state are not
// architectural dependency operands and are intentionally omitted.
bool
fstCanonicalRegister(const RegId &reg, uint32_t &canonical)
{
    if (reg.is(IntRegClass)) {
        if (reg.index() >= X86ISA::int_reg::NumArchRegs) return false;
        canonical = uint32_t(reg.index());
        return true;
    }
    if (reg.is(CCRegClass)) {
        canonical = 17; // RFLAGS dependency group
        return true;
    }
    if (reg.is(FloatRegClass)) {
        const auto index = reg.index();
        if (index < X86ISA::float_reg::XmmBase) {
            // gem5 aliases MMX and x87 storage. The portable namespace does
            // the same because they are not independently renameable here.
            canonical = 24 + uint32_t(index);
            return true;
        }
        if (index < X86ISA::float_reg::MicrofpBase) {
            canonical = 32 +
                uint32_t(index - X86ISA::float_reg::XmmBase) / 2;
            return true;
        }
        return false;
    }
    if (reg.is(VecRegClass) && reg.index() < 32) {
        canonical = 32 + uint32_t(reg.index());
        return true;
    }
    return false;
}

std::map<uint64_t, uint8_t>
parseSyscallArgCounts(const std::string &spec)
{
    std::map<uint64_t, uint8_t> result;
    std::stringstream input(spec);
    std::string item;
    while (std::getline(input, item, ',')) {
        if (item.empty()) continue;
        const auto separator = item.find(':');
        if (separator == std::string::npos) {
            throw std::runtime_error(
                "TaoTrace syscall_arg_counts entry lacks ':' : " + item);
        }
        const uint64_t number = std::stoull(item.substr(0, separator));
        const unsigned long count = std::stoul(item.substr(separator + 1));
        if (count > 6) {
            throw std::runtime_error(
                "TaoTrace syscall_arg_counts exceeds six: " + item);
        }
        if (!result.emplace(number, uint8_t(count)).second) {
            throw std::runtime_error(
                "TaoTrace duplicate syscall_arg_counts entry: " + item);
        }
    }
    return result;
}

bool
isMaybeBlockingSyscall(uint64_t number)
{
    // Portable best-effort hint, deliberately narrower than claiming that an
    // invocation actually slept. Linux x86-64 numbers only; absence remains
    // invalid, matching drmemtrace MAYBE_BLOCKING_SYSCALL semantics.
    switch (number) {
      case 0:    // read
      case 1:    // write
      case 7:    // poll
      case 23:   // select
      case 35:   // nanosleep
      case 43:   // accept
      case 45:   // recvfrom
      case 47:   // recvmsg
      case 202:  // futex
      case 230:  // clock_nanosleep
      case 232:  // epoll_wait
      case 270:  // pselect6
      case 271:  // ppoll
      case 281:  // epoll_pwait
        return true;
      default:
        return false;
    }
}

struct GuestPteSnapshot {
    uint64_t root = 0;
    uint64_t table_pages = 0;
    uint64_t present_pages = 0;
    uint64_t nonpresent_pages = 0;
    std::unordered_map<uint64_t, bool> present;
    // Keep raw values only for non-present leaves. Zero means an empty leaf;
    // a non-zero value is a Linux software encoding (for example swap or a
    // migration/protection entry) and can have different fault semantics.
    std::unordered_map<uint64_t, uint64_t> nonpresent_raw;
};

GuestPteSnapshot
readGuestPteSnapshot(gem5::ThreadContext *tc, uint64_t root = 0)
{
    if (!tc) {
        throw std::runtime_error(
            "TaoTrace guest PTE snapshot lacks a ThreadContext");
    }
    X86ISA::Efer efer =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Efer);
    if (!efer.lma) {
        throw std::runtime_error(
            "TaoTrace guest PTE snapshot requires x86-64 long mode");
    }
    GuestPteSnapshot snapshot;
    if (root == 0) {
        X86ISA::CR3 cr3 =
            tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3);
        root = uint64_t(cr3.longPdtb) << 12;
    }
    snapshot.root = root;
    snapshot.table_pages = 1;

    auto &proxy = tc->getSystemPtr()->physProxy;
    const auto read_pte = [&proxy](uint64_t table, uint64_t index) {
        return proxy.read<X86ISA::PageTableEntry>(
            table + index * sizeof(X86ISA::PageTableEntry));
    };
    const auto record_page = [&snapshot](uint64_t virtual_page,
                                         bool present, uint64_t raw) {
        const auto inserted = snapshot.present.emplace(
            virtual_page, present);
        if (!inserted.second && inserted.first->second != present) {
            throw std::runtime_error(
                "TaoTrace guest PTE snapshot contains conflicting leaves");
        }
        if (!inserted.second) return;
        if (present) {
            ++snapshot.present_pages;
        } else {
            ++snapshot.nonpresent_pages;
            snapshot.nonpresent_raw.emplace(virtual_page, raw);
        }
    };

    // Scan the lower canonical half. Absent upper-level entries remain
    // unknown instead of expanding enormous unmapped ranges. Every leaf slot
    // below an existing user page-table path is exact, including a zero PTE.
    for (uint64_t l4 = 0; l4 < 256; ++l4) {
        const auto pml4e = read_pte(snapshot.root, l4);
        if (!pml4e.p || !pml4e.u) continue;
        const uint64_t l3_table = uint64_t(pml4e.base) << 12;
        ++snapshot.table_pages;
        for (uint64_t l3 = 0; l3 < 512; ++l3) {
            const auto pdpte = read_pte(l3_table, l3);
            if (!pdpte.p || !pdpte.u) continue;
            const uint64_t l3_vpage = (l4 << 27) | (l3 << 18);
            if (pdpte.ps) {
                for (uint64_t offset = 0; offset < (uint64_t(1) << 18);
                     ++offset) {
                    record_page(l3_vpage | offset, true, uint64_t(pdpte));
                }
                continue;
            }
            const uint64_t l2_table = uint64_t(pdpte.base) << 12;
            ++snapshot.table_pages;
            for (uint64_t l2 = 0; l2 < 512; ++l2) {
                const auto pde = read_pte(l2_table, l2);
                if (!pde.p || !pde.u) continue;
                const uint64_t l2_vpage = l3_vpage | (l2 << 9);
                if (pde.ps) {
                    for (uint64_t offset = 0; offset < 512; ++offset) {
                    record_page(l2_vpage | offset, true, uint64_t(pde));
                    }
                    continue;
                }
                const uint64_t l1_table = uint64_t(pde.base) << 12;
                ++snapshot.table_pages;
                for (uint64_t l1 = 0; l1 < 512; ++l1) {
                    const auto pte = read_pte(l1_table, l1);
                    record_page(l2_vpage | l1, pte.p, uint64_t(pte));
                }
            }
        }
    }
    return snapshot;
}

void
writeGuestPteSnapshot(const std::string &path, const char *schema,
                      const GuestPteSnapshot &snapshot,
                      const std::vector<std::pair<uint32_t, uint64_t>>
                          *boundary_inflight_faults = nullptr)
{
    std::ofstream metadata(path, std::ios::trunc);
    if (!metadata) {
        throw std::runtime_error(
            "TaoTrace failed to open guest PTE metadata: " + path);
    }
    std::vector<std::pair<uint64_t, uint64_t>> nonpresent(
        snapshot.nonpresent_raw.begin(), snapshot.nonpresent_raw.end());
    std::sort(nonpresent.begin(), nonpresent.end());
    const auto encoded_nonpresent = std::count_if(
        nonpresent.begin(), nonpresent.end(),
        [](const auto &entry) { return entry.second != 0; });
    metadata << "{\n"
             << "  \"schema\": \"" << schema << "\",\n"
             << "  \"page_size\": 4096,\n"
             << "  \"cr3_root_physical\": " << snapshot.root << ",\n"
             << "  \"page_table_pages_scanned\": "
             << snapshot.table_pages << ",\n"
             << "  \"known_pages\": " << snapshot.present.size()
             << ",\n"
             << "  \"present_pages\": " << snapshot.present_pages
             << ",\n"
             << "  \"nonpresent_pages\": "
             << snapshot.nonpresent_pages << ",\n"
             << "  \"zero_nonpresent_pages\": "
             << (snapshot.nonpresent_pages - encoded_nonpresent) << ",\n"
             << "  \"encoded_nonpresent_pages\": "
             << encoded_nonpresent << ",\n"
             << "  \"nonpresent_details\": [";
    for (size_t index = 0; index < nonpresent.size(); ++index) {
        const auto &[virtual_page, raw_pte] = nonpresent[index];
        metadata << (index == 0 ? "\n" : ",\n")
                 << "    {\"virtual_page\": " << virtual_page
                 << ", \"raw_pte\": " << raw_pte << "}";
    }
    if (!nonpresent.empty()) metadata << "\n  ";
    metadata << "]";
    if (boundary_inflight_faults) {
        auto sorted_faults = *boundary_inflight_faults;
        std::sort(sorted_faults.begin(), sorted_faults.end());
        metadata << ",\n  \"boundary_inflight_fault_count\": "
                 << sorted_faults.size()
                 << ",\n  \"boundary_inflight_faults\": [";
        for (size_t index = 0; index < sorted_faults.size(); ++index) {
            const auto &[core_id, virtual_page] = sorted_faults[index];
            metadata << (index == 0 ? "\n" : ",\n")
                     << "    {\"core_id\": " << core_id
                     << ", \"virtual_page\": " << virtual_page << "}";
        }
        if (!sorted_faults.empty()) metadata << "\n  ";
        metadata << "]";
    }
    metadata << "\n}\n";
    if (!metadata) {
        throw std::runtime_error(
            "TaoTrace failed to write guest PTE metadata: " + path);
    }
}

// FST header core_id 必须等于 manifest 的 core 序号，而 manifest 序号来自
//   下游 CORE_RE = (?:switch|cores)(\d+)\.core（见 scripts/gem5_fs_roi.py）。
//   restore 时 switched-in O3 核的 cpuId() 可能为 0，不能用；改从 probe 的
//   name()（形如 board.processor.switch3.core.tao_trace）解析同一序号，与
//   离线转换器 --core 参数逐字节一致。解析失败回退 -1（由调用方兜底）。
int32_t
parseFstCoreIdFromName(const std::string &probe_name)
{
    for (const char *tag : {"switch", "cores"}) {
        const std::string needle = std::string(".") + tag;
        std::size_t pos = probe_name.find(needle);
        while (pos != std::string::npos) {
            std::size_t digit = pos + needle.size();
            std::size_t end = digit;
            while (end < probe_name.size() &&
                   std::isdigit((unsigned char)probe_name[end])) {
                ++end;
            }
            // 必须紧跟 ".core"，避免误配其它含 switch/cores 的名字。
            if (probe_name.compare(end, 5, ".core") == 0) {
                if (end > digit) {
                    return int32_t(std::stoul(
                        probe_name.substr(digit, end - digit)));
                }
                return 0;  // "switch.core" 无数字即单核 core 0
            }
            pos = probe_name.find(needle, pos + 1);
        }
    }
    return -1;
}

} // namespace

TaoTrace::TaoTrace(const TaoTraceParams &params)
    : ProbeListenerObject(params),
      emit_macro_(params.emit_macro),
      emit_micro_(params.emit_micro),
      emit_mem_events_(params.emit_mem_events),
      measure_cpl_(params.measure_cpl),
      emit_native_response_jsonl_(params.emit_native_response_jsonl),
      native_response_anomaly_limit_(params.native_response_anomaly_limit),
      emit_wrong_path_oracle_(params.emit_wrong_path_oracle),
      functional_user_only_(params.functional_user_only),
      functional_include_kernel_(params.functional_include_kernel),
      functional_warmup_(params.functional_warmup),
      functional_user_target_(params.functional_user_target),
      require_roi_param_(params.require_roi),
      syscall_arg_counts_(parseSyscallArgCounts(params.syscall_arg_counts)),
      output_dir_(params.output_dir),
      uarch_profile_path_(params.uarch_profile_path)
{
    live_instances_ += 1;
    wrong_path_core_id_ = parseFstCoreIdFromName(name());
    if (functional_user_only_ && functional_include_kernel_) {
        throw std::runtime_error(
            "TaoTrace functional_user_only and functional_include_kernel "
            "are mutually exclusive");
    }
    if (functional_include_kernel_ && !measure_cpl_) {
        throw std::runtime_error(
            "TaoTrace functional_include_kernel requires measure_cpl=true "
            "so idle CPL0 execution can be excluded");
    }
    if (functional_user_target_ > 0) {
        if (!functionalTraceEnabled() || !emit_micro_) {
            throw std::runtime_error(
                "TaoTrace functional_user_target requires "
                "one functional trace mode and emit_micro=true");
        }
        if (global_functional_user_target_ != 0 &&
            global_functional_user_target_ != functional_user_target_) {
            throw std::runtime_error(
                "all TaoTrace instances must use the same "
                "functional_user_target");
        }
        global_functional_user_target_ = functional_user_target_;
        functional_target_instances_ += 1;
    }
    if (functional_warmup_) {
        if (!functionalTraceEnabled() || !emit_micro_ ||
            functional_user_target_ == 0) {
            throw std::runtime_error(
                "TaoTrace functional_warmup requires a functional trace mode, "
                "emit_micro, and a non-zero functional_user_target");
        }
        functional_warmup_instances_.insert(this);
    }
    // V9.6：require_roi_ 进程级共享。任意一个 TaoTrace 实例置位即生效；
    //   实践中所有 TaoTrace SimObject 通过同一份 Python 配置生成，参数一致。
    if (require_roi_param_) {
        require_roi_ = true;
    }
    // 记录流物理格式：默认 jsonl；"fst" 时 records.micro 直写 FST 二进制。
    if (toLower(params.trace_format) == "fst") {
        trace_format_ = TraceFormat::FST;
    }
    // The bit declares the configured record encoding, not whether this
    // particular core happened to retire a CPL0 instruction in the window.
    if (functional_include_kernel_) {
        fst_feature_flags_ |= kFstFeaturePrivilege;
    }
    openOutput();
    if (functionalTraceEnabled()) {
        registerExitCallback([this]() { writeFunctionalBoundary(); });
    }
}

TaoTrace::~TaoTrace()
{
    for (auto it = wrong_path_instances_.begin();
         it != wrong_path_instances_.end();) {
        if (it->second == this) {
            it = wrong_path_instances_.erase(it);
        } else {
            ++it;
        }
    }
    functional_warmup_instances_.erase(this);
    for (auto it = pending_functional_returns_.begin();
         it != pending_functional_returns_.end();) {
        if (it->second.owner == this) {
            it = pending_functional_returns_.erase(it);
        } else {
            ++it;
        }
    }
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
    finalizeNativeResponseDiagnostic();
    closeFile(out_native_response_diag_);
    // V4：若本实例的 mem_events 正是进程级 sink，先清掉全局指针，避免
    //   teardown 期间 Ruby 析构再次回调 traceCacheEvent 时出现悬空 FILE*。
    if (global_mem_events_ == out_mem_events_) {
        global_mem_events_ = nullptr;
    }
    closeFile(out_mem_events_);
    closeFile(out_records_micro_);
    closeFile(out_labels_micro_);
    // FST v7：先追加 sparse syscall metadata table，再回填头部。
    if (out_fst_) {
        finalizeFst();
        std::fflush(out_fst_);
        std::fclose(out_fst_);
        out_fst_ = nullptr;
    }
    if (functional_user_target_ > 0) {
        functional_target_reached_.erase(this);
        if (functional_target_instances_ > 0) {
            functional_target_instances_ -= 1;
        }
    }
    if (live_instances_ > 0) {
        live_instances_ -= 1;
    }
    if (live_instances_ == 0 && global_roi_boundaries_) {
        std::fflush(global_roi_boundaries_);
        std::fclose(global_roi_boundaries_);
        global_roi_boundaries_ = nullptr;
    }
    if (live_instances_ == 0 && global_cpl_class_) {
        std::fflush(global_cpl_class_);
        std::fclose(global_cpl_class_);
        global_cpl_class_ = nullptr;
    }
    if (live_instances_ == 0 && global_page_fault_events_) {
        std::fflush(global_page_fault_events_);
        std::fclose(global_page_fault_events_);
        global_page_fault_events_ = nullptr;
    }
    if (live_instances_ == 0 && global_wrong_path_oracle_) {
        std::fflush(global_wrong_path_oracle_);
        std::fclose(global_wrong_path_oracle_);
        global_wrong_path_oracle_ = nullptr;
        wrong_path_header_written_ = false;
    }
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
        if (trace_format_ == TraceFormat::FST) {
            // 直写 FST 二进制：只开一个 records.micro.fst，不产 labels.micro。
            //   文件名保留 records.micro 语义前缀，扩展名换成 .fst，方便下游按
            //   core*.records.micro.fst 识别；header 先写 count=0 占位，析构时回填。
            std::snprintf(path, sizeof(path), "%s/%s.records.micro.fst",
                          output_dir_.c_str(), name().c_str());
            fst_path_ = path;
            out_fst_ = std::fopen(path, "w+b");
            if (!out_fst_) {
                warn("TaoTrace: failed to open %s", path);
            } else {
                // header core_id 从 probe name 解析（与下游 manifest CORE_RE
                //   和离线转换器 --core 同口径）；解析失败回退 0 并告警。
                int32_t parsed = parseFstCoreIdFromName(name());
                if (parsed < 0) {
                    warn("TaoTrace: cannot parse core id from '%s'; "
                         "FST header core_id defaults to 0", name().c_str());
                    parsed = 0;
                }
                fst_core_id_ = parsed;
                writeFstHeader();
                // gem5 在 m5 exit 时通常不析构 SimObject，因此 exit callback
                // 必须追加 syscall table 并回填最终 header。
                //   注册 exit callback 保证 clean exit 时头部被最终化并 flush。
                registerExitCallback([this]() {
                    if (out_fst_) {
                        finalizeFst();
                        std::fflush(out_fst_);
                    }
                });
            }
        } else {
            out_records_micro_ = open_one("records.micro");
            out_labels_micro_  = open_one("labels.micro");
        }
    }
    if (require_roi_ && !global_roi_boundaries_) {
        std::snprintf(path, sizeof(path), "%s/roi_boundaries.jsonl",
                      output_dir_.c_str());
        global_roi_boundaries_ = std::fopen(path, "w");
        if (!global_roi_boundaries_)
            warn("TaoTrace: failed to open %s", path);
    }
    if (measure_cpl_ && !global_cpl_class_) {
        std::snprintf(path, sizeof(path), "%s/oracle",
                      output_dir_.c_str());
        ::mkdir(path, 0755);
        std::snprintf(path, sizeof(path), "%s/oracle/cpl_class.jsonl",
                      output_dir_.c_str());
        global_cpl_class_ = std::fopen(path, "w");
        if (!global_cpl_class_) {
            warn("TaoTrace: failed to open %s", path);
        }
    }
    if (measure_cpl_ && !global_page_fault_events_) {
        std::snprintf(path, sizeof(path),
                      "%s/oracle/page_fault_events.jsonl",
                      output_dir_.c_str());
        global_page_fault_events_ = std::fopen(path, "w");
        if (!global_page_fault_events_) {
            warn("TaoTrace: failed to open %s", path);
        } else {
            std::fprintf(global_page_fault_events_,
                "{\"schema\":\"taotrace-x86-page-fault-events-v1\"," 
                "\"record\":\"metadata\",\"oracle_only\":true," 
                "\"address_unit\":\"byte\",\"tick_unit\":" 
                "\"gem5_tick\"}\n");
            std::fflush(global_page_fault_events_);
        }
    }
    if (measure_cpl_) {
        registerExitCallback([this]() { finalizeCplMeasurement(); });
        const int32_t native_core_id = parseFstCoreIdFromName(name());
        if (functional_warmup_ && native_core_id >= 0) {
            TaoTraceNativeAccessRegistry::prepareContext(
                uint32_t(native_core_id));
            TaoTraceFrontendRegistry::prepareContext(
                uint32_t(native_core_id));
        }
        std::snprintf(path, sizeof(path),
                      "%s/oracle/native-summary-core%d.json",
                      output_dir_.c_str(),
                      native_core_id >= 0 ? native_core_id : 0);
        native_response_summary_path_ = path;
        if (emit_native_response_jsonl_) {
            std::snprintf(path, sizeof(path),
                          "%s/oracle/native-response-core%d.jsonl",
                          output_dir_.c_str(),
                          native_core_id >= 0 ? native_core_id : 0);
            out_native_response_diag_ = std::fopen(path, "w");
            if (!out_native_response_diag_) {
                warn("TaoTrace: failed to open %s", path);
            } else {
            std::fprintf(out_native_response_diag_,
                "{\"schema\":\"taotrace-native-response-v7\","
                "\"record\":\"metadata\",\"oracle_only\":true,"
                "\"fst_input\":false,\"lifecycle_join\":"
                "\"context-id-inst-seq-num\","
                "\"target_stop\":\"committed-native-drain\","
                "\"hierarchy_source\":\"ruby-slicc-controller-actions\","
                "\"hierarchy_identity_transport\":"
                "\"context-id-inst-seq-num-no-request-retention\","
                "\"hierarchy_request_semantics\":"
                "\"sequencer-mandatory-queue-enqueue\","
                "\"measurement_boundary_semantics\":"
                "\"preboundary-inflight-ledger-retire-cleanup\","
                "\"lifecycle_tick_semantics\":"
                "\"sequencer-acceptance-and-hit-callback-gem5-tick\","
                "\"ruby_memory_fetch_semantics\":"
                "\"l2-to-directory-fetch-not-dram-transaction\","
                "\"memory_read_transaction_semantics\":"
                "\"accepted-ruby-memory-port-read-packet\","
                "\"external_hit_spelling\":"
                "\"sequencer-controller-miss\"}\n");
            std::fflush(out_native_response_diag_);
            }
        }
        registerExitCallback(
            [this]() { finalizeNativeResponseDiagnostic(); });
    }
    if (emit_wrong_path_oracle_ && !global_wrong_path_oracle_) {
        std::snprintf(path, sizeof(path), "%s/oracle",
                      output_dir_.c_str());
        ::mkdir(path, 0755);
        std::snprintf(path, sizeof(path), "%s/oracle/wrong_path.jsonl",
                      output_dir_.c_str());
        global_wrong_path_oracle_ = std::fopen(path, "w");
        if (!global_wrong_path_oracle_) {
            warn("TaoTrace: failed to open %s", path);
        }
    }
    if (global_wrong_path_oracle_ && !wrong_path_header_written_) {
        std::fprintf(global_wrong_path_oracle_,
            "{\"schema\":\"taotrace-wrong-path-oracle-v3\"," 
            "\"record\":\"metadata\",\"oracle_only\":true," 
            "\"fst_input\":false,\"scope\":\"functional-measurement\"," 
            "\"cpl_attribution\":\"decoded-x86-mode\"," 
            "\"late_data_complete_attribution\":true," 
            "\"tick_unit\":\"gem5_tick\"}\n");
        std::fflush(global_wrong_path_oracle_);
        wrong_path_header_written_ = true;
    }
}

// V28.1 per-core ROI hook。全局计数只描述 first-begin/last-end 生命周期；
// emit 必须查询该 core 自己的 depth，避免先结束的 core 把 join/futex 尾部
// 混入仍在执行的其他 core 的 ROI。
void
TaoTrace::traceSourceRoiCheckpoint(gem5::ThreadContext *tc)
{
    if (!tc || !tc->getSystemPtr()) return;
    X86ISA::Efer efer =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Efer);
    if (!efer.lma) {
        warn("TaoTrace: source ROI checkpoint ignored outside x86-64 "
             "long mode");
        return;
    }
    X86ISA::CR3 cr3 =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3);
    const uint64_t root = uint64_t(cr3.longPdtb) << 12;
    if (root == 0) {
        throw std::runtime_error(
            "TaoTrace source ROI checkpoint captured a zero guest CR3");
    }
    tc->getSystemPtr()->setSourceRoiAddressSpaceId(root);
    inform("TaoTrace: source ROI checkpoint captured guest PTE root=%#lx",
           (unsigned long)root);
}

void
TaoTrace::traceWorkBegin(uint32_t core_id, uint64_t workid,
                         uint64_t threadid)
{
    uint64_t &core_depth = roi_active_count_per_core_[core_id];
    if (core_depth == 0) {
        cpl_measurement_begin_tick_per_core_[core_id] = uint64_t(curTick());
        cpl_measurement_end_tick_per_core_.erase(core_id);
    }
    core_depth += 1;
    roi_active_count_ += 1;
    roi_active_ = (roi_active_count_ > 0);
    cpl_measurement_active_ = true;
    cpl_measurement_begin_tick_ = uint64_t(curTick());
    if (global_roi_boundaries_) {
        std::fprintf(global_roi_boundaries_,
            "{\"event\":\"begin\",\"core_id\":%u,\"workid\":%lu,"
            "\"threadid\":%lu,\"tick\":%lu,\"core_depth\":%lu,"
            "\"global_depth\":%lu}\n",
            core_id, (unsigned long)workid, (unsigned long)threadid,
            (unsigned long)curTick(), (unsigned long)core_depth,
            (unsigned long)roi_active_count_);
        std::fflush(global_roi_boundaries_);
    }
}

void
TaoTrace::traceWorkEnd(uint32_t core_id, uint64_t workid,
                       uint64_t threadid)
{
    uint64_t &core_depth = roi_active_count_per_core_[core_id];
    const bool matched = core_depth > 0;
    if (matched) {
        core_depth -= 1;
        if (core_depth == 0) {
            cpl_measurement_end_tick_per_core_[core_id] = uint64_t(curTick());
        }
        if (roi_active_count_ > 0) {
            roi_active_count_ -= 1;
        }
    } else {
        warn("TaoTrace: unmatched WORKEND on core %u", core_id);
    }
    roi_active_ = (roi_active_count_ > 0);
    if (!roi_active_) cpl_measurement_active_ = false;
    if (global_roi_boundaries_) {
        std::fprintf(global_roi_boundaries_,
            "{\"event\":\"end\",\"core_id\":%u,\"workid\":%lu,"
            "\"threadid\":%lu,\"tick\":%lu,\"core_depth\":%lu,"
            "\"global_depth\":%lu,\"matched\":%u}\n",
            core_id, (unsigned long)workid, (unsigned long)threadid,
            (unsigned long)curTick(), (unsigned long)core_depth,
            (unsigned long)roi_active_count_, matched ? 1u : 0u);
        std::fflush(global_roi_boundaries_);
    }
}

void
TaoTrace::traceMeasurementBegin()
{
    // The serial marker is the one process-wide functional boundary. Capture
    // guest page-table state before opening any per-core measurement stream.
    if (!functional_warmup_instances_.empty()) {
        captureMeasurementPteState();
    }
    for (auto *trace : functional_warmup_instances_) {
        trace->beginFunctionalMeasurement();
    }
    cpl_measurement_active_ = true;
    cpl_measurement_begin_tick_ = uint64_t(curTick());
    cpl_measurement_end_tick_ = 0;
}

void
TaoTrace::traceMeasurementEnd()
{
    cpl_measurement_end_tick_ = uint64_t(curTick());
    cpl_measurement_active_ = false;
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
    if (emitGateOpen(core_id)) {
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
    using KernelEntryListener =
        ProbeListenerArg<TaoTrace, KernelEntryEvent>;

    connectListener<DynInstListener>(this, "Fetch",       &TaoTrace::onFetch);
    connectListener<DynInstListener>(this, "Rename",      &TaoTrace::onRename);
    connectListener<DynInstListener>(this, "Dispatch",    &TaoTrace::onDispatch);
    connectListener<DynInstListener>(this, "PreCommit",   &TaoTrace::onPreCommit);
    connectListener<DynInstListener>(this, "Commit",      &TaoTrace::onCommit);
    connectListener<DynInstListener>(this, "CommitStall", &TaoTrace::onCommitStall);
    connectListener<DynInstListener>(this, "Squash",      &TaoTrace::onSquash);
    connectListener<DynInstListener>(this, "Execute",     &TaoTrace::onExecute);
    connectListener<DynInstListener>(this, "ToCommit",    &TaoTrace::onToCommit);
    connectListener<KernelEntryListener>(
        this, "KernelEntry", &TaoTrace::onKernelEntry);
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

bool
TaoTrace::isSyscallMacro(const DynInstPtr &inst)
{
    if (!inst->macroop) return false;
    const void *key = inst->macroop.get();
    auto cached = syscall_macro_cache_.find(key);
    if (cached != syscall_macro_cache_.end()) return cached->second;
    const std::string name = toLower(inst->macroop->getName());
    const std::string disasm = toLower(
        inst->macroop->disassemble(inst->pcState().instAddr()));
    const bool result =
        containsAny(name, {"syscall", "sysenter", "int80"}) ||
        containsAny(disasm, {"syscall", "sysenter", "int80"});
    syscall_macro_cache_.emplace(key, result);
    return result;
}

bool
TaoTrace::isSyscallBoundary(const DynInstPtr &inst)
{
    if (inst->macroop) {
        if (!inst->staticInst || !inst->staticInst->isFirstMicroop()) {
            return false;
        }
        return isSyscallInst(inst) || isSyscallMacro(inst);
    }
    return isSyscallInst(inst);
}

bool
TaoTrace::isInterruptReturn(const DynInstPtr &inst) const
{
    if (!inst || !inst->staticInst || !inst->staticInst->isLastMicroop()) {
        return false;
    }
    const StaticInstPtr macro = inst->macroop;
    if (!macro) return false;
    const std::string name = toLower(macro->getName());
    if (name.find("iret") != std::string::npos) return true;
    const std::string disasm = toLower(
        macro->disassemble(inst->pcState().instAddr()));
    return disasm.find("iret") != std::string::npos;
}

bool
TaoTrace::isIdleInstruction(const DynInstPtr &inst) const
{
    if (!inst || !inst->staticInst ||
        (inst->staticInst->isMicroop() &&
         !inst->staticInst->isLastMicroop())) {
        return false;
    }
    const StaticInstPtr macro = inst->macroop;
    const StaticInstPtr candidate = macro ? macro : inst->staticInst;
    const std::string name = toLower(candidate->getName());
    if (containsAny(name, {"hlt", "mwait"})) return true;
    const std::string disasm = toLower(
        candidate->disassemble(inst->pcState().instAddr()));
    return containsAny(disasm, {"hlt", "mwait"});
}

bool
TaoTrace::isPollIdleInstruction(const DynInstPtr &inst) const
{
    if (!inst || !inst->staticInst ||
        (inst->staticInst->isMicroop() &&
         !inst->staticInst->isLastMicroop())) {
        return false;
    }
    const StaticInstPtr macro = inst->macroop;
    const StaticInstPtr candidate = macro ? macro : inst->staticInst;
    auto x86 = dynamic_cast<const X86ISA::X86StaticInst *>(candidate.get());
    if (x86 && x86->machInst.opcode.type == X86ISA::OneByteOpcode &&
        uint8_t(x86->machInst.opcode.op) == 0x90 &&
        x86->machInst.legacy.rep) {
        // Intel PAUSE is F3 90. gem5 currently decodes it as NOP and even its
        // disassembly says "nop", so name/disassembly matching cannot see it.
        return true;
    }
    const std::string name = toLower(candidate->getName());
    if (name.find("pause") != std::string::npos) return true;
    const std::string disasm = toLower(
        candidate->disassemble(inst->pcState().instAddr()));
    return disasm.find("pause") != std::string::npos;
}

bool
TaoTrace::isUserInstruction(const DynInstPtr &inst) const
{
    return instructionCpl(inst) == 3;
}

uint8_t
TaoTrace::instructionCpl(const DynInstPtr &inst) const
{
    if (inst && inst->staticInst) {
        auto x86 = dynamic_cast<const X86ISA::X86StaticInst *>(
            inst->staticInst.get());
        if (x86) return uint8_t(x86->machInst.mode.cpl);
    }
    return inst && inst->tcBase() &&
        inst->tcBase()->getIsaPtr()->inUserMode() ? 3 : 0;
}

bool
TaoTrace::pteAddressSpaceMatches(const DynInstPtr &inst) const
{
    if (!initial_pte_state_captured_ || !inst || !inst->tcBase()) {
        return false;
    }
    auto *tc = inst->tcBase();
    X86ISA::Efer efer =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Efer);
    if (!efer.lma) return false;
    X86ISA::CR3 cr3 =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3);
    return (uint64_t(cr3.longPdtb) << 12) == initial_pte_root_;
}

void
TaoTrace::maybeCaptureInitialPteState(const DynInstPtr &inst)
{
    if (!functionalTraceEnabled() || !emit_micro_ || !functionalCaptureActive() ||
        !isUserInstruction(inst) || !inst || !inst->tcBase()) {
        return;
    }

    auto *tc = inst->tcBase();
    if (initial_pte_root_checked_) return;

    X86ISA::Efer efer =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Efer);
    if (!efer.lma) {
        throw std::runtime_error(
            "TaoTrace initial PTE snapshot requires x86-64 long mode");
    }
    X86ISA::CR3 cr3 =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3);
    const uint64_t root = uint64_t(cr3.longPdtb) << 12;
    const uint64_t checkpoint_root =
        tc->getSystemPtr()->getSourceRoiAddressSpaceId();
    if (initial_pte_state_captured_) {
        if (root != initial_pte_root_) {
            // A single-process FS workload still coexists with login shells
            // and service processes.  On a high core count, an otherwise
            // idle core can briefly fetch one of those CPL3 streams during
            // functional warmup. FST records every CR3 identity, but this
            // snapshot still belongs to one root. Leave PTE state unknown for
            // the other root instead of merging page tables or aborting the
            // run; token identity remains available.
            if (!initial_pte_root_mismatch_warned_) {
                warn("TaoTrace: ignoring non-target CR3 root=%#lx on %s; "
                     "target=%#lx and PTE state remains unknown",
                     (unsigned long)root, name(),
                     (unsigned long)initial_pte_root_);
                initial_pte_root_mismatch_warned_ = true;
            }
            return;
        }
        // ThreadContext objects are stable for the simulation lifetime. Keep
        // a per-probe handle only after this core has actually observed the
        // selected target address space; the measurement barrier rechecks
        // its live CR3 before choosing an owner.
        pte_snapshot_tc_ = tc;
        initial_pte_root_checked_ = true;
        return;
    }

    // New ROI checkpoints serialize the CR3 of the exact marker thread. Use
    // that root even if a guest daemon wins the first userspace fetch after
    // restore. A zero value preserves compatibility with legacy checkpoints.
    const uint64_t snapshot_root =
        checkpoint_root != 0 ? checkpoint_root : root;
    auto snapshot = readGuestPteSnapshot(tc, snapshot_root);
    if (snapshot.root != snapshot_root) {
        throw std::runtime_error(
            "TaoTrace CR3 changed while taking initial PTE snapshot");
    }
    writeGuestPteSnapshot(
        output_dir_ + "/initial-pte-state.json",
        "fastsim-gem5-initial-pte-v1", snapshot);
    initial_pte_root_ = snapshot.root;
    initial_pte_present_ = std::move(snapshot.present);
    pte_snapshot_tc_ = tc;
    initial_pte_state_captured_ = true;
    initial_pte_root_checked_ = true;
    inform("TaoTrace: initial guest PTE snapshot root=%#lx live_root=%#lx "
           "known=%lu present=%lu nonpresent=%lu source=%s",
           (unsigned long)snapshot.root,
           (unsigned long)root,
           (unsigned long)initial_pte_present_.size(),
           (unsigned long)snapshot.present_pages,
           (unsigned long)snapshot.nonpresent_pages,
           checkpoint_root != 0 ? "checkpoint-cr3" : "first-user-cr3");
}

void
TaoTrace::captureMeasurementPteState()
{
    if (measurement_pte_state_captured_) return;
    if (!initial_pte_state_captured_) {
        throw std::runtime_error(
            "TaoTrace measurement PTE snapshot lacks initial guest state");
    }

    std::vector<std::pair<uint32_t, uint64_t>> boundary_inflight_faults;
    for (auto *trace : functional_warmup_instances_) {
        if (!trace) continue;
        trace->measurement_boundary_inflight_fault_pages_.clear();
        if (!trace->premeasurement_page_fault_pending_) continue;
        trace->measurement_boundary_inflight_fault_pages_.insert(
            trace->premeasurement_page_fault_virtual_page_);
        const int32_t parsed_core = trace->fst_core_id_ >= 0
            ? trace->fst_core_id_ : parseFstCoreIdFromName(trace->name());
        boundary_inflight_faults.emplace_back(
            parsed_core >= 0 ? uint32_t(parsed_core) : UINT32_MAX,
            trace->premeasurement_page_fault_virtual_page_);
    }

    TaoTrace *owner = nullptr;
    uint32_t owner_core = UINT32_MAX;
    for (auto *trace : functional_warmup_instances_) {
        if (!trace || !trace->pte_snapshot_tc_) continue;
        const int32_t parsed_core = trace->fst_core_id_ >= 0
            ? trace->fst_core_id_ : parseFstCoreIdFromName(trace->name());
        const uint32_t candidate_core = parsed_core >= 0
            ? uint32_t(parsed_core) : UINT32_MAX;
        if (!owner || candidate_core < owner_core) {
            owner = trace;
            owner_core = candidate_core;
        }
    }
    if (!owner) {
        throw std::runtime_error(
            "TaoTrace measurement PTE snapshot lacks a physical-memory proxy");
    }

    // A ThreadContext is only the route to System::physProxy here. Linux may
    // deschedule the target while its serial WORKBEGIN marker drains, so walk
    // the serialized target root rather than requiring a live matching CR3.
    auto snapshot = readGuestPteSnapshot(
        owner->pte_snapshot_tc_, initial_pte_root_);
    if (snapshot.root != initial_pte_root_) {
        throw std::runtime_error(
            "TaoTrace guest CR3 changed at the measurement boundary");
    }
    writeGuestPteSnapshot(
        owner->output_dir_ + "/measurement-pte-state.json",
        "fastsim-gem5-measurement-pte-v1", snapshot,
        &boundary_inflight_faults);
    measurement_pte_root_ = snapshot.root;
    measurement_pte_present_ = std::move(snapshot.present);
    measurement_pte_state_captured_ = true;
    inform("TaoTrace: measurement guest PTE snapshot root=%#lx known=%lu "
           "present=%lu nonpresent=%lu boundary_inflight_faults=%lu",
           (unsigned long)snapshot.root,
           (unsigned long)measurement_pte_present_.size(),
           (unsigned long)snapshot.present_pages,
           (unsigned long)snapshot.nonpresent_pages,
           (unsigned long)boundary_inflight_faults.size());
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

void
TaoTrace::noteCplSyscallEntry(const DynInstPtr &inst)
{
    if (!measure_cpl_) return;
    if (cpl_core_id_ < 0) {
        cpl_core_id_ = parseFstCoreIdFromName(name());
        if (cpl_core_id_ < 0) cpl_core_id_ = 0;
    }
    if (!cplMeasurementActive()) return;
    if (!cpl_syscall_pending_) {
        cpl_pending_syscall_pc_ = inst->pcState().instAddr();
        cpl_pending_syscall_thread_id_ = getTraceThreadId(inst);
        cpl_pending_sysnum_ = uint64_t(
            inst->tcBase()->getReg(X86ISA::int_reg::Rax));
    }
    cpl_syscall_pending_ = true;
}

bool
TaoTrace::cplMeasurementActive() const
{
    // The architectural WORKBEGIN instruction is observed before the guest's
    // serial marker line reaches Terminal::writeData().  Functional warmup
    // uses that serial line as its exact measurement boundary, so do not let
    // the earlier per-core/global ROI gate leak warmup commits into the CPL
    // oracle.  Likewise, once this core has frozen its oracle at the trace
    // target, slower cores must not extend its measurement window.
    if (functional_warmup_ &&
        (!functional_measurement_started_ || cpl_finalized_)) {
        return false;
    }
    if (!require_roi_) return cpl_measurement_active_;
    if (cpl_core_id_ < 0) return false;
    auto it = roi_active_count_per_core_.find(uint32_t(cpl_core_id_));
    return it != roi_active_count_per_core_.end() && it->second > 0;
}

uint64_t
TaoTrace::cplMeasurementBeginTick() const
{
    if (require_roi_ && cpl_core_id_ >= 0) {
        auto it = cpl_measurement_begin_tick_per_core_.find(
            uint32_t(cpl_core_id_));
        if (it != cpl_measurement_begin_tick_per_core_.end()) {
            return it->second;
        }
    }
    return cpl_measurement_begin_tick_;
}

uint64_t
TaoTrace::cplMeasurementEndTick() const
{
    if (require_roi_ && cpl_core_id_ >= 0) {
        auto it = cpl_measurement_end_tick_per_core_.find(
            uint32_t(cpl_core_id_));
        if (it != cpl_measurement_end_tick_per_core_.end()) {
            return it->second;
        }
    }
    return cpl_measurement_end_tick_ != 0
        ? cpl_measurement_end_tick_ : uint64_t(curTick());
}

void
TaoTrace::initializeCpl(uint64_t tick, uint64_t clock_period_ticks,
                        bool initial_user)
{
    if (cpl_initialized_) return;
    cpl_initialized_ = true;
    // Align the oracle to the functional trace, which starts at this core's
    // first user record (or at an exact from-user exception before that
    // record retires).  A core can already be in CPL0 when the global ROI
    // marker opens; charging that unobserved tail creates a marker-skew error.
    cpl_first_tick_ = tick;
    cpl_last_tick_ = cpl_first_tick_;
    cpl_clock_period_ticks_ = std::max<uint64_t>(1, clock_period_ticks);
    // The CPL oracle starts on this core's first measured event, not on the
    // process-wide serial marker.  Freeze the pretracked fetch population at
    // the same boundary so status cycles and request lifecycles cover exactly
    // the CPI numerator window.
    if (cpl_core_id_ >= 0) {
        TaoTraceFrontendRegistry::enableContext(uint32_t(cpl_core_id_));
    }
    cpl_cycle_class_ = initial_user
        ? CplCycleClass::USER : CplCycleClass::UNKNOWN_KERNEL;
    cpl_parent_stack_.clear();
    if (!initial_user) {
        ++cpl_entries_[static_cast<size_t>(CplCycleClass::UNKNOWN_KERNEL)];
    }
}

void
TaoTrace::settleCpl(uint64_t tick)
{
    if (!cpl_initialized_ || tick <= cpl_last_tick_) return;
    const uint64_t delta = tick - cpl_last_tick_;
    cpl_ticks_[static_cast<size_t>(cpl_cycle_class_)] += delta;
    if (cpl_cycle_class_ == CplCycleClass::SYSCALL) {
        cpl_syscalls_[cpl_active_sysnum_].kernel_ticks += delta;
    }
    cpl_last_tick_ = tick;
}

void
TaoTrace::onKernelEntry(const KernelEntryEvent &event)
{
    if (event.fault == NoFault) return;
    const auto page_fault =
        std::dynamic_pointer_cast<X86ISA::PageFault>(event.fault);
    if (functional_warmup_ && !functional_measurement_started_ &&
        event.fromUser && page_fault) {
        premeasurement_page_fault_pending_ = true;
        premeasurement_page_fault_virtual_page_ =
            uint64_t(page_fault->getAddress()) >> kFstVpageBits;
    }
    if (!measure_cpl_) return;
    if (cpl_core_id_ < 0) cpl_core_id_ = int32_t(event.coreId);
    if (event.coreId != uint32_t(cpl_core_id_) ||
        !cplMeasurementActive()) {
        return;
    }
    // The user-only trace has no causal record for a privileged span already
    // in progress when its gate opens.  Wait for the first user commit.  A
    // precise exception/IRQ originating from user mode is retained because
    // it belongs to the faulting user instruction that will later retire.
    if (!cpl_initialized_ && !event.fromUser) return;

    CplCycleClass event_class = CplCycleClass::UNKNOWN_KERNEL;
    const char *fault_name = event.fault->name();
    if (event.source == KernelEntrySource::ExternalInterrupt) {
        event_class = CplCycleClass::IRQ;
    } else if (page_fault ||
               (fault_name && std::strcmp(fault_name, "Page-Fault") == 0)) {
        event_class = CplCycleClass::PAGE_FAULT;
    } else if (fault_name &&
               (std::strcmp(fault_name, "Re-execution fault") == 0 ||
                std::strcmp(fault_name, "System call retry fault") == 0 ||
                std::strcmp(fault_name,
                            "Generic HTM failure fault") == 0 ||
                // gem5 debug micro-ops (for example MOVNTDQ's unsupported
                // non-temporal-hint warning) advance the guest PC in the
                // host and never enter the guest kernel.  They are simulator
                // control flow, not CPL0 execution.
                std::strcmp(fault_name, "warn fault") == 0 ||
                std::strcmp(fault_name, "hack fault") == 0 ||
                std::strcmp(fault_name, "inform fault") == 0)) {
        return;
    }
    if (event_class == CplCycleClass::UNKNOWN_KERNEL) {
        ++cpl_unknown_sources_[std::string("fault:") +
            (fault_name ? fault_name : "<null>")];
    }

    if (event_class == CplCycleClass::PAGE_FAULT &&
        global_page_fault_events_) {
        if (page_fault) {
            const uint64_t address = uint64_t(page_fault->getAddress());
            std::fprintf(global_page_fault_events_,
                "{\"record\":\"page_fault\",\"core_id\":%u," 
                "\"thread_id\":%u,\"tick\":%lu,\"from_user\":%s," 
                "\"virtual_address\":%lu,\"virtual_page\":%lu," 
                "\"error_code\":%lu}\n",
                event.coreId, unsigned(event.threadId),
                (unsigned long)event.tick,
                event.fromUser ? "true" : "false",
                (unsigned long)address,
                (unsigned long)(address >> kFstVpageBits),
                (unsigned long)page_fault->getErrorCode());
            std::fflush(global_page_fault_events_);
        }
    }

    initializeCpl(uint64_t(event.tick), uint64_t(event.clockPeriodTicks),
                  event.fromUser);
    settleCpl(uint64_t(event.tick));
    cpl_parent_stack_.push_back(cpl_cycle_class_);
    cpl_cycle_class_ = event_class;
    ++cpl_entries_[static_cast<size_t>(event_class)];

    if (event_class == CplCycleClass::IRQ) {
        auto x86_fault = std::dynamic_pointer_cast<X86ISA::X86FaultBase>(
            event.fault);
        if (x86_fault) ++cpl_irq_vectors_[x86_fault->getVector()];
    }
}

void
TaoTrace::emitFunctionalSyscallMarker(
    const DynInstPtr &inst, uint64_t syscall_number)
{
    if (!inst || !inst->tcBase() || !functionalTraceEnabled() || !emit_micro_ ||
        !functionalCaptureActive()) {
        return;
    }
    if (functionalTargetReached()) {
        return;
    }
    if (cpl_core_id_ < 0) {
        cpl_core_id_ = parseFstCoreIdFromName(name());
        if (cpl_core_id_ < 0) cpl_core_id_ = 0;
    }
    const uint64_t pc = inst->pcState().instAddr();
    const uint32_t source_context_id = getTraceThreadId(inst);
    PendingSyscall syscall;
    const auto pending = pending_syscalls_.find(inst->seqNum);
    if (pending != pending_syscalls_.end()) {
        syscall = pending->second;
    } else {
        auto tc = inst->tcBase();
        syscall.nr = syscall_number;
        syscall.arg0 = tc->getReg(X86ISA::int_reg::Rdi);
        syscall.arg1 = tc->getReg(X86ISA::int_reg::Rsi);
        syscall.arg2 = tc->getReg(X86ISA::int_reg::Rdx);
        syscall.arg3 = tc->getReg(X86ISA::int_reg::R10);
        syscall.arg4 = tc->getReg(X86ISA::int_reg::R8);
        syscall.arg5 = tc->getReg(X86ISA::int_reg::R9);
        syscall.user_rsp = tc->getReg(X86ISA::int_reg::Rsp);
        syscall.address_space =
            tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3) &
            ~uint64_t(0xfff);
        syscall.return_pc = pc + 2;
        syscall.pre_timestamp_us =
            uint64_t(curTick()) / sim_clock::as_int::us;
        syscall.pre_cpu = uint32_t(cpl_core_id_);
    }
    // The PreCommit refresh below is authoritative.  The ThreadContext seen
    // after commitHead may already reflect a syscall micro-op destination,
    // so use the caller's number only as a compatibility fallback.
    const uint64_t resolved_syscall_number =
        pending != pending_syscalls_.end() ? syscall.nr : syscall_number;
    syscall.nr = resolved_syscall_number;
    const std::array<uint64_t, 6> arguments{
        syscall.arg0, syscall.arg1, syscall.arg2,
        syscall.arg3, syscall.arg4, syscall.arg5};
    const auto configured_args =
        syscall_arg_counts_.find(resolved_syscall_number);
    const bool maybe_blocking =
        isMaybeBlockingSyscall(resolved_syscall_number);
    if (out_fst_) {
        noteFstAddressSpace(syscall.address_space);
        FstRecord record;
        record.pc = pc;
        record.address = resolved_syscall_number;
        record.next_pc = record.pc + 2;
        record.flags = uint16_t(record.flags | kFstSerialize);
        record.op_class = kFstSyscallOpClass;
        for (auto &encoded : record.producer_classes) encoded = 7;
        record.reserved = kFstDestClassMarker;
        const uint64_t record_ordinal = fst_record_count_;
        if (std::fwrite(&record, sizeof(record), 1, out_fst_) != 1) {
            throw std::runtime_error("TaoTrace failed to write FST syscall");
        }
        ++fst_record_count_;
        fst_feature_flags_ |= kFstFeatureSyscall | kFstFeatureDestClass;

        FunctionalSyscallMetadata metadata;
        metadata.record_ordinal = record_ordinal;
        metadata.syscall_ordinal = fst_syscall_metadata_.size();
        metadata.number = resolved_syscall_number;
        metadata.pre_timestamp_us = syscall.pre_timestamp_us;
        metadata.pre_cpu = syscall.pre_cpu;
        metadata.valid_fields = uint16_t(
            kSyscallPreTimestampValid | kSyscallPreCpuValid);
        if (configured_args != syscall_arg_counts_.end()) {
            metadata.argument_count = configured_args->second;
            metadata.arguments = arguments;
            metadata.valid_fields = uint16_t(
                metadata.valid_fields | kSyscallArgumentsValid);
        }
        if (maybe_blocking) {
            metadata.valid_fields = uint16_t(
                metadata.valid_fields | kSyscallMaybeBlockingValid);
            metadata.flags = uint8_t(
                metadata.flags | kSyscallMaybeBlocking);
        }
        fst_syscall_metadata_.push_back(metadata);
        pending_functional_returns_.emplace(
            FunctionalReturnKey{
                syscall.address_space, syscall.user_rsp,
                syscall.return_pc},
            PendingFunctionalReturn{
                this, fst_syscall_metadata_.size() - 1});
        noteFunctionalRecordEmitted(true, true);
        return;
    }
    if (out_records_micro_) {
        std::fprintf(out_records_micro_,
            "{\"core_id\":%d,\"source_context_id\":%u,"
            "\"address_space_id\":%lu,"
            "\"macro_pc\":%lu,\"is_syscall\":1,"
            "\"syscall_number\":%lu,\"is_serialize\":1,"
            "\"syscall_pre_timestamp_us\":%lu,"
            "\"syscall_pre_cpu\":%u,"
            "\"op_class\":0,\"is_microop\":0,\"is_last_microop\":1,"
            "\"n_src\":0,\"n_dst\":0,"
            "\"producer_dists\":[0,0,0,0],"
            "\"producer_classes\":[255,255,255,255],"
            "\"destination_class_counts\":[0,0,0,0]",
            cpl_core_id_, source_context_id,
            (unsigned long)syscall.address_space, (unsigned long)pc,
            (unsigned long)resolved_syscall_number,
            (unsigned long)syscall.pre_timestamp_us, syscall.pre_cpu);
        if (configured_args != syscall_arg_counts_.end()) {
            std::fputs(",\"syscall_args\":[", out_records_micro_);
            for (uint8_t index = 0; index < configured_args->second; ++index) {
                if (index != 0) std::fputc(',', out_records_micro_);
                std::fprintf(out_records_micro_, "%lu",
                    (unsigned long)arguments[index]);
            }
            std::fputc(']', out_records_micro_);
        }
        if (maybe_blocking) {
            std::fputs(",\"syscall_maybe_blocking\":true",
                       out_records_micro_);
        }
        std::fputs("}\n", out_records_micro_);
        noteFunctionalRecordEmitted(true, true);
    }
}

void
TaoTrace::maybeCompleteFunctionalSyscall(const DynInstPtr &inst)
{
    if (!inst || !inst->tcBase() || !out_fst_ || fst_finalized_ ||
        !functionalTraceEnabled() || !isUserInstruction(inst)) {
        return;
    }
    auto tc = inst->tcBase();
    const FunctionalReturnKey key{
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3) & ~uint64_t(0xfff),
        tc->getReg(X86ISA::int_reg::Rsp),
        inst->pcState().instAddr()};
    const auto range = pending_functional_returns_.equal_range(key);
    if (range.first == range.second) return;
    auto candidate = range.first;
    auto next = candidate;
    ++next;
    if (next != range.second) {
        // Ambiguous association must remain invalid; never guess between two
        // address-space/stack/return-PC-identical invocations.
        return;
    }
    TaoTrace *owner = candidate->second.owner;
    const size_t index = candidate->second.metadata_index;
    if (!owner || index >= owner->fst_syscall_metadata_.size()) {
        pending_functional_returns_.erase(candidate);
        return;
    }
    auto &metadata = owner->fst_syscall_metadata_[index];
    const uint64_t raw_return = tc->getReg(X86ISA::int_reg::Rax);
    metadata.return_value_raw = raw_return;
    metadata.post_timestamp_us =
        uint64_t(curTick()) / sim_clock::as_int::us;
    metadata.post_cpu = fst_core_id_ >= 0
        ? uint32_t(fst_core_id_) : getCoreId(inst);
    metadata.valid_fields = uint16_t(
        metadata.valid_fields | kSyscallReturnValueValid |
        kSyscallFailureValid | kSyscallPostTimestampValid |
        kSyscallPostCpuValid);
    const int64_t signed_return = static_cast<int64_t>(raw_return);
    if (signed_return < 0 && signed_return >= -4095) {
        metadata.flags = uint8_t(metadata.flags | kSyscallFailed);
        metadata.errno_value = uint32_t(-signed_return);
        metadata.valid_fields = uint16_t(
            metadata.valid_fields | kSyscallErrnoValid);
    }
    ++owner->fst_completed_syscall_returns_;
    pending_functional_returns_.erase(candidate);
}

bool
TaoTrace::functionalCaptureActive() const
{
    return functional_warmup_ || cplMeasurementActive();
}

bool
TaoTrace::functionalTraceEnabled() const
{
    return functional_user_only_ || functional_include_kernel_;
}

bool
TaoTrace::functionalTargetReached() const
{
    return functional_user_target_ > 0 &&
        functional_measurement_started_ &&
        functional_measurement_user_record_count_ >= functional_user_target_ &&
        functional_target_reached_.count(this) != 0;
}

void
TaoTrace::beginFunctionalMeasurement()
{
    if (!functional_warmup_ || functional_measurement_started_) return;
    functional_warmup_record_count_ = functional_record_count_;
    functional_warmup_instruction_count_ = functional_instruction_count_;
    functional_warmup_user_record_count_ = functional_user_record_count_;
    functional_warmup_user_instruction_count_ =
        functional_user_instruction_count_;
    functional_measurement_started_ = true;
    const int32_t context_id = fst_core_id_ >= 0
        ? fst_core_id_ : parseFstCoreIdFromName(name());
    if (context_id >= 0) {
        TaoTraceNativeAccessRegistry::enableContext(uint32_t(context_id));
    }
    inform("TaoTrace: %s functional warmup boundary records=%lu instructions=%lu",
           name(), (unsigned long)functional_warmup_record_count_,
           (unsigned long)functional_warmup_instruction_count_);
}

void
TaoTrace::noteFunctionalRecordEmitted(bool completes_instruction,
                                      bool is_user)
{
    ++functional_record_count_;
    if (completes_instruction) ++functional_instruction_count_;
    if (is_user) {
        ++functional_user_record_count_;
        if (completes_instruction) ++functional_user_instruction_count_;
    }
    if (functional_measurement_started_) {
        ++functional_measurement_record_count_;
        if (completes_instruction) {
            ++functional_measurement_instruction_count_;
        }
        if (is_user) {
            ++functional_measurement_user_record_count_;
            if (completes_instruction) {
                ++functional_measurement_user_instruction_count_;
            }
        }
    }
    if (functional_user_target_ == 0 || !functional_measurement_started_ ||
        functional_measurement_user_record_count_ < functional_user_target_ ||
        !completes_instruction ||
        functional_target_reached_.count(this) != 0) {
        return;
    }

    functional_target_reached_.insert(this);
    const int32_t context_id = cpl_core_id_ >= 0
        ? cpl_core_id_ : parseFstCoreIdFromName(name());
    if (context_id >= 0) {
        TaoTraceNativeAccessRegistry::disableContext(uint32_t(context_id));
        TaoTraceFrontendRegistry::disableContext(uint32_t(context_id));
    }
    // Freeze each core's trace and CPL/PMU oracle at the same boundary. A
    // faster core must not accumulate wait time while slower cores finish.
    finalizeCplMeasurement();
    inform("TaoTrace: %s reached functional measurement target %lu "
           "at macro boundary records=%lu instructions=%lu (%u/%u cores)",
           name(), (unsigned long)functional_user_target_,
           (unsigned long)functional_measurement_record_count_,
           (unsigned long)functional_measurement_instruction_count_,
           unsigned(functional_target_reached_.size()),
           unsigned(functional_target_instances_));
    if (!functional_target_exit_scheduled_ &&
        functional_target_instances_ > 0 &&
        functional_target_reached_.size() == functional_target_instances_) {
        functional_target_exit_scheduled_ = true;
        scheduleFunctionalTargetDrain(this);
    }
}

void
TaoTrace::scheduleFunctionalTargetDrain(TaoTrace *owner)
{
    if (!owner) return;
    functional_target_drain_owner_ = owner;
    auto *event = new EventFunctionWrapper(
        [] { pollFunctionalTargetDrain(); },
        "TaoTrace committed native-response drain", true);
    // Polling every CPU cycle turns a single malformed lifecycle entry into
    // an unbounded hot event.  Ruby responses are asynchronous and the CPL
    // oracle is already frozen at the target, so a 64-cycle cadence preserves
    // the exact result while keeping the drain observer negligible.
    constexpr Tick DrainPollPeriodCycles = 64;
    const Tick delay = std::max<Tick>(
        1, owner->cpl_clock_period_ticks_ * DrainPollPeriodCycles);
    owner->schedule(event, curTick() + delay);
}

void
TaoTrace::pollFunctionalTargetDrain()
{
    ++functional_target_drain_polls_;
    bool complete = true;
    for (auto *trace : functional_warmup_instances_) {
        if (!trace || functional_target_reached_.count(trace) == 0) continue;
        if (!trace->nativeResponseDrainComplete()) complete = false;
    }
    if (complete) {
        inform("TaoTrace: committed native-response drain complete after "
               "%lu polls", (unsigned long)functional_target_drain_polls_);
        // Keep the registered exit cause consumed by the standard-library
        // Simulator. The timing of this event is new (after exact drain), but
        // inventing a new cause makes exit_event.py reject an otherwise clean
        // simulation.
        exitSimLoop("a thread reached the max instruction count", 86);
        return;
    }

    // A valid in-flight store/load normally resolves in a few polls.  Emit
    // bounded, exponentially spaced evidence for any entry that does not,
    // and fail explicitly after more than two million simulated CPU cycles.
    // Continuing the workload forever cannot create a missing lifecycle fact
    // once tracking has been disabled at every core's exact target boundary.
    const bool diagnostic_poll =
        (functional_target_drain_polls_ &
         (functional_target_drain_polls_ - 1)) == 0;
    if (diagnostic_poll) {
        for (auto *trace : functional_warmup_instances_) {
            if (!trace || functional_target_reached_.count(trace) == 0) {
                continue;
            }
            trace->logNativeResponsePending(functional_target_drain_polls_);
        }
    }
    constexpr uint64_t MaxDrainPolls = uint64_t(1) << 15;
    if (functional_target_drain_polls_ >= MaxDrainPolls) {
        warn("TaoTrace: committed native-response drain timed out after "
             "%lu polls; refusing to publish an incomplete baseline",
             (unsigned long)functional_target_drain_polls_);
        exitSimLoop("TaoTrace committed native-response drain timed out", 1);
        return;
    }
    scheduleFunctionalTargetDrain(functional_target_drain_owner_);
}

void
TaoTrace::writeFunctionalBoundary()
{
    if (!functionalTraceEnabled()) return;
    char path[1024];
    const int32_t core_id = fst_core_id_ >= 0
        ? fst_core_id_ : parseFstCoreIdFromName(name());
    std::snprintf(path, sizeof(path),
                  "%s/functional-boundary-core%d.json",
                  output_dir_.c_str(), core_id);
    std::FILE *file = std::fopen(path, "w");
    if (!file) {
        warn("TaoTrace: failed to open %s", path);
        return;
    }
    std::fprintf(file,
        "{\"schema\":\"tcsim-functional-boundary-v1\","
        "\"core_id\":%d,\"functional_warmup_enabled\":%s,"
        "\"measurement_started\":%s,"
        "\"warmup_records\":%lu,\"warmup_instructions\":%lu,"
        "\"measurement_records\":%lu,"
        "\"measurement_instructions\":%lu,"
        "\"warmup_user_records\":%lu,\"warmup_user_instructions\":%lu,"
        "\"measurement_user_records\":%lu,"
        "\"measurement_user_instructions\":%lu,"
        "\"trace_scope\":\"%s\","
        "\"total_records\":%lu,\"total_instructions\":%lu,"
        "\"target_records\":%lu,\"target_reached\":%s}\n",
        core_id, functional_warmup_ ? "true" : "false",
        functional_measurement_started_ ? "true" : "false",
        (unsigned long)functional_warmup_record_count_,
        (unsigned long)functional_warmup_instruction_count_,
        (unsigned long)functional_measurement_record_count_,
        (unsigned long)functional_measurement_instruction_count_,
        (unsigned long)functional_warmup_user_record_count_,
        (unsigned long)functional_warmup_user_instruction_count_,
        (unsigned long)functional_measurement_user_record_count_,
        (unsigned long)functional_measurement_user_instruction_count_,
        functional_include_kernel_ ? "user-plus-kernel" : "user",
        (unsigned long)functional_record_count_,
        (unsigned long)functional_instruction_count_,
        (unsigned long)functional_user_target_,
        functional_target_reached_.count(this) ? "true" : "false");
    std::fclose(file);
}

void
TaoTrace::NativeHierarchyAggregate::merge(
    const TaoTraceNativeHierarchyFacts &facts)
{
    for (unsigned level = 0; level < TaoTraceNativeHierarchyFacts::Levels;
         ++level) {
        accesses[level] += facts.accesses[level];
        hits[level] += facts.hits[level];
        tag_misses[level] += facts.tagMisses[level];
        permission_upgrades[level] += facts.permissionUpgrades[level];
        merged_misses[level] += facts.mergedMisses[level];
        remote_supplies[level] += facts.remoteSupplies[level];
    }
    unique_fills += facts.uniqueFills;
    ruby_memory_fetches += facts.rubyMemoryFetches;
    memory_read_transactions += facts.memoryReadTransactions;
}

void
TaoTrace::NativeHierarchyAggregate::merge(
    const NativeHierarchyAggregate &other)
{
    for (unsigned level = 0; level < TaoTraceNativeHierarchyFacts::Levels;
         ++level) {
        accesses[level] += other.accesses[level];
        hits[level] += other.hits[level];
        tag_misses[level] += other.tag_misses[level];
        permission_upgrades[level] += other.permission_upgrades[level];
        merged_misses[level] += other.merged_misses[level];
        remote_supplies[level] += other.remote_supplies[level];
    }
    unique_fills += other.unique_fills;
    ruby_memory_fetches += other.ruby_memory_fetches;
    memory_read_transactions += other.memory_read_transactions;
}

void
TaoTrace::NativeScopeAggregate::merge(const NativeScopeAggregate &other)
{
    memory_uops += other.memory_uops;
    architectural_line_requests += other.architectural_line_requests;
    ruby_admission_fragments += other.ruby_admission_fragments;
    ruby_aliased_admission_fragments +=
        other.ruby_aliased_admission_fragments;
    ruby_hierarchy_request_fragments +=
        other.ruby_hierarchy_request_fragments;
    ruby_response_fragments += other.ruby_response_fragments;
    sequencer_coalesced_fragments +=
        other.sequencer_coalesced_fragments;
    no_ruby_uops += other.no_ruby_uops;
    native_outcome_uops += other.native_outcome_uops;
    native_external_hit_fragments += other.native_external_hit_fragments;
    native_external_miss_fragments += other.native_external_miss_fragments;
    native_responder_machine_unknown +=
        other.native_responder_machine_unknown;
    hierarchy_complete_uops += other.hierarchy_complete_uops;
    hierarchy_incomplete_uops += other.hierarchy_incomplete_uops;
    hierarchy.merge(other.hierarchy);
}

void
TaoTrace::recordNativeAnomaly(
    const char *kind, const PendingNativeResponseDiag &pending,
    const TaoTraceNativeAccessRegistry::Snapshot &native)
{
    if (native_diag_anomaly_samples_.size() >=
        native_response_anomaly_limit_) {
        ++native_diag_anomaly_samples_dropped_;
        return;
    }
    native_diag_anomaly_samples_.push_back({
        kind ? kind : "unknown",
        pending.inst_name,
        pending.thread_id,
        pending.inst_seq_num,
        pending.pc,
        pending.eff_addr,
        pending.phys_addr,
        pending.mem_req_flags,
        pending.eff_size,
        pending.is_load,
        pending.is_store,
        pending.is_atomic,
        pending.is_data_prefetch,
        pending.cycle_class,
        pending.proxy_path_class,
        pending.line_requests,
        native.admissions,
        native.aliasedAdmissions,
        native.hierarchyRequests,
        native.responses,
        native.externalHits,
        native.externalMisses,
        native.hierarchy.accesses[0],
    });
}

void
TaoTrace::accountNativeResolved(
    const PendingNativeResponseDiag &pending,
    const TaoTraceNativeAccessRegistry::Snapshot &native)
{
    auto &scope = native_diag_by_scope_[
        static_cast<size_t>(pending.cycle_class)];
    ++scope.memory_uops;
    scope.architectural_line_requests += pending.line_requests;
    scope.ruby_admission_fragments += native.admissions;
    scope.ruby_aliased_admission_fragments += native.aliasedAdmissions;
    scope.ruby_hierarchy_request_fragments += native.hierarchyRequests;
    scope.ruby_response_fragments += native.responses;
    scope.sequencer_coalesced_fragments += native.coalesced;
    scope.no_ruby_uops += native.responses == 0;
    scope.native_outcome_uops += native.responses != 0;
    scope.native_external_hit_fragments += native.externalHits;
    scope.native_external_miss_fragments += native.externalMisses;
    scope.native_responder_machine_unknown +=
        native.responderMachineUnknown;
    const bool hierarchy_complete = native.responses == 0 ||
        native.hierarchy.accesses[0] == native.hierarchyRequests;
    scope.hierarchy_complete_uops += hierarchy_complete;
    scope.hierarchy_incomplete_uops += !hierarchy_complete;
    scope.hierarchy.merge(native.hierarchy);

    if (pending.proxy_path_class < native_diag_proxy_confusion_.size()) {
        native_diag_proxy_confusion_[pending.proxy_path_class][0] +=
            native.externalHits;
        native_diag_proxy_confusion_[pending.proxy_path_class][1] +=
            native.externalMisses;
    }
    for (unsigned bit = 0; bit < native_diag_terminal_reason_counts_.size();
         ++bit) {
        if (native.terminalReasonMask & (uint32_t(1) << bit)) {
            ++native_diag_terminal_reason_counts_[bit];
        }
    }
    if (native.responses != native.externalHits + native.externalMisses) {
        ++native_diag_response_without_fact_uops_;
        recordNativeAnomaly("response_fact_mismatch", pending, native);
    }
    if (!hierarchy_complete) {
        recordNativeAnomaly("hierarchy_request_l1d_mismatch", pending, native);
    }
}

static bool
nativeHierarchyReady(
    const TaoTraceNativeAccessRegistry::Snapshot &native)
{
    // A store may retire before Ruby completes.  During the SLICC response
    // action, Sequencer::hitCallback() publishes the response packet before
    // the transition's demand-profile action records the L1D outcome.  Do
    // not reduce and erase that identity in the middle of the transition.
    // No-Ruby terminal UOPs have no hierarchy request to wait for.
    return native.responses == 0 ||
        native.hierarchy.accesses[0] == native.hierarchyRequests;
}

static void
writeNativeHierarchyJson(std::FILE *file,
                         const TaoTraceNativeHierarchyFacts &facts)
{
    static constexpr const char *levels[] = {"l1d", "l2", "llc"};
    std::fputc('{', file);
    for (unsigned level = 0; level < facts.Levels; ++level) {
        if (level != 0) std::fputc(',', file);
        std::fprintf(file,
            "\"%s\":{\"accesses\":%u,\"hits\":%u,"
            "\"tag_misses\":%u,\"permission_upgrades\":%u,"
            "\"merged_misses\":%u,\"remote_supplies\":%u}",
            levels[level], facts.accesses[level], facts.hits[level],
            facts.tagMisses[level], facts.permissionUpgrades[level],
            facts.mergedMisses[level], facts.remoteSupplies[level]);
    }
    std::fprintf(file,
        ",\"unique_fills\":%u,\"ruby_memory_fetches\":%u,"
        "\"memory_read_transactions\":%u}",
        facts.uniqueFills, facts.rubyMemoryFetches,
        facts.memoryReadTransactions);
}

const char *
TaoTrace::nativeDiagScopeName(CplCycleClass cycle_class)
{
    switch (cycle_class) {
      case CplCycleClass::USER: return "user";
      case CplCycleClass::SYSCALL: return "syscall";
      case CplCycleClass::PAGE_FAULT: return "page_fault";
      case CplCycleClass::IRQ: return "irq";
      case CplCycleClass::SCHEDULER: return "scheduler";
      case CplCycleClass::IDLE: return "idle";
      case CplCycleClass::UNKNOWN_KERNEL: return "unknown_kernel";
      case CplCycleClass::COUNT: return "invalid";
    }
    return "invalid";
}

void
TaoTrace::writeNativeResponseCommit(
    const DynInstPtr &inst, const SharedAttr &attr,
    CplCycleClass cycle_class, uint64_t line_requests,
    bool fallback_source)
{
    if (native_diag_finalized_ || !inst) return;
    const uint32_t tid = getTraceThreadId(inst);
    const uint32_t core_id = cpl_core_id_ >= 0
        ? uint32_t(cpl_core_id_) : getCoreId(inst);
    const uint64_t key =
        (uint64_t(tid) << 48) | uint64_t(inst->seqNum);
    // Import the complete Request-carried ledger before marking retirement.
    // This is necessary for a load that completed before the process-wide
    // warmup marker but retired after it on another core.  Such a request is
    // no longer visible to a measurement-only registry, while its extension
    // still contains the exact Ruby or explicit no-Ruby lifecycle.
    if (inst->savedRequest && inst->isCompleted()) {
        const auto request = inst->savedRequest->mainReq();
        if (request) {
            const auto extension =
                request->getExtension<TaoTraceRubyResponseExtension>();
            if (extension) {
                TaoTraceNativeAccessRegistry::noteObservedLifecycle(
                    tid, inst->seqNum, *extension);
            }
        }
    }
    TaoTraceNativeAccessRegistry::noteCommitted(tid, inst->seqNum);
    // SharedAttr is also the functional/proxy cache descriptor.  The
    // fallback path may reuse a macro's first completed memory micro-op for
    // later micro-ops, so its native fields are not identity-safe.  Import
    // packet-carried facts only when this exact UOP supplied the packet;
    // fallback UOPs are resolved exclusively by their Request extension and
    // the (ContextID, InstSeqNum) registry.  Otherwise a store can receive a
    // fake first lifecycle at commit and its real post-commit Ruby lifecycle
    // becomes a duplicate (2 requests/responses but one L1D outcome).
    if (!fallback_source && attr.native_response_count != 0) {
        TaoTraceNativeAccessRegistry::noteObservedComplete(
            tid, inst->seqNum, attr.native_hierarchy_request_count,
            attr.native_response_count,
            attr.native_external_hits, attr.native_external_misses,
            attr.native_coalesced, attr.native_responder_machine_mask,
            attr.native_responder_machine_unknown,
            attr.native_has_admission_tick,
            attr.native_first_admission_tick,
            attr.native_last_admission_tick,
            attr.native_has_response_tick,
            attr.native_last_response_tick,
            attr.native_hierarchy);
    }
    const auto native =
        TaoTraceNativeAccessRegistry::snapshot(tid, inst->seqNum);
    PendingNativeResponseDiag pending;
    pending.thread_id = tid;
    pending.inst_seq_num = uint64_t(inst->seqNum);
    pending.cycle_class = cycle_class;
    pending.proxy_path_class = attr.path_class;
    pending.line_requests = line_requests;
    pending.fallback_source = fallback_source;
    pending.inst_name = inst->staticInst ? inst->staticInst->getName() : "";
    pending.pc = inst->pcState().instAddr();
    pending.eff_addr = inst->effAddr;
    pending.phys_addr = inst->physEffAddr;
    pending.mem_req_flags = inst->memReqFlags;
    pending.eff_size = inst->effSize;
    pending.is_load = inst->isLoad();
    pending.is_store = inst->isStore();
    pending.is_atomic = inst->isAtomic();
    pending.is_data_prefetch = inst->isDataPrefetch();
    pending.has_request = inst->hasRequest();
    pending.read_predicate = inst->readPredicate();
    pending.mem_access_predicate = inst->readMemAccPredicate();
    pending.strictly_ordered = inst->strictlyOrdered();
    pending.translation_started = inst->translationStarted();
    pending.translation_completed = inst->translationCompleted();
    pending.executed = inst->isExecuted();
    pending.completed = inst->isCompleted();
    ++native_diag_committed_uops_;
    if (fallback_source) {
        ++native_diag_fallback_source_uops_;
    } else {
        ++native_diag_packet_source_uops_;
    }
    if (native.resolved() && nativeHierarchyReady(native) &&
        native.responses != 0) {
        ++native_diag_response_at_commit_uops_;
        accountNativeResolved(pending, native);
    } else if (native.resolved() && nativeHierarchyReady(native)) {
        ++native_diag_terminal_at_commit_uops_;
        accountNativeResolved(pending, native);
    } else {
        pending_native_response_diag_[key] = pending;
    }
    if (out_native_response_diag_) {
      std::fprintf(out_native_response_diag_,
        "{\"record\":\"commit\",\"core_id\":%u,\"thread_id\":%u,"
        "\"inst_seq_num\":%lu,\"scope\":\"%s\","
        "\"attribution_source\":\"%s\",\"proxy_path_class\":%u,"
        "\"line_requests\":%lu,\"native_admission_count\":%u,"
        "\"native_aliased_admissions\":%u,"
        "\"native_hierarchy_request_count\":%u,"
        "\"native_issuance_closed\":%s,\"native_terminal_no_ruby\":%s,"
        "\"native_terminal_reason_mask\":%u,"
        "\"native_response_count\":%u,"
        "\"native_external_hits\":%u,\"native_external_misses\":%u,"
        "\"native_coalesced\":%u,\"native_responder_machine_mask\":%lu,"
        "\"native_responder_machine_unknown\":%u,"
        "\"native_first_admission_tick\":%lu,"
        "\"native_last_admission_tick\":%lu,"
        "\"native_last_response_tick\":%lu,"
        "\"native_hierarchy\":",
        core_id, tid, (unsigned long)inst->seqNum,
        nativeDiagScopeName(cycle_class),
        fallback_source ? "fallback" : "packet",
        unsigned(attr.path_class), (unsigned long)line_requests,
        native.admissions, native.aliasedAdmissions,
        native.hierarchyRequests,
        native.issuanceClosed ? "true" : "false",
        native.terminalNoRuby ? "true" : "false",
        native.terminalReasonMask, native.responses,
        native.externalHits, native.externalMisses, native.coalesced,
        (unsigned long)native.responderMachineMask,
        native.responderMachineUnknown,
        (unsigned long)native.firstAdmissionTick,
        (unsigned long)native.lastAdmissionTick,
        (unsigned long)native.lastResponseTick);
      writeNativeHierarchyJson(out_native_response_diag_, native.hierarchy);
      std::fputs("}\n", out_native_response_diag_);
    }
    // A lifecycle can become response-complete before the SLICC transition
    // has published its controller-level hierarchy outcome.  Keep the
    // registry identity whenever the commit-time reducer had to defer it;
    // otherwise the late hierarchy action has nowhere to attach and the
    // target drain can never recover.  Erase only the exact state that was
    // eligible for reduction above.
    if (native.resolved() && nativeHierarchyReady(native)) {
        TaoTraceNativeAccessRegistry::erase(tid, inst->seqNum);
    }
}

void
TaoTrace::writeNativeResponseLate(
    const DynInstPtr &inst, const SharedAttr &attr)
{
    if (native_diag_finalized_ || !inst) return;
    refreshNativeResponsePending();
}

void
TaoTrace::refreshNativeResponsePending()
{
    if (native_diag_finalized_) return;
    const uint32_t core_id = cpl_core_id_ >= 0
        ? uint32_t(cpl_core_id_)
        : uint32_t(std::max<int32_t>(0, parseFstCoreIdFromName(name())));
    for (auto it = pending_native_response_diag_.begin();
         it != pending_native_response_diag_.end();) {
        const auto &pending = it->second;
        const auto native = TaoTraceNativeAccessRegistry::snapshot(
            pending.thread_id, pending.inst_seq_num);
        const bool hint_prefetch_terminal = pending.is_data_prefetch &&
            pending.executed && pending.completed && !pending.has_request &&
            !native.found;
        if (!hint_prefetch_terminal &&
            (!native.resolved() || !nativeHierarchyReady(native))) {
            ++it;
            continue;
        }
        const bool ruby_response = native.found && native.responses != 0;
        if (ruby_response) {
            ++native_diag_late_response_uops_;
        } else {
            ++native_diag_late_terminal_uops_;
        }
        accountNativeResolved(pending, native);
        if (out_native_response_diag_) {
          std::fprintf(out_native_response_diag_,
            "{\"record\":\"resolution\",\"core_id\":%u,"
            "\"thread_id\":%u,\"inst_seq_num\":%lu,"
            "\"scope\":\"%s\",\"attribution_source\":\"%s\","
            "\"proxy_path_class\":%u,\"line_requests\":%lu,"
            "\"resolution_kind\":\"%s\","
            "\"native_admission_count\":%u,"
            "\"native_aliased_admissions\":%u,"
            "\"native_hierarchy_request_count\":%u,"
            "\"native_issuance_closed\":%s,"
            "\"native_terminal_no_ruby\":%s,"
            "\"native_terminal_reason_mask\":%u,"
            "\"native_response_count\":%u,"
            "\"native_external_hits\":%u,"
            "\"native_external_misses\":%u,"
            "\"native_coalesced\":%u,"
            "\"native_responder_machine_mask\":%lu,"
            "\"native_responder_machine_unknown\":%u,"
            "\"native_first_admission_tick\":%lu,"
            "\"native_last_admission_tick\":%lu,"
            "\"native_last_response_tick\":%lu,"
            "\"native_hierarchy\":",
            core_id, pending.thread_id,
            (unsigned long)pending.inst_seq_num,
            nativeDiagScopeName(pending.cycle_class),
            pending.fallback_source ? "fallback" : "packet",
            unsigned(pending.proxy_path_class),
            (unsigned long)pending.line_requests,
            ruby_response ? "ruby_response" : "no_ruby_terminal",
            native.admissions, native.aliasedAdmissions,
            native.hierarchyRequests,
            native.issuanceClosed ? "true" : "false",
            native.terminalNoRuby ? "true" : "false",
            native.terminalReasonMask, native.responses,
            native.externalHits, native.externalMisses, native.coalesced,
            (unsigned long)native.responderMachineMask,
            native.responderMachineUnknown,
            (unsigned long)native.firstAdmissionTick,
            (unsigned long)native.lastAdmissionTick,
            (unsigned long)native.lastResponseTick);
          writeNativeHierarchyJson(out_native_response_diag_, native.hierarchy);
          std::fputs("}\n", out_native_response_diag_);
        }
        TaoTraceNativeAccessRegistry::erase(
            pending.thread_id, pending.inst_seq_num);
        it = pending_native_response_diag_.erase(it);
    }
    if (out_native_response_diag_) std::fflush(out_native_response_diag_);
}

bool
TaoTrace::nativeResponseDrainComplete()
{
    refreshNativeResponsePending();
    return pending_native_response_diag_.empty();
}

void
TaoTrace::logNativeResponsePending(uint64_t poll)
{
    refreshNativeResponsePending();
    if (pending_native_response_diag_.empty()) return;
    const uint32_t core_id = cpl_core_id_ >= 0
        ? uint32_t(cpl_core_id_)
        : uint32_t(std::max<int32_t>(0, parseFstCoreIdFromName(name())));
    inform("TaoTrace: native-response drain pending core=%u poll=%lu "
           "uops=%lu", core_id, (unsigned long)poll,
           (unsigned long)pending_native_response_diag_.size());
    unsigned emitted = 0;
    for (const auto &entry : pending_native_response_diag_) {
        if (emitted++ >= 4) break;
        const auto &pending = entry.second;
        const auto native = TaoTraceNativeAccessRegistry::snapshot(
            pending.thread_id, pending.inst_seq_num);
        inform("TaoTrace: native-response pending sample core=%u thread=%u "
               "seq=%lu found=%u committed=%u squashed=%u admissions=%u "
               "responses=%u issuance_closed=%u terminal_no_ruby=%u "
               "hierarchy_requests=%u l1d_accesses=%u source=%s path=%u "
               "name=%s pc=%#lx eff=%#lx phys=%#lx size=%u flags=%#lx "
               "load=%u store=%u atomic=%u prefetch=%u request=%u "
               "pred=%u mem_pred=%u strict=%u translation=%u/%u "
               "executed=%u completed=%u",
               core_id, pending.thread_id,
               (unsigned long)pending.inst_seq_num,
               native.found ? 1u : 0u, native.committed ? 1u : 0u,
               native.squashed ? 1u : 0u, native.admissions,
               native.responses, native.issuanceClosed ? 1u : 0u,
               native.terminalNoRuby ? 1u : 0u,
               native.hierarchyRequests, native.hierarchy.accesses[0],
               pending.fallback_source ? "fallback" : "packet",
               unsigned(pending.proxy_path_class), pending.inst_name.c_str(),
               (unsigned long)pending.pc, (unsigned long)pending.eff_addr,
               (unsigned long)pending.phys_addr, pending.eff_size,
               (unsigned long)pending.mem_req_flags,
               pending.is_load ? 1u : 0u, pending.is_store ? 1u : 0u,
               pending.is_atomic ? 1u : 0u,
               pending.is_data_prefetch ? 1u : 0u,
               pending.has_request ? 1u : 0u,
               pending.read_predicate ? 1u : 0u,
               pending.mem_access_predicate ? 1u : 0u,
               pending.strictly_ordered ? 1u : 0u,
               pending.translation_started ? 1u : 0u,
               pending.translation_completed ? 1u : 0u,
               pending.executed ? 1u : 0u,
               pending.completed ? 1u : 0u);
    }
}

void
TaoTrace::finalizeNativeResponseDiagnostic()
{
    if (native_diag_finalized_ || native_response_summary_path_.empty()) return;
    refreshNativeResponsePending();
    for (const auto &entry : pending_native_response_diag_) {
        const auto &pending = entry.second;
        recordNativeAnomaly(
            "unresolved_lifecycle", pending,
            TaoTraceNativeAccessRegistry::snapshot(
                pending.thread_id, pending.inst_seq_num));
    }
    native_diag_finalized_ = true;
    if (out_native_response_diag_) {
        std::fprintf(out_native_response_diag_,
            "{\"record\":\"summary\",\"committed_memory_uops\":%lu,"
            "\"packet_source_uops\":%lu,\"fallback_source_uops\":%lu,"
            "\"response_at_commit_uops\":%lu,"
            "\"terminal_at_commit_uops\":%lu,"
            "\"late_response_uops\":%lu,\"late_terminal_uops\":%lu,"
            "\"response_without_native_fact_uops\":%lu,"
            "\"unresolved_lifecycle_uops\":%lu,"
            "\"target_drain_polls\":%lu}\n",
            (unsigned long)native_diag_committed_uops_,
            (unsigned long)native_diag_packet_source_uops_,
            (unsigned long)native_diag_fallback_source_uops_,
            (unsigned long)native_diag_response_at_commit_uops_,
            (unsigned long)native_diag_terminal_at_commit_uops_,
            (unsigned long)native_diag_late_response_uops_,
            (unsigned long)native_diag_late_terminal_uops_,
            (unsigned long)native_diag_response_without_fact_uops_,
            (unsigned long)pending_native_response_diag_.size(),
            (unsigned long)functional_target_drain_polls_);
        std::fflush(out_native_response_diag_);
    }

    NativeScopeAggregate total;
    for (const auto &scope : native_diag_by_scope_) total.merge(scope);
    const uint32_t core_id = cpl_core_id_ >= 0
        ? uint32_t(cpl_core_id_)
        : uint32_t(std::max<int32_t>(0, parseFstCoreIdFromName(name())));
    std::FILE *summary = std::fopen(native_response_summary_path_.c_str(), "w");
    if (!summary) {
        warn("TaoTrace: failed to open %s",
             native_response_summary_path_.c_str());
        return;
    }
    auto write_hierarchy = [](std::FILE *file,
                              const NativeHierarchyAggregate &facts) {
        static constexpr const char *levels[] = {"l1d", "l2", "llc"};
        std::fputc('{', file);
        for (unsigned level = 0;
             level < TaoTraceNativeHierarchyFacts::Levels; ++level) {
            if (level) std::fputc(',', file);
            std::fprintf(file,
                "\"%s\":{\"accesses\":%lu,\"hits\":%lu,"
                "\"tag_misses\":%lu,\"permission_upgrades\":%lu,"
                "\"merged_misses\":%lu,\"remote_supplies\":%lu}",
                levels[level],
                (unsigned long)facts.accesses[level],
                (unsigned long)facts.hits[level],
                (unsigned long)facts.tag_misses[level],
                (unsigned long)facts.permission_upgrades[level],
                (unsigned long)facts.merged_misses[level],
                (unsigned long)facts.remote_supplies[level]);
        }
        std::fprintf(file,
            ",\"unique_fills\":%lu,\"ruby_memory_fetches\":%lu,"
            "\"memory_read_transactions\":%lu}",
            (unsigned long)facts.unique_fills,
            (unsigned long)facts.ruby_memory_fetches,
            (unsigned long)facts.memory_read_transactions);
    };
    auto write_population = [&](const NativeScopeAggregate &population) {
        std::fprintf(summary,
            "{\"memory_uops\":%lu,"
            "\"architectural_line_requests\":%lu,"
            "\"ruby_admission_fragments\":%lu,"
            "\"ruby_aliased_admission_fragments\":%lu,"
            "\"ruby_hierarchy_request_fragments\":%lu,"
            "\"ruby_response_fragments\":%lu,"
            "\"sequencer_coalesced_fragments\":%lu,"
            "\"no_ruby_uops\":%lu,"
            "\"hierarchy_complete_uops\":%lu,"
            "\"hierarchy_incomplete_uops\":%lu,"
            "\"hierarchy\":",
            (unsigned long)population.memory_uops,
            (unsigned long)population.architectural_line_requests,
            (unsigned long)population.ruby_admission_fragments,
            (unsigned long)population.ruby_aliased_admission_fragments,
            (unsigned long)population.ruby_hierarchy_request_fragments,
            (unsigned long)population.ruby_response_fragments,
            (unsigned long)population.sequencer_coalesced_fragments,
            (unsigned long)population.no_ruby_uops,
            (unsigned long)population.hierarchy_complete_uops,
            (unsigned long)population.hierarchy_incomplete_uops);
        write_hierarchy(summary, population.hierarchy);
        std::fputc('}', summary);
    };

    const double coverage = native_diag_committed_uops_ == 0 ? 0.0 :
        double(total.native_outcome_uops) /
        double(native_diag_committed_uops_);
    std::fprintf(summary,
        "{\"schema\":\"taotrace-native-summary-v1\","
        "\"record\":\"summary\",\"oracle_only\":true,"
        "\"fst_input\":false,\"core_id\":%u,"
        "\"source_sideband_schema\":\"taotrace-native-response-v7\","
        "\"lifecycle_join\":\"context-id-inst-seq-num\","
        "\"target_stop\":\"committed-native-drain\","
        "\"hierarchy_source\":\"ruby-slicc-controller-actions\","
        "\"hierarchy_identity_transport\":"
        "\"context-id-inst-seq-num-no-request-retention\","
        "\"hierarchy_request_semantics\":"
        "\"sequencer-mandatory-queue-enqueue\","
        "\"measurement_boundary_semantics\":"
        "\"preboundary-inflight-ledger-retire-cleanup\","
        "\"ruby_memory_fetch_semantics\":"
        "\"l2-to-directory-fetch-not-dram-transaction\","
        "\"memory_read_transaction_semantics\":"
        "\"accepted-ruby-memory-port-read-packet\","
        "\"full_jsonl_enabled\":%s,"
        "\"committed_memory_uops\":%lu,"
        "\"packet_source_uops\":%lu,\"fallback_source_uops\":%lu,"
        "\"response_at_commit_uops\":%lu,"
        "\"terminal_at_commit_uops\":%lu,"
        "\"late_response_uops\":%lu,\"late_terminal_uops\":%lu,"
        "\"pending_without_response_uops\":%lu,"
        "\"response_without_native_fact_uops\":%lu,"
        "\"native_outcome_uops\":%lu,\"native_no_ruby_uops\":%lu,"
        "\"native_admission_fragments\":%lu,"
        "\"native_hierarchy_request_fragments\":%lu,"
        "\"native_response_fragments\":%lu,"
        "\"native_external_hit_fragments\":%lu,"
        "\"native_external_miss_fragments\":%lu,"
        "\"native_coalesced_fragments\":%lu,"
        "\"native_responder_machine_unknown\":%lu,"
        "\"native_hierarchy_complete_uops\":%lu,"
        "\"native_hierarchy_incomplete_uops\":%lu,"
        "\"native_outcome_coverage\":%.17g,"
        "\"target_drain_polls\":%lu,",
        core_id, emit_native_response_jsonl_ ? "true" : "false",
        (unsigned long)native_diag_committed_uops_,
        (unsigned long)native_diag_packet_source_uops_,
        (unsigned long)native_diag_fallback_source_uops_,
        (unsigned long)native_diag_response_at_commit_uops_,
        (unsigned long)native_diag_terminal_at_commit_uops_,
        (unsigned long)native_diag_late_response_uops_,
        (unsigned long)native_diag_late_terminal_uops_,
        (unsigned long)pending_native_response_diag_.size(),
        (unsigned long)native_diag_response_without_fact_uops_,
        (unsigned long)total.native_outcome_uops,
        (unsigned long)total.no_ruby_uops,
        (unsigned long)total.ruby_admission_fragments,
        (unsigned long)total.ruby_hierarchy_request_fragments,
        (unsigned long)total.ruby_response_fragments,
        (unsigned long)total.native_external_hit_fragments,
        (unsigned long)total.native_external_miss_fragments,
        (unsigned long)total.sequencer_coalesced_fragments,
        (unsigned long)total.native_responder_machine_unknown,
        (unsigned long)total.hierarchy_complete_uops,
        (unsigned long)total.hierarchy_incomplete_uops,
        coverage, (unsigned long)functional_target_drain_polls_);

    static constexpr const char *path_names[] = {
        "proxy_l1", "proxy_l2", "proxy_llc", "proxy_remote", "proxy_dram"};
    std::fputs("\"proxy_native_confusion\":{", summary);
    for (unsigned path = 0; path < native_diag_proxy_confusion_.size(); ++path) {
        if (path) std::fputc(',', summary);
        std::fprintf(summary,
            "\"%s\":{\"native_hit_fragments\":%lu,"
            "\"native_miss_fragments\":%lu}", path_names[path],
            (unsigned long)native_diag_proxy_confusion_[path][0],
            (unsigned long)native_diag_proxy_confusion_[path][1]);
    }
    static constexpr const char *reason_names[] = {
        "predicated_off", "no_request", "store_forward", "local_access",
        "failed_store_conditional", "zero_size", "prefetch_skipped"};
    std::fputs("},\"terminal_reason_counts\":{", summary);
    bool first_reason = true;
    for (unsigned bit = 0; bit < native_diag_terminal_reason_counts_.size();
         ++bit) {
        if (native_diag_terminal_reason_counts_[bit] == 0) continue;
        if (!first_reason) std::fputc(',', summary);
        first_reason = false;
        std::fprintf(summary, "\"%s\":%lu", reason_names[bit],
            (unsigned long)native_diag_terminal_reason_counts_[bit]);
    }
    std::fputs("},\"native_ruby_pmu_by_scope\":{", summary);
    for (size_t index = 0; index < native_diag_by_scope_.size(); ++index) {
        if (index) std::fputc(',', summary);
        std::fprintf(summary, "\"%s\":",
            nativeDiagScopeName(CplCycleClass(index)));
        write_population(native_diag_by_scope_[index]);
    }
    std::fprintf(summary,
        "},\"anomalies\":{\"limit\":%lu,\"retained\":%lu,"
        "\"dropped\":%lu,\"samples\":[",
        (unsigned long)native_response_anomaly_limit_,
        (unsigned long)native_diag_anomaly_samples_.size(),
        (unsigned long)native_diag_anomaly_samples_dropped_);
    for (size_t index = 0; index < native_diag_anomaly_samples_.size();
         ++index) {
        if (index) std::fputc(',', summary);
        const auto &sample = native_diag_anomaly_samples_[index];
        std::fprintf(summary,
            "{\"kind\":\"%s\",\"core_id\":%u,\"thread_id\":%u,"
            "\"inst_seq_num\":%lu,\"scope\":\"%s\","
            "\"inst_name\":\"%s\",\"pc\":%lu,\"eff_addr\":%lu,"
            "\"phys_addr\":%lu,\"mem_req_flags\":%lu,"
            "\"eff_size\":%u,\"is_load\":%s,\"is_store\":%s,"
            "\"is_atomic\":%s,\"is_data_prefetch\":%s,"
            "\"proxy_path_class\":%u,\"line_requests\":%lu,"
            "\"native_admission_count\":%u,"
            "\"native_aliased_admissions\":%u,"
            "\"native_hierarchy_request_count\":%u,"
            "\"native_response_count\":%u,"
            "\"native_external_hits\":%u,"
            "\"native_external_misses\":%u,\"l1d_accesses\":%u}",
            sample.kind.c_str(), core_id, sample.thread_id,
            (unsigned long)sample.inst_seq_num,
            nativeDiagScopeName(sample.cycle_class),
            sample.inst_name.c_str(), (unsigned long)sample.pc,
            (unsigned long)sample.eff_addr,
            (unsigned long)sample.phys_addr,
            (unsigned long)sample.mem_req_flags, sample.eff_size,
            sample.is_load ? "true" : "false",
            sample.is_store ? "true" : "false",
            sample.is_atomic ? "true" : "false",
            sample.is_data_prefetch ? "true" : "false",
            unsigned(sample.proxy_path_class),
            (unsigned long)sample.line_requests, sample.admissions,
            sample.aliased_admissions, sample.hierarchy_requests,
            sample.responses, sample.external_hits, sample.external_misses,
            sample.l1d_accesses);
    }
    std::fputs("]}}\n", summary);
    std::fflush(summary);
    std::fclose(summary);
}

void
TaoTrace::accountCplCommit(const DynInstPtr &inst, bool is_syscall)
{
    if (!measure_cpl_ || !inst || !inst->tcBase()) return;
    if (cpl_finalized_) return;
    if (cpl_core_id_ < 0) {
        cpl_core_id_ = parseFstCoreIdFromName(name());
        if (cpl_core_id_ < 0) cpl_core_id_ = int32_t(getCoreId(inst));
    }
    if (!cplMeasurementActive()) {
        // During functional warmup the native registry keeps only requests
        // that can still cross the exact measurement boundary. Once an older
        // memory UOP retires it is outside the measured population and its
        // identity must be removed even if a store response arrives later.
        const auto pre_si = inst->staticInst;
        if (functional_warmup_ && !functional_measurement_started_ &&
            pre_si && (pre_si->isLoad() || pre_si->isStore() ||
                       isLockedAtomicMicro(inst))) {
            TaoTraceNativeAccessRegistry::erase(
                getTraceThreadId(inst), inst->seqNum);
        }
        if (cpl_initialized_) settleCpl(cplMeasurementEndTick());
        return;
    }

    const uint64_t tick = uint64_t(curTick());
    const bool decoded_user = isUserInstruction(inst);
    const bool syscall_macro = isSyscallMacro(inst);
    const bool halt_idle_inst = !decoded_user && isIdleInstruction(inst);
    const bool poll_idle_inst = !decoded_user && isPollIdleInstruction(inst);
    constexpr uint32_t PollIdlePauseThreshold = 128;
    constexpr uint32_t PollIdleMaxGapCommits = 64;
    if (!cpl_initialized_ && !decoded_user) return;
    initializeCpl(tick,
                  inst->cpu ? uint64_t(inst->cpu->clockPeriod()) : 1,
                  decoded_user);

    if (decoded_user) {
        cpl_poll_candidate_pc_ = 0;
        cpl_poll_candidate_pauses_ = 0;
        cpl_poll_candidate_gap_commits_ = 0;
    } else if (poll_idle_inst) {
        const uint64_t pc = inst->pcState().instAddr();
        if (pc == cpl_poll_candidate_pc_ &&
            cpl_poll_candidate_gap_commits_ <= PollIdleMaxGapCommits) {
            ++cpl_poll_candidate_pauses_;
        } else {
            cpl_poll_candidate_pc_ = pc;
            cpl_poll_candidate_pauses_ = 1;
        }
        cpl_poll_candidate_gap_commits_ = 0;
    } else if (cpl_poll_candidate_pauses_ != 0 &&
               ++cpl_poll_candidate_gap_commits_ >
                   PollIdleMaxGapCommits) {
        // A polling loop may observe a wake condition without first taking an
        // IRQ on this CPU. Once PAUSE has disappeared for a bounded number of
        // commits, close the persistent idle frame so wakeup/scheduler/syscall
        // tail work is active kernel execution again. The current commit is
        // charged to the restored parent; only the bounded hysteresis remains
        // in IDLE.
        cpl_poll_idle_ = false;
        cpl_poll_candidate_pc_ = 0;
        cpl_poll_candidate_pauses_ = 0;
        cpl_poll_candidate_gap_commits_ = 0;
    }

    // HLT/MWAIT ends at the first ordinary kernel commit after wakeup. A
    // confirmed idle=poll loop remains IDLE across its loop body. After an
    // IRQ return we clear cpl_poll_idle_ below; the first ordinary commit then
    // restores the parent class unless a fresh repeated-PAUSE sequence proves
    // that the CPU returned to the poll loop.
    if (!decoded_user && cpl_cycle_class_ == CplCycleClass::IDLE &&
        !cpl_poll_idle_ && !halt_idle_inst && !isInterruptReturn(inst)) {
        settleCpl(tick);
        if (!cpl_parent_stack_.empty()) {
            cpl_cycle_class_ = cpl_parent_stack_.back();
            cpl_parent_stack_.pop_back();
        } else {
            cpl_cycle_class_ = CplCycleClass::UNKNOWN_KERNEL;
        }
    }
    settleCpl(tick);

    // A decoded user instruction after SYSRET/IRET closes every privileged
    // frame. Syscall transition micro-ops retain SYSCALL attribution even
    // though their macro-op was decoded at CPL3.
    if (decoded_user &&
        !(cpl_cycle_class_ == CplCycleClass::SYSCALL && syscall_macro)) {
        cpl_parent_stack_.clear();
        cpl_cycle_class_ = CplCycleClass::USER;
        cpl_active_sysnum_ = 0;
        cpl_poll_idle_ = false;
    } else if (!decoded_user && cpl_cycle_class_ == CplCycleClass::USER) {
        cpl_parent_stack_.push_back(CplCycleClass::USER);
        cpl_cycle_class_ = cpl_syscall_pending_
            ? CplCycleClass::SYSCALL : CplCycleClass::UNKNOWN_KERNEL;
        ++cpl_entries_[static_cast<size_t>(cpl_cycle_class_)];
        if (cpl_cycle_class_ == CplCycleClass::UNKNOWN_KERNEL) {
            const StaticInstPtr macro = inst->macroop;
            const StaticInstPtr candidate = macro ? macro : inst->staticInst;
            const std::string name = candidate
                ? candidate->getName() : std::string("<null>");
            ++cpl_unknown_sources_[std::string("transition:") + name];
        }
        if (cpl_cycle_class_ == CplCycleClass::SYSCALL) {
            cpl_active_sysnum_ = cpl_pending_sysnum_;
        }
        cpl_syscall_pending_ = false;
    }

    const bool confirmed_poll_idle = poll_idle_inst &&
        cpl_poll_candidate_pauses_ >= PollIdlePauseThreshold;
    if (!decoded_user && (halt_idle_inst || confirmed_poll_idle) &&
        cpl_cycle_class_ != CplCycleClass::IDLE) {
        cpl_parent_stack_.push_back(cpl_cycle_class_);
        cpl_cycle_class_ = CplCycleClass::IDLE;
        cpl_poll_idle_ = confirmed_poll_idle;
        ++cpl_entries_[static_cast<size_t>(CplCycleClass::IDLE)];
    }

    ++cpl_commits_[static_cast<size_t>(cpl_cycle_class_)];
    if (decoded_user) {
        ++cpl_user_commits_;
        if (!isSyscallMacro(inst)) ++cpl_user_functional_uops_;
    }

    auto account_commit_pmu = [&](PmuScope &pmu) {
        ++pmu.retired_uops;
        if (!inst->staticInst->isMicroop() ||
            inst->staticInst->isLastMicroop()) {
            ++pmu.retired_instructions;
        }
        if (inst->staticInst->isControl()) {
            ++pmu.branches;
            if (inst->branchPredMispredicted()) {
                ++pmu.branch_misses;
            }
        }
    };
    // PMU privilege scope follows the committed instruction's decoded CPL,
    // while cycle scope follows the currently active kernel frame.  This is
    // important for the syscall transition macro-op: its user-decoded uops
    // belong to perf :u even if the cycle classifier has already opened the
    // SYSCALL frame.  Keeping those rules separate makes the following exact:
    //   pmu(:uk) = pmu(:u) + active kernel classes (idle excluded).
    const CplCycleClass commit_class = decoded_user
        ? CplCycleClass::USER : cpl_cycle_class_;
    account_commit_pmu(
        cpl_pmu_by_class_[static_cast<size_t>(commit_class)]);
    // Application :uk excludes poll-idle work. Keep the idle PMU domain for
    // conservation/diagnosis, but do not contaminate the active scope.
    if (commit_class != CplCycleClass::IDLE) {
        account_commit_pmu(cpl_pmu_user_plus_kernel_);
    }
    if (decoded_user) account_commit_pmu(cpl_pmu_user_);
    if (commit_class == CplCycleClass::SYSCALL) {
        account_commit_pmu(cpl_syscalls_[cpl_active_sysnum_].pmu);
    }

    const auto si = inst->staticInst;
    const bool data_touching = si &&
        (si->isLoad() || si->isStore() || isLockedAtomicMicro(inst));
    if (data_touching) {
        const uint32_t data_tid = getTraceThreadId(inst);
        // O3 distinguishes the architectural predicate from the memory-
        // access predicate.  A masked access with no active bytes, and a
        // fault-suppressed software prefetch, can retire with a live
        // LSQRequest while readMemAccPredicate() is false.  Such an access
        // intentionally never reaches Ruby; treating hasRequest() as proof
        // of a future Ruby admission leaves the target drain pending forever.
        if (!inst->readPredicate() || !inst->readMemAccPredicate()) {
            TaoTraceNativeAccessRegistry::noteTerminal(
                data_tid, inst->seqNum,
                TaoTraceNativeTerminalReason::PredicatedOff);
        } else if (!inst->hasRequest()) {
            TaoTraceNativeAccessRegistry::noteTerminal(
                data_tid, inst->seqNum,
                TaoTraceNativeTerminalReason::NoRequest);
        }
        const uint64_t data_key =
            (uint64_t(data_tid) << 48) |
            uint64_t(inst->seqNum);
        const uint64_t access_size = std::max<uint64_t>(1, inst->effSize);
        const uint64_t line_requests =
            1 + ((uint64_t(inst->physEffAddr) & 63) + access_size - 1) / 64;
        DtlbOutcome dtlb_outcome = DtlbOutcome::UNKNOWN;
        if (inst->translationStarted() && inst->translationCompleted()) {
            dtlb_outcome = inst->dataTlbMissObserved()
                ? DtlbOutcome::MISS : DtlbOutcome::HIT;
        } else {
            ++cpl_dtlb_unknown_uops_;
        }
        ++cpl_committed_memory_uops_;
        cpl_line_requests_ += line_requests;
        const auto completed = pending_shared_attr_.find(data_key);
        if (completed != pending_shared_attr_.end()) {
            accountCplDataPmu(
                completed->second, commit_class, cpl_active_sysnum_,
                line_requests, dtlb_outcome);
            ++cpl_packet_attributed_uops_;
            writeNativeResponseCommit(
                inst, completed->second, commit_class, line_requests,
                false);
        } else {
            const bool skips_accumulate =
                (functional_user_only_ && !decoded_user) ||
                (functionalTraceEnabled() && isSyscallMacro(inst)) ||
                (isSyscallInst(inst) &&
                 pending_syscalls_.count(inst->seqNum));
            if (skips_accumulate) {
                const bool is_store =
                    si->isStore() || isLockedAtomicMicro(inst);
                const SharedAttr fallback = deriveSharedAttrFromLineState(
                    inst->physEffAddr, getCoreId(inst), is_store);
                accountCplDataPmu(
                    fallback, commit_class, cpl_active_sysnum_, line_requests,
                    dtlb_outcome);
                ++cpl_fallback_attributed_uops_;
                writeNativeResponseCommit(
                    inst, fallback, commit_class, line_requests, true);
            } else {
                // accumulateMicro will use the same fallback attribution that
                // it emits into FST. Freeze the commit-time scope and line
                // count here so a later CPL transition cannot move the event.
                pending_cpl_data_class_[data_key] = {
                    commit_class, cpl_active_sysnum_, line_requests,
                    dtlb_outcome};
            }
        }
    }

    if (decoded_user && is_syscall &&
        cpl_cycle_class_ == CplCycleClass::USER) {
        cpl_active_sysnum_ = cpl_pending_sysnum_;
        cpl_parent_stack_.push_back(CplCycleClass::USER);
        cpl_cycle_class_ = CplCycleClass::SYSCALL;
        cpl_syscall_pending_ = false;
        ++cpl_syscall_boundaries_;
        ++cpl_entries_[static_cast<size_t>(CplCycleClass::SYSCALL)];
        ++cpl_syscalls_[cpl_active_sysnum_].count;
        emitFunctionalSyscallMarker(inst, cpl_active_sysnum_);
    }

    if (!decoded_user && isInterruptReturn(inst) &&
        !inst->tcBase()->getIsaPtr()->inUserMode() &&
        !cpl_parent_stack_.empty()) {
        cpl_cycle_class_ = cpl_parent_stack_.back();
        cpl_parent_stack_.pop_back();
        if (cpl_cycle_class_ == CplCycleClass::IDLE) {
            cpl_poll_idle_ = false;
            cpl_poll_candidate_pc_ = 0;
            cpl_poll_candidate_pauses_ = 0;
            cpl_poll_candidate_gap_commits_ = 0;
        }
    }
}

void
TaoTrace::accountCplDataPmu(
    const SharedAttr &a, CplCycleClass data_class, uint64_t syscall_number,
    uint64_t line_requests, DtlbOutcome dtlb_outcome)
{
    auto account_data_pmu = [&](PmuScope &pmu) {
        ++pmu.memory_uops;
        pmu.line_requests += line_requests;
        pmu.l1d_accesses += line_requests;
        if (a.path_class == 0) {
            pmu.l1d_hits += line_requests;
        } else {
            pmu.l1d_misses += line_requests;
            pmu.l2_accesses += line_requests;
            if (a.path_class == 1) {
                pmu.l2_hits += line_requests;
            } else {
                pmu.l2_misses += line_requests;
                pmu.llc_accesses += line_requests;
                if (a.path_class == 2) {
                    pmu.llc_hits += line_requests;
                } else {
                    pmu.llc_misses += line_requests;
                }
            }
        }
        if (a.coh == CoherenceAction::WB_REQUIRED) {
            pmu.permission_upgrades += line_requests;
        }
        if (a.coh == CoherenceAction::REMOTE_HIT_CLEAN ||
            a.coh == CoherenceAction::REMOTE_HIT_DIRTY) {
            pmu.remote_supplies += line_requests;
        }
        // The sticky DynInst result comes from LSQRequest::markDelayed(), not
        // from the independent TaoTrace functional-TLB feature model. Cache
        // line expansion must not manufacture extra translation lookups.
        ++pmu.dtlb_accesses;
        if (dtlb_outcome == DtlbOutcome::HIT) ++pmu.dtlb_hits;
        else if (dtlb_outcome == DtlbOutcome::MISS) ++pmu.dtlb_misses;
    };
    account_data_pmu(
        cpl_pmu_by_class_[static_cast<size_t>(data_class)]);
    if (data_class != CplCycleClass::IDLE) {
        account_data_pmu(cpl_pmu_user_plus_kernel_);
    }
    if (data_class == CplCycleClass::USER) {
        account_data_pmu(cpl_pmu_user_);
    } else if (data_class == CplCycleClass::SYSCALL) {
        account_data_pmu(cpl_syscalls_[syscall_number].pmu);
    }
}

void
TaoTrace::finalizeCplMeasurement()
{
    if (!measure_cpl_ || cpl_finalized_ || !global_cpl_class_) return;
    cpl_finalized_ = true;
    if (cpl_initialized_) settleCpl(cplMeasurementEndTick());
    auto ticks = [&](CplCycleClass c) -> uint64_t {
        return cpl_ticks_[static_cast<size_t>(c)];
    };
    auto commits = [&](CplCycleClass c) -> uint64_t {
        return cpl_commits_[static_cast<size_t>(c)];
    };
    auto entries = [&](CplCycleClass c) -> uint64_t {
        return cpl_entries_[static_cast<size_t>(c)];
    };
    uint64_t measured_ticks = 0;
    for (uint64_t value : cpl_ticks_) measured_ticks += value;
    const uint64_t span_ticks = cpl_last_tick_ >= cpl_first_tick_
        ? cpl_last_tick_ - cpl_first_tick_ : 0;
    const uint64_t period = std::max<uint64_t>(1, cpl_clock_period_ticks_);
    const uint64_t irq_idle_ticks =
        ticks(CplCycleClass::PAGE_FAULT) + ticks(CplCycleClass::IRQ) +
        ticks(CplCycleClass::SCHEDULER) + ticks(CplCycleClass::IDLE) +
        ticks(CplCycleClass::UNKNOWN_KERNEL);
    std::fprintf(global_cpl_class_,
        "{\"core_id\":%d,\"first_tick\":%lu,\"last_tick\":%lu,"
        "\"clock_period_ticks\":%lu,\"span_ticks\":%lu,"
        "\"measured_ticks\":%lu,"
        "\"user_ticks\":%lu,\"syscall_kernel_ticks\":%lu,"
        "\"page_fault_kernel_ticks\":%lu,\"irq_kernel_ticks\":%lu,"
        "\"scheduler_kernel_ticks\":%lu,\"idle_ticks\":%lu,"
        "\"unknown_kernel_ticks\":%lu,\"irq_idle_kernel_ticks\":%lu,"
        "\"user_cycles\":%lu,\"syscall_kernel_cycles\":%lu,"
        "\"page_fault_kernel_cycles\":%lu,\"irq_kernel_cycles\":%lu,"
        "\"scheduler_kernel_cycles\":%lu,\"idle_cycles\":%lu,"
        "\"unknown_kernel_cycles\":%lu,\"irq_idle_kernel_cycles\":%lu,"
        "\"user_commits\":%lu,\"user_functional_uops\":%lu,"
        "\"syscall_kernel_commits\":%lu,"
        "\"page_fault_kernel_commits\":%lu,\"irq_kernel_commits\":%lu,"
        "\"idle_commits\":%lu,\"unknown_kernel_commits\":%lu,"
        "\"irq_idle_kernel_commits\":%lu,\"syscall_entries\":%lu,"
        "\"page_fault_entries\":%lu,\"irq_entries\":%lu,"
        "\"idle_entries\":%lu,\"unknown_kernel_entries\":%lu,"
        "\"irq_idle_entries\":%lu,\"syscall_boundaries\":%lu,"
        "\"tick_sum_matches\":%u}\n",
        cpl_core_id_,
        (unsigned long)cpl_first_tick_, (unsigned long)cpl_last_tick_,
        (unsigned long)period, (unsigned long)span_ticks,
        (unsigned long)measured_ticks,
        (unsigned long)ticks(CplCycleClass::USER),
        (unsigned long)ticks(CplCycleClass::SYSCALL),
        (unsigned long)ticks(CplCycleClass::PAGE_FAULT),
        (unsigned long)ticks(CplCycleClass::IRQ),
        (unsigned long)ticks(CplCycleClass::SCHEDULER),
        (unsigned long)ticks(CplCycleClass::IDLE),
        (unsigned long)ticks(CplCycleClass::UNKNOWN_KERNEL),
        (unsigned long)irq_idle_ticks,
        (unsigned long)(ticks(CplCycleClass::USER) / period),
        (unsigned long)(ticks(CplCycleClass::SYSCALL) / period),
        (unsigned long)(ticks(CplCycleClass::PAGE_FAULT) / period),
        (unsigned long)(ticks(CplCycleClass::IRQ) / period),
        (unsigned long)(ticks(CplCycleClass::SCHEDULER) / period),
        (unsigned long)(ticks(CplCycleClass::IDLE) / period),
        (unsigned long)(ticks(CplCycleClass::UNKNOWN_KERNEL) / period),
        (unsigned long)(irq_idle_ticks / period),
        (unsigned long)cpl_user_commits_,
        (unsigned long)(
            cpl_user_functional_uops_ + cpl_syscall_boundaries_),
        (unsigned long)commits(CplCycleClass::SYSCALL),
        (unsigned long)commits(CplCycleClass::PAGE_FAULT),
        (unsigned long)commits(CplCycleClass::IRQ),
        (unsigned long)commits(CplCycleClass::IDLE),
        (unsigned long)commits(CplCycleClass::UNKNOWN_KERNEL),
        (unsigned long)(commits(CplCycleClass::PAGE_FAULT) +
            commits(CplCycleClass::IRQ) + commits(CplCycleClass::SCHEDULER) +
            commits(CplCycleClass::IDLE) +
            commits(CplCycleClass::UNKNOWN_KERNEL)),
        (unsigned long)entries(CplCycleClass::SYSCALL),
        (unsigned long)entries(CplCycleClass::PAGE_FAULT),
        (unsigned long)entries(CplCycleClass::IRQ),
        (unsigned long)entries(CplCycleClass::IDLE),
        (unsigned long)entries(CplCycleClass::UNKNOWN_KERNEL),
        (unsigned long)(entries(CplCycleClass::PAGE_FAULT) +
            entries(CplCycleClass::IRQ) + entries(CplCycleClass::SCHEDULER) +
            entries(CplCycleClass::IDLE) +
            entries(CplCycleClass::UNKNOWN_KERNEL)),
        (unsigned long)cpl_syscall_boundaries_,
        measured_ticks == span_ticks ? 1u : 0u);
    std::fflush(global_cpl_class_);

    char path[1024];
    std::snprintf(path, sizeof(path), "%s/oracle/cpi-core%d.json",
                  output_dir_.c_str(), cpl_core_id_);
    std::FILE *cpi = std::fopen(path, "w");
    if (!cpi) return;
    const uint64_t n_user =
        cpl_user_functional_uops_ + cpl_syscall_boundaries_;
    const double cpi_user = n_user
        ? double(ticks(CplCycleClass::USER)) /
              double(period) / double(n_user) : 0.0;
    const double cpi_incl = n_user
        ? double(ticks(CplCycleClass::USER) +
                 ticks(CplCycleClass::SYSCALL) +
                 ticks(CplCycleClass::PAGE_FAULT) +
                 ticks(CplCycleClass::IRQ) +
                 ticks(CplCycleClass::SCHEDULER) +
                 ticks(CplCycleClass::UNKNOWN_KERNEL)) /
              double(period) / double(n_user)
        : 0.0;
    std::fprintf(cpi,
        "{\"core_id\":%d,\"n_user\":%lu,\"user_cycles\":%.17g,"
        "\"syscall_kernel_cycles\":%.17g,"
        "\"irq_idle_kernel_cycles\":%.17g,"
        "\"cpi_user\":%.17g,\"cpi_incl\":%.17g,"
        "\"syscalls\":[",
        cpl_core_id_, (unsigned long)n_user,
        double(ticks(CplCycleClass::USER)) / double(period),
        double(ticks(CplCycleClass::SYSCALL)) / double(period),
        double(irq_idle_ticks) / double(period),
        cpi_user, cpi_incl);
    bool first = true;
    for (const auto &entry : cpl_syscalls_) {
        if (!first) std::fputc(',', cpi);
        first = false;
        std::fprintf(cpi,
            "{\"sysnum\":%lu,\"count\":%lu,\"kernel_cycles\":%.17g}",
            (unsigned long)entry.first,
            (unsigned long)entry.second.count,
            double(entry.second.kernel_ticks) / double(period));
    }
    std::fprintf(cpi, "]}\n");
    std::fclose(cpi);

    const auto cycles = [&](CplCycleClass c) -> uint64_t {
        return ticks(c) / period;
    };
    uint64_t measured_cycles = 0;
    for (size_t i = 0; i < CplCycleClassCount; ++i) {
        measured_cycles += cpl_ticks_[i] / period;
    }
    // CPI_user+kernel includes every active CPL0 class.  Idle is elapsed
    // wall/core time rather than useful user or kernel execution, so it stays
    // in measured_cycles for conservation but is excluded from this CPI.
    const uint64_t active_cycles =
        cycles(CplCycleClass::USER) + cycles(CplCycleClass::SYSCALL) +
        cycles(CplCycleClass::PAGE_FAULT) + cycles(CplCycleClass::IRQ) +
        cycles(CplCycleClass::SCHEDULER) +
        cycles(CplCycleClass::UNKNOWN_KERNEL);
    std::snprintf(path, sizeof(path),
                  "%s/oracle/kernel-events-core%d.json",
                  output_dir_.c_str(), cpl_core_id_);
    std::FILE *events_file = std::fopen(path, "w");
    if (!events_file) return;
    auto write_pmu = [&](const PmuScope &pmu) {
        std::fprintf(events_file,
            "{\"retired_instructions\":%lu,\"retired_uops\":%lu,"
            "\"memory_uops\":%lu,\"line_requests\":%lu,"
            "\"branches\":%lu,\"branch_misses\":%lu,"
            "\"l1d_accesses\":%lu,\"l1d_hits\":%lu,\"l1d_misses\":%lu,"
            "\"l1d_tag_accesses\":%lu,\"l1d_tag_hits\":%lu,"
            "\"l1d_tag_misses\":%lu,"
            "\"l2_accesses\":%lu,\"l2_hits\":%lu,\"l2_misses\":%lu,"
            "\"private_l2_tag_accesses\":%lu,"
            "\"private_l2_tag_hits\":%lu,"
            "\"private_l2_tag_misses\":%lu,"
            "\"llc_accesses\":%lu,\"llc_hits\":%lu,\"llc_misses\":%lu,"
            "\"llc_tag_accesses\":%lu,\"llc_tag_hits\":%lu,"
            "\"llc_tag_misses\":%lu,"
            "\"permission_upgrades\":%lu,\"remote_supplies\":%lu,"
            "\"llc_merged_misses\":%lu,\"llc_unique_fills\":%lu,"
            "\"dram_reads\":%lu,\"dram_writes\":%lu,"
            "\"dtlb_accesses\":%lu,\"dtlb_hits\":%lu,"
            "\"dtlb_misses\":%lu}",
            (unsigned long)pmu.retired_instructions,
            (unsigned long)pmu.retired_uops,
            (unsigned long)pmu.memory_uops,
            (unsigned long)pmu.line_requests,
            (unsigned long)pmu.branches,
            (unsigned long)pmu.branch_misses,
            (unsigned long)pmu.l1d_accesses,
            (unsigned long)pmu.l1d_hits,
            (unsigned long)pmu.l1d_misses,
            (unsigned long)pmu.l1d_accesses,
            (unsigned long)pmu.l1d_hits,
            (unsigned long)pmu.l1d_misses,
            (unsigned long)pmu.l2_accesses,
            (unsigned long)pmu.l2_hits,
            (unsigned long)pmu.l2_misses,
            (unsigned long)pmu.l2_accesses,
            (unsigned long)pmu.l2_hits,
            (unsigned long)pmu.l2_misses,
            (unsigned long)pmu.llc_accesses,
            (unsigned long)pmu.llc_hits,
            (unsigned long)pmu.llc_misses,
            (unsigned long)pmu.llc_accesses,
            (unsigned long)pmu.llc_hits,
            (unsigned long)pmu.llc_misses,
            (unsigned long)pmu.permission_upgrades,
            (unsigned long)pmu.remote_supplies,
            (unsigned long)pmu.llc_merged_misses,
            (unsigned long)pmu.llc_unique_fills,
            (unsigned long)pmu.dram_reads,
            (unsigned long)pmu.dram_writes,
            (unsigned long)pmu.dtlb_accesses,
            (unsigned long)pmu.dtlb_hits,
            (unsigned long)pmu.dtlb_misses);
    };
    std::fprintf(events_file,
        "{\"core_id\":%d,\"measured_cycles\":%lu,\"n_user\":%lu,"
        "\"user_cycles\":%lu,\"syscall_kernel_cycles\":%lu,"
        "\"page_fault_kernel_cycles\":%lu,\"irq_kernel_cycles\":%lu,"
        "\"scheduler_kernel_cycles\":%lu,\"idle_cycles\":%lu,"
        "\"unknown_kernel_cycles\":%lu,\"blocked_wall_cycles\":0,"
        "\"idle_detection\":\"x86-halt-mwait-or-repeated-f3-90-v2\","
        "\"poll_idle_pause_threshold\":128,"
        "\"poll_idle_max_gap_commits\":64,"
        "\"cpi_user\":%.17g,\"cpi_user_plus_kernel\":%.17g,"
        "\"cycles_per_user_uop_user\":%.17g,"
        "\"cycles_per_user_uop_user_plus_kernel\":%.17g,"
        "\"user_retired_instructions\":%lu,"
        "\"user_plus_kernel_retired_instructions\":%lu,"
        "\"perf_like_cpi_user\":%.17g,"
        "\"perf_like_cpi_user_plus_kernel\":%.17g,"
        "\"pmu_source\":\"taotrace-path-class-v3\","
        "\"pmu_contract_id\":\"perf-gem5-fastsim-x86-fs-v1\","
        "\"branch_miss_source\":\"taotrace-retired-bpred-v1\","
        "\"pmu_user\":",
        cpl_core_id_, (unsigned long)measured_cycles, (unsigned long)n_user,
        (unsigned long)cycles(CplCycleClass::USER),
        (unsigned long)cycles(CplCycleClass::SYSCALL),
        (unsigned long)cycles(CplCycleClass::PAGE_FAULT),
        (unsigned long)cycles(CplCycleClass::IRQ),
        (unsigned long)cycles(CplCycleClass::SCHEDULER),
        (unsigned long)cycles(CplCycleClass::IDLE),
        (unsigned long)cycles(CplCycleClass::UNKNOWN_KERNEL),
        n_user ? double(cycles(CplCycleClass::USER)) / double(n_user) : 0.0,
        n_user ? double(active_cycles) / double(n_user) : 0.0,
        n_user ? double(cycles(CplCycleClass::USER)) / double(n_user) : 0.0,
        n_user ? double(active_cycles) / double(n_user) : 0.0,
        (unsigned long)cpl_pmu_user_.retired_instructions,
        (unsigned long)cpl_pmu_user_plus_kernel_.retired_instructions,
        cpl_pmu_user_.retired_instructions
            ? double(cycles(CplCycleClass::USER)) /
                  double(cpl_pmu_user_.retired_instructions) : 0.0,
        cpl_pmu_user_plus_kernel_.retired_instructions
            ? double(active_cycles) /
                  double(cpl_pmu_user_plus_kernel_.retired_instructions) : 0.0);
    write_pmu(cpl_pmu_user_);
    std::fprintf(events_file, ",\"pmu_user_plus_kernel\":");
    write_pmu(cpl_pmu_user_plus_kernel_);
    std::fprintf(events_file, ",\"pmu_kernel_by_class\":{");
    struct NamedKernelPmuClass {
        const char *name;
        CplCycleClass cycle_class;
    };
    static constexpr NamedKernelPmuClass kernel_pmu_classes[] = {
        {"syscall", CplCycleClass::SYSCALL},
        {"page_fault", CplCycleClass::PAGE_FAULT},
        {"irq", CplCycleClass::IRQ},
        {"scheduler", CplCycleClass::SCHEDULER},
        {"idle", CplCycleClass::IDLE},
        {"unknown_kernel", CplCycleClass::UNKNOWN_KERNEL},
    };
    bool first_pmu_class = true;
    for (const auto &item : kernel_pmu_classes) {
        if (!first_pmu_class) std::fputc(',', events_file);
        first_pmu_class = false;
        std::fprintf(events_file, "\"%s\":", item.name);
        write_pmu(cpl_pmu_by_class_[static_cast<size_t>(item.cycle_class)]);
    }
    std::fprintf(events_file, "},\"syscall_profiles\":[");
    bool first_syscall_profile = true;
    for (const auto &entry : cpl_syscalls_) {
        if (!first_syscall_profile) std::fputc(',', events_file);
        first_syscall_profile = false;
        std::fprintf(events_file,
            "{\"sysnum\":%lu,\"count\":%lu,\"kernel_cycles\":%lu,"
            "\"pmu\":",
            (unsigned long)entry.first,
            (unsigned long)entry.second.count,
            (unsigned long)(entry.second.kernel_ticks / period));
        write_pmu(entry.second.pmu);
        std::fputc('}', events_file);
    }
    std::fprintf(events_file, "]");
    const uint64_t attributed_memory_uops =
        cpl_packet_attributed_uops_ + cpl_fallback_attributed_uops_ +
        cpl_explicitly_rejected_uops_;
    const uint64_t unaccounted_memory_uops =
        cpl_committed_memory_uops_ >= attributed_memory_uops
            ? cpl_committed_memory_uops_ - attributed_memory_uops : 0;
    const uint64_t duplicate_memory_uops =
        attributed_memory_uops > cpl_committed_memory_uops_
            ? attributed_memory_uops - cpl_committed_memory_uops_ : 0;
    std::fprintf(events_file,
        ",\"memory_accounting\":{\"committed_memory_uops\":%lu,"
        "\"packet_attributed_uops\":%lu,"
        "\"fallback_attributed_uops\":%lu,"
        "\"explicitly_rejected_uops\":%lu,"
        "\"line_requests\":%lu,\"unaccounted_uops\":%lu,"
        "\"duplicate_accounting_uops\":%lu,"
        "\"dtlb_unknown_uops\":%lu,"
        "\"late_packets_after_fallback\":%lu}",
        (unsigned long)cpl_committed_memory_uops_,
        (unsigned long)cpl_packet_attributed_uops_,
        (unsigned long)cpl_fallback_attributed_uops_,
        (unsigned long)cpl_explicitly_rejected_uops_,
        (unsigned long)cpl_line_requests_,
        (unsigned long)unaccounted_memory_uops,
        (unsigned long)duplicate_memory_uops,
        (unsigned long)cpl_dtlb_unknown_uops_,
        (unsigned long)cpl_late_packets_after_fallback_);
    const auto frontend = TaoTraceFrontendRegistry::snapshot(
        uint32_t(std::max<int32_t>(0, cpl_core_id_)));
    const auto terminal = [&](TaoTraceFrontendRegistry::Terminal value) {
        return frontend.terminals[static_cast<unsigned>(value)];
    };
    const uint64_t frontend_latency_cycles =
        frontend.requestToResponseTicks / period;
    std::fprintf(events_file,
        ",\"frontend_accounting\":{"
        "\"schema\":\"taotrace-scoped-frontend-v1\","
        "\"scope\":\"exact-cpl-first-event-to-functional-target-window\","
        "\"inflight_at_start\":%lu,\"requests_started\":%lu,"
        "\"user_mode_requests_started\":%lu,"
        "\"kernel_mode_requests_started\":%lu,"
        "\"invalid_same_block_refetches\":%lu,"
        "\"invalid_new_block_requests\":%lu,"
        "\"valid_block_changes\":%lu,"
        "\"translations_completed\":%lu,"
        "\"icache_send_attempts\":%lu,"
        "\"icache_requests_sent\":%lu,"
        "\"icache_send_rejects\":%lu,"
        "\"translation_squashes\":%lu,"
        "\"translation_faults\":%lu,"
        "\"no_good_address_terminals\":%lu,"
        "\"retry_discards\":%lu,"
        "\"icache_responses\":%lu,"
        "\"icache_squashed_responses\":%lu,"
        "\"inflight_at_end\":%lu,"
        "\"squash_events\":%lu,"
        "\"squash_events_with_outstanding\":%lu,"
        "\"status_cycle_samples\":%lu,"
        "\"running_cycles\":%lu,\"idle_cycles\":%lu,"
        "\"squashing_cycles\":%lu,\"blocked_cycles\":%lu,"
        "\"fetching_cycles\":%lu,\"trap_pending_cycles\":%lu,"
        "\"quiesce_pending_cycles\":%lu,\"itlb_wait_cycles\":%lu,"
        "\"icache_wait_response_cycles\":%lu,"
        "\"icache_wait_retry_cycles\":%lu,"
        "\"icache_access_complete_cycles\":%lu,"
        "\"ftq_wait_cycles\":%lu,\"no_good_addr_cycles\":%lu,"
        "\"request_to_response_ticks\":%lu,"
        "\"request_to_response_cycles\":%lu,"
        "\"request_to_response_samples\":%lu,"
        "\"request_population_conserved\":%s,"
        "\"request_mode_conserved\":%s,"
        "\"request_reason_conserved\":%s,"
        "\"send_accounting_conserved\":%s}",
        (unsigned long)frontend.inflightAtStart,
        (unsigned long)frontend.requestsStarted,
        (unsigned long)frontend.userModeRequestsStarted,
        (unsigned long)frontend.kernelModeRequestsStarted,
        (unsigned long)frontend.invalidSameBlockRefetches,
        (unsigned long)frontend.invalidNewBlockRequests,
        (unsigned long)frontend.validBlockChanges,
        (unsigned long)frontend.translationsCompleted,
        (unsigned long)frontend.sendAttempts,
        (unsigned long)frontend.requestsSent,
        (unsigned long)frontend.sendRejects,
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::TranslationSquash),
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::TranslationFault),
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::NoGoodAddress),
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::RetryDiscard),
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::Response),
        (unsigned long)terminal(
            TaoTraceFrontendRegistry::Terminal::SquashedResponse),
        (unsigned long)frontend.inflightAtEnd,
        (unsigned long)frontend.squashEvents,
        (unsigned long)frontend.squashEventsWithOutstanding,
        (unsigned long)frontend.statusCycleCount(),
        (unsigned long)frontend.statusCycles[0],
        (unsigned long)frontend.statusCycles[1],
        (unsigned long)frontend.statusCycles[2],
        (unsigned long)frontend.statusCycles[3],
        (unsigned long)frontend.statusCycles[4],
        (unsigned long)frontend.statusCycles[5],
        (unsigned long)frontend.statusCycles[6],
        (unsigned long)frontend.statusCycles[7],
        (unsigned long)frontend.statusCycles[8],
        (unsigned long)frontend.statusCycles[9],
        (unsigned long)frontend.statusCycles[10],
        (unsigned long)frontend.statusCycles[11],
        (unsigned long)frontend.statusCycles[12],
        (unsigned long)frontend.requestToResponseTicks,
        (unsigned long)frontend_latency_cycles,
        (unsigned long)frontend.requestToResponseSamples,
        frontend.requestPopulationConserved() ? "true" : "false",
        frontend.requestModeConserved() ? "true" : "false",
        frontend.requestReasonConserved() ? "true" : "false",
        frontend.sendAccountingConserved() ? "true" : "false");
    std::fprintf(events_file, ",\"event_counts\":{"
        "\"syscall\":%lu,\"page_fault\":%lu,\"irq\":%lu,"
        "\"scheduler\":%lu,\"idle\":%lu,\"unknown_kernel\":%lu},"
        "\"irq_vectors\":{",
        (unsigned long)entries(CplCycleClass::SYSCALL),
        (unsigned long)entries(CplCycleClass::PAGE_FAULT),
        (unsigned long)entries(CplCycleClass::IRQ),
        (unsigned long)entries(CplCycleClass::SCHEDULER),
        (unsigned long)entries(CplCycleClass::IDLE),
        (unsigned long)entries(CplCycleClass::UNKNOWN_KERNEL));
    bool first_vector = true;
    for (const auto &entry : cpl_irq_vectors_) {
        if (!first_vector) std::fputc(',', events_file);
        first_vector = false;
        std::fprintf(events_file, "\"%lu\":%lu",
            (unsigned long)entry.first, (unsigned long)entry.second);
    }
    std::fprintf(events_file, "},\"unknown_kernel_sources\":[");
    bool first_unknown_source = true;
    for (const auto &entry : cpl_unknown_sources_) {
        if (!first_unknown_source) std::fputc(',', events_file);
        first_unknown_source = false;
        // Fault and generated instruction names are static gem5 identifiers;
        // replace the only two JSON-significant bytes defensively.
        std::string source = entry.first;
        for (char &ch : source) {
            if (ch == '\"' || ch == '\\') ch = '_';
        }
        std::fprintf(events_file, "{\"source\":\"%s\",\"count\":%lu}",
            source.c_str(), (unsigned long)entry.second);
    }
    std::fprintf(events_file, "]}\n");
    std::fclose(events_file);
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

    if (pkt->req) {
        auto response =
            pkt->req->getExtension<TaoTraceRubyResponseExtension>();
        if (response) {
            a.native_hierarchy_request_count = response->hierarchyRequests;
            a.native_response_count = response->responses;
            a.native_external_hits = response->externalHits;
            a.native_external_misses = response->externalMisses;
            a.native_coalesced = response->coalesced;
            a.native_responder_machine_mask =
                response->responderMachineMask;
            a.native_responder_machine_unknown =
                response->responderMachineUnknown;
            a.native_has_admission_tick = response->hasAdmissionTick;
            a.native_first_admission_tick = response->firstAdmissionTick;
            a.native_last_admission_tick = response->lastAdmissionTick;
            a.native_has_response_tick = response->hasResponseTick;
            a.native_last_response_tick = response->lastResponseTick;
            a.native_hierarchy = response->hierarchy;
        }
    }

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

    // V10.3 A 字段：在 L3 LRU touch 之前 peek，与 ref_sim 同时机/同公式 → bit-exact
    {
        uint32_t res = 0, pos = 0;
        l3_lru_.peekSetState(cl, &res, &pos);
        a.d_llc_set_residency = (res > 31) ? 31 : uint8_t(res);
        a.d_llc_set_lru_pos   = (pos > 31) ? 31 : uint8_t(pos);
    }

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
    if (!isSyscallBoundary(inst) || !tc) return;
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
    sc.user_rsp = tc->getReg(X86ISA::int_reg::Rsp);
    sc.address_space =
        tc->readMiscRegNoEffect(X86ISA::misc_reg::Cr3) & ~uint64_t(0xfff);
    // SYSCALL, SYSENTER and INT 0x80 are all two-byte x86 gateways.  This
    // return-PC component, together with CR3 and the saved user stack, lets a
    // syscall which resumes on another simulated CPU find its origin row.
    sc.return_pc = inst->pcState().instAddr() + 2;
    sc.pre_timestamp_us = uint64_t(curTick()) / sim_clock::as_int::us;
    sc.pre_cpu = fst_core_id_ >= 0
        ? uint32_t(fst_core_id_) : getCoreId(inst);
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
    if (!emitGateOpen(core_id)) return;  // V28.1 per-core ROI 闸门
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
        const auto pending_cpl = pending_cpl_data_class_.find(key);
        if (pending_cpl != pending_cpl_data_class_.end()) {
            accountCplDataPmu(
                emit_oracle, pending_cpl->second.cycle_class,
                pending_cpl->second.syscall_number,
                pending_cpl->second.line_requests,
                pending_cpl->second.dtlb_outcome);
            ++cpl_fallback_attributed_uops_;
            writeNativeResponseCommit(
                inst, emit_oracle, pending_cpl->second.cycle_class,
                pending_cpl->second.line_requests, true);
            pending_cpl_data_class_.erase(pending_cpl);
        }
        // V3：commit 时为每条 mem-touching micro 写一行 mem_event
        // V9.6：ROI gate 短路 mem_events 的两条 emit；line_states_/LRU
        //   等内部状态机由本函数其它路径 always-update，不受闸门影响。
        if (out_mem_events_ && emitGateOpen(acc.core_id)) {
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
                "\"same_line_recent\":%u,"
                "\"d_mshr_depth\":%u,\"dtlb_hit\":%u,"
                "\"d_walker_levels\":%u,\"d_walker_dram_misses\":%u,"
                "\"d_bank_id\":%u,"
                "\"d_llc_set_residency\":%u,\"d_llc_set_lru_pos\":%u}\n",
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
                (unsigned)emit_oracle.same_line_recent_bucket,
                (unsigned)emit_oracle.d_mshr_depth,
                (unsigned)emit_oracle.dtlb_hit,
                (unsigned)emit_oracle.d_walker_levels,
                (unsigned)emit_oracle.d_walker_dram_misses,
                (unsigned)emit_oracle.d_bank_id,
                (unsigned)emit_oracle.d_llc_set_residency,
                (unsigned)emit_oracle.d_llc_set_lru_pos);
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
    if (si->isControl() && inst->branchPredMispredicted()) {
        acc.last_mispredicted = true;
    }

    acc.last_commit_tick = curTick();

    // Predictor-independent committed branch facts.  DynInst::pcState holds
    // the resolved architectural next state at retirement; advancePC moves a
    // clone to that actual successor.  pcState().branching() is also the
    // actual direction used by O3's execute/commit path, not predTaken.
    const bool is_retired_branch = si->isControl();
    const uint16_t branch_history_before = branch_history_[tid];
    bool branch_taken = false;
    uint64_t branch_next_pc = 0;
    uint64_t branch_target = 0;
    if (is_retired_branch) {
        branch_taken = inst->pcState().branching();
        std::unique_ptr<PCStateBase> next_pc(inst->pcState().clone());
        si->advancePC(*next_pc);
        branch_next_pc = uint64_t(next_pc->instAddr());
        branch_target = branch_taken ? branch_next_pc : 0;
        acc.is_branch = true;
        acc.branch_taken = branch_taken;
        acc.branch_next_pc = branch_next_pc;
        acc.branch_target = branch_target;
        acc.branch_history_before = branch_history_before;
    }

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
                        i_attr, branch_taken, branch_target, branch_next_pc,
                        branch_history_before);
    }

    // Update only from the committed actual direction, after the current UOP
    // has observed history_before.  This is not the predictor's speculative
    // GHR and is safe as a functional feature.
    if (is_retired_branch) {
        uint16_t hist = branch_history_[tid];
        hist = uint16_t((hist << 1) | (branch_taken ? 1u : 0u));
        branch_history_[tid] = hist;
    }

    const bool boundary = !si->isMicroop() || si->isLastMicroop();
    if (boundary) {
        acc.is_branch = acc.is_branch || si->isControl();
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

    writeRecordsLine(acc, it, mo, acc.branch_target, st,
                     acc.branch_history_before, acc.access_distance_bucket);
    writeLabelsLine(acc.core_id, acc.thread_id, acc.first_seq,
                    exposed_cyc, macro_cyc, branch_pen_cyc,
                    fetch_cyc, exec_cyc, branch_mispred);
    writeDiagLine(acc.core_id, acc.thread_id, acc.first_seq,
                  acc.first_fetch_tick, acc.last_commit_tick, prev,
                  last_squash_tick_[acc.thread_id]);

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
    if (!emitGateOpen(acc.core_id)) return;  // V28.1 per-core ROI 闸门
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
        "\"is_branch\":%u,\"branch_taken\":%u,"
        "\"branch_target\":%" PRIu64 ",\"branch_next_pc\":%" PRIu64 ","
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
        acc.is_branch ? 1u : 0u, acc.branch_taken ? 1u : 0u,
        branch_target, acc.branch_next_pc,
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
    if (!emitGateOpen(core_id)) return;  // V28.1 per-core ROI 闸门
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
    if (!emitGateOpen(core_id)) return;  // V28.1 per-core ROI 闸门
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
    if (!emitGateOpen(core_id)) return;  // V28.1 per-core ROI 闸门
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
                          const InstSharedAttr &i_oracle,
                          bool branch_taken,
                          uint64_t branch_target,
                          uint64_t branch_next_pc,
                          uint16_t branch_history)
{
    if (!emit_micro_) return;
    if (!out_records_micro_ && !out_labels_micro_ && !out_fst_) return;
    if (functionalTraceEnabled() && !functionalCaptureActive()) return;
    if (functionalTargetReached()) {
        return;
    }
    uint32_t core_id   = getCoreId(inst);
    uint32_t thread_id = getTraceThreadId(inst);
    // V9.6 ROI 闸门：require_roi_=true 且 ROI 关闭时短路。
    //   注意：last_writer_per_thread_ / micro_seq_per_thread_ 也都在 emit 侧
    //   维护——ROI 关闭期间不递增；从而 ROI 内首条 µop 仍然得到 micro_seq=1，
    //   producer-distance 在 ROI 段内严格按段内顺序；ROI 跨段衔接由
    //   last_writer 自动处理（跨段的写者 micro_seq 可能 < 当前段，
    //   计算出的 dist 仍然合法且单调）。
    if (!emitGateOpen(core_id)) return;

    auto si = inst->staticInst;
    if (!si) return;
    const uint8_t instruction_cpl = instructionCpl(inst);
    const bool functional_user = instruction_cpl == 3;
    const uint8_t encoded_cpl =
        functional_include_kernel_ ? instruction_cpl : 3;
    const bool warmup_kernel_idle =
        !functional_measurement_started_ &&
        (isIdleInstruction(inst) || isPollIdleInstruction(inst));
    if (functional_include_kernel_ && !functional_user &&
        (warmup_kernel_idle ||
         (functional_measurement_started_ &&
          (!cpl_initialized_ || cpl_cycle_class_ == CplCycleClass::IDLE)))) {
        return;
    }

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
    uint8_t destination_class_counts[4] = {0u, 0u, 0u, 0u};
    size_t tracked_destination_count = 0;
    const size_t nd = si->numDestRegs();
    for (size_t i = 0; i < nd; ++i) {
        const RegId &r = si->destRegIdx(i);
        int cls = static_cast<int>(r.classValue());
        if (!isTrackableRegClassMicro(cls)) continue;
        const uint8_t encoded_class = encodeRegClassValueMicro(cls);
        if (encoded_class >= 4 ||
            destination_class_counts[encoded_class] == 31) {
            throw std::runtime_error(
                "TaoTrace destination register class exceeds FST v7 width");
        }
        ++destination_class_counts[encoded_class];
        ++tracked_destination_count;
        dsts.emplace_back(encoded_class,
                          static_cast<uint32_t>(r.index()));
    }
    std::sort(srcs.begin(), srcs.end());
    srcs.erase(std::unique(srcs.begin(), srcs.end()), srcs.end());
    std::sort(dsts.begin(), dsts.end());
    dsts.erase(std::unique(dsts.begin(), dsts.end()), dsts.end());

    // Keep dependency identities de-duplicated, but retain operand counts for
    // n_dst and per-class free-list demand.  This matches
    // UnifiedRenameMap::canRename(), which checks numDestRegs(class).
    if (tracked_destination_count > UINT8_MAX) {
        throw std::runtime_error(
            "TaoTrace destination count exceeds FST v7 width");
    }

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
    const uint64_t address_space_id = inst->tcBase()
        ? (inst->tcBase()->readMiscRegNoEffect(
               X86ISA::misc_reg::Cr3) & ~uint64_t(0xfff))
        : 0;
    if (out_fst_) {
        noteFstAddressSpace(address_space_id);
        observeFstStaticInstruction(
            inst, is_microop == 0 || is_last_microop != 0);
    }

    uint64_t macro_pc = inst->pcState().instAddr();
    uint32_t micro_pc = uint32_t(inst->pcState().microPC());
    uint64_t cacheline = (vaddr & ~uint64_t(63));
    // V10 paddr-line：与 mem_events.commit.cacheline_addr (paddr) 同口径，
    //   方便下游用 paddr-key 与 mem_events / Ruby evict 流做 join。
    uint64_t cacheline_paddr = (paddr & ~uint64_t(63));
    const bool is_memory_instruction =
        si->isLoad() || si->isStore() || si->isAtomic();
    const bool pte_address_space_matches =
        is_memory_instruction && pteAddressSpaceMatches(inst);
    const auto initial_pte = pte_address_space_matches
        ? initial_pte_present_.find(vaddr >> kFstVpageBits)
        : initial_pte_present_.end();
    const bool initial_pte_state_valid =
        initial_pte != initial_pte_present_.end();
    const bool initial_pte_present =
        initial_pte_state_valid && initial_pte->second;
    const auto measurement_pte = pte_address_space_matches
        ? measurement_pte_present_.find(vaddr >> kFstVpageBits)
        : measurement_pte_present_.end();
    const bool measurement_pte_state_valid =
        measurement_pte != measurement_pte_present_.end();
    const bool measurement_pte_present =
        measurement_pte_state_valid && measurement_pte->second;
    const bool measurement_boundary_inflight_fault =
        pte_address_space_matches &&
        measurement_boundary_inflight_fault_pages_.count(
            vaddr >> kFstVpageBits) != 0;

    uint64_t fetch_tick    = inst->fetchTick != Tick(-1)
                                ? uint64_t(inst->fetchTick) : 0;
    uint64_t issue_tick    = inst->issueTick != -1
                                ? uint64_t(inst->issueTick) : 0;
    uint64_t complete_tick = inst->completeTick != -1
                                ? uint64_t(inst->completeTick) : 0;
    uint64_t commit_tick   = uint64_t(curTick());
    int mispredicted =
        (si->isControl() && inst->branchPredMispredicted()) ? 1 : 0;

    if (out_records_micro_) {
        std::fprintf(out_records_micro_,
            "{\"core_id\":%u,\"thread_id\":%u,\"micro_seq\":%" PRIu64 ","
            "\"address_space_id\":%" PRIu64 ","
            "\"seq_num\":%" PRIu64 ","
            "\"macro_pc\":%" PRIu64 ",\"micro_pc\":%u,"
            "\"vaddr\":%" PRIu64 ",\"paddr\":%" PRIu64 ","
            "\"initial_pte_state_valid\":%u,"
            "\"initial_pte_present\":%u,"
            "\"measurement_pte_state_valid\":%u,"
            "\"measurement_pte_present\":%u,"
            "\"measurement_boundary_inflight_fault\":%u,"
            "\"cacheline_addr\":%" PRIu64 ",\"cacheline_paddr\":%" PRIu64 ","
            "\"size\":%u,"
            "\"is_load\":%d,\"is_store\":%d,\"is_atomic\":%d,"
            "\"is_branch\":%d,\"is_branch_cond\":%d,\"is_branch_indirect\":%d,"
            "\"is_call\":%d,\"is_return\":%d,"
            "\"branch_taken\":%u,\"branch_target\":%" PRIu64 ","
            "\"branch_next_pc\":%" PRIu64 ",\"branch_history\":%u,"
            "\"is_int\":%d,\"is_fp\":%d,\"is_simd\":%d,\"is_serialize\":%d,"
            "\"op_class\":%d,\"cpl\":%u,"
            "\"is_microop\":%d,\"is_last_microop\":%d,"
            "\"n_src\":%u,\"n_dst\":%u,"
            "\"producer_dists\":[%u,%u,%u,%u],"
            "\"producer_classes\":[%u,%u,%u,%u],"
            "\"destination_class_counts\":[%u,%u,%u,%u],"
            "\"mesi_before\":%u,\"coh_oracle\":%u,"
            "\"sharer_bucket\":%u,\"owner_dist\":%u,\"dirty_owner\":%u,"
            "\"path_class\":%u,\"inval_fanout\":%u,\"same_line_recent\":%u,"
            "\"oracle_source\":%u,"
            "\"i_path_class\":%u,\"i_coh_oracle\":%u,"
            "\"i_mesi_before\":%u,\"i_oracle_source\":%u,"
            "\"d_mshr_depth\":%u,\"dtlb_hit\":%u,"
            "\"d_walker_levels\":%u,\"d_walker_dram_misses\":%u,"
            "\"d_bank_id\":%u,"
            "\"i_mshr_depth\":%u,\"itlb_hit\":%u,"
            "\"i_walker_levels\":%u,\"i_walker_dram_misses\":%u,"
            "\"i_bank_id\":%u,"
            "\"d_llc_set_residency\":%u,\"d_llc_set_lru_pos\":%u,"
            "\"i_llc_set_residency\":%u,\"i_llc_set_lru_pos\":%u}\n",
            core_id, thread_id, micro_seq, address_space_id,
            uint64_t(inst->seqNum),
            macro_pc, micro_pc,
            vaddr, paddr,
            initial_pte_state_valid ? 1u : 0u,
            initial_pte_present ? 1u : 0u,
            measurement_pte_state_valid ? 1u : 0u,
            measurement_pte_present ? 1u : 0u,
            measurement_boundary_inflight_fault ? 1u : 0u,
            cacheline, cacheline_paddr, unsigned(size),
            si->isLoad() ? 1 : 0,
            si->isStore() ? 1 : 0,
            si->isAtomic() ? 1 : 0,
            si->isControl() ? 1 : 0,
            si->isCondCtrl() ? 1 : 0,
            si->isIndirectCtrl() ? 1 : 0,
            si->isCall() ? 1 : 0,
            si->isReturn() ? 1 : 0,
            branch_taken ? 1u : 0u, branch_target, branch_next_pc,
            unsigned(branch_history),
            si->isInteger() ? 1 : 0,
            si->isFloating() ? 1 : 0,
            si->isVector() ? 1 : 0,
            si->isSerializing() ? 1 : 0,
            int(si->opClass()), unsigned(encoded_cpl),
            is_microop, is_last_microop,
            unsigned(srcs.size()), unsigned(tracked_destination_count),
            p_dists[0], p_dists[1], p_dists[2], p_dists[3],
            unsigned(p_classes[0]), unsigned(p_classes[1]),
            unsigned(p_classes[2]), unsigned(p_classes[3]),
            unsigned(destination_class_counts[0]),
            unsigned(destination_class_counts[1]),
            unsigned(destination_class_counts[2]),
            unsigned(destination_class_counts[3]),
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
            unsigned(i_oracle.oracle_source),
            unsigned(oracle.d_mshr_depth),
            unsigned(oracle.dtlb_hit),
            unsigned(oracle.d_walker_levels),
            unsigned(oracle.d_walker_dram_misses),
            unsigned(oracle.d_bank_id),
            unsigned(i_oracle.i_mshr_depth),
            unsigned(i_oracle.itlb_hit),
            unsigned(i_oracle.i_walker_levels),
            unsigned(i_oracle.i_walker_dram_misses),
            unsigned(i_oracle.i_bank_id),
            unsigned(oracle.d_llc_set_residency),
            unsigned(oracle.d_llc_set_lru_pos),
            unsigned(i_oracle.i_llc_set_residency),
            unsigned(i_oracle.i_llc_set_lru_pos));
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

    if (out_fst_) {
        // 直写 FST schema-7：producer class 放低三位，对应的 committed
        //   Int/Float/Vec/CC destination count 放高五位。每条记录均置 packing
        //   marker，包括 n_dst=0 的记录，使 header feature 不会掩盖逐记录缺失。
        //   header core_id 已在 openOutput 从 probe name 解析并写入。
        FstRecord rec;
        rec.pc = macro_pc;
        // JSON 行始终带 "paddr" key，故转换器对每条记录都置 kPhysicalAddr，
        //   address 取 paddr（非访存时为 0）。
        rec.address = paddr;
        rec.flags = uint16_t(rec.flags | kFstPhysicalAddr);
        rec.target = branch_target;
        rec.next_pc = branch_next_pc;
        rec.size = size;
        const bool is_branch = si->isControl();
        auto set_flag = [&rec](FstFlag f, bool v) {
            if (v) rec.flags = uint16_t(rec.flags | f);
        };
        set_flag(kFstLoad, si->isLoad());
        set_flag(kFstStore, si->isStore());
        set_flag(kFstAtomic, si->isAtomic());
        set_flag(kFstBranch, is_branch);
        set_flag(kFstConditional, si->isCondCtrl());
        set_flag(kFstIndirect, si->isIndirectCtrl());
        set_flag(kFstCall, si->isCall());
        set_flag(kFstReturn, si->isReturn());
        set_flag(kFstTaken, branch_taken);
        // 行内 branch_taken/branch_next_pc key 恒在，故 outcome-valid 等价于 is_branch。
        set_flag(kFstBranchOutcome, is_branch);
        set_flag(kFstMicroOp, is_microop != 0);
        set_flag(kFstLastMicroOp, is_last_microop != 0);
        set_flag(kFstSerialize, si->isSerializing());
        const int op_class = int(si->opClass());
        if (encoded_cpl != 3 && op_class > INT16_MAX - 2) {
            throw std::runtime_error(
                "TaoTrace kernel OpClass exceeds FST privilege width");
        }
        rec.op_class = encoded_cpl == 3
            ? int16_t(op_class) : int16_t(-op_class - 2);
        if (encoded_cpl != 3) {
            fst_feature_flags_ |= kFstFeaturePrivilege;
        }
        rec.n_src = uint8_t(std::min<size_t>(srcs.size(), 255));
        rec.n_dst = uint8_t(tracked_destination_count);
        for (int k = 0; k < 4; ++k) {
            rec.producer_dists[k] = p_dists[k];
            const uint8_t producer = p_classes[k] == 255 ? 7 : p_classes[k];
            rec.producer_classes[k] = uint8_t(
                (destination_class_counts[k] << 3) | producer);
        }
        rec.reserved = kFstDestClassMarker;
        fst_feature_flags_ |= kFstFeatureDestClass;
        // Tokenize every valid committed memory reference. PTE-state bits are
        // still known only for the snapshot root, but page identity itself is
        // portable across all observed address spaces.
        if (is_memory_instruction) {
            const uint64_t voff = vaddr & (kFstVpageBytes - 1);
            const uint64_t poff = paddr & (kFstVpageBytes - 1);
            const bool crosses_page =
                size != 0 && voff + size > kFstVpageBytes;
            // 同页偏移一致性由 translation 保证；不一致则保守跳过 token（不崩溃）。
            if (!crosses_page && voff == poff) {
                const uint64_t vpage = vaddr >> kFstVpageBits;
                const uint64_t ppage = paddr >> kFstVpageBits;
                const auto identity = std::make_tuple(
                    address_space_id, vpage, ppage);
                auto found = fst_page_tokens_.find(identity);
                uint32_t token;
                if (found != fst_page_tokens_.end()) {
                    token = found->second;
                } else if (fst_next_page_token_ != 0 &&
                           fst_next_page_token_ < kFstDestClassMarker) {
                    token = fst_next_page_token_++;
                    fst_page_tokens_.emplace(identity, token);
                    uint32_t mapping_flags =
                        kFstVirtualPageMapPhysicalValid;
                    if (initial_pte_state_valid) {
                        mapping_flags |=
                            kFstVirtualPageMapInitialPteStateValid;
                        if (initial_pte_present) {
                            mapping_flags |=
                                kFstVirtualPageMapInitialPtePresent;
                        }
                    }
                    fst_virtual_page_mappings_.push_back(
                        FstVirtualPageMapping{
                            token, mapping_flags, fst_record_count_,
                            address_space_id, vpage, ppage});
                } else {
                    token = 0;  // 31-bit 溢出：与转换器 overflow 行为不同，
                                //   但 10M ROI 远不会触及，保守不置 token。
                }
                if (token != 0) {
                    rec.reserved |= token;
                    rec.flags = uint16_t(rec.flags | kFstVpageToken);
                }
            }
        }
        if (rec.flags & kFstVpageToken) {
            fst_feature_flags_ |= kFstFeatureVpageTokens;
        }
        std::fwrite(&rec, sizeof(rec), 1, out_fst_);
        ++fst_record_count_;
    }

    if (out_records_micro_ || out_fst_) {
        noteFunctionalRecordEmitted(
            is_microop == 0 || is_last_microop != 0, functional_user);
    }

    // 更新 last_writer（micro 粒度）
    for (auto &kv : dsts) {
        uint32_t key = encodeReg(kv.first, kv.second);
        lw[key] = micro_seq;
    }
}

void
TaoTrace::writeFstHeader()
{
    if (!out_fst_) return;
    FstHeader header;
    header.core_id = (fst_core_id_ >= 0) ? uint32_t(fst_core_id_) : 0u;
    header.record_count = fst_record_count_;
    header.feature_flags = fst_feature_flags_;
    header.reserved[3] = kFstLinuxX86_64;
    if (fst_finalized_ && !fst_syscall_metadata_.empty()) {
        header.reserved[0] = sizeof(FstHeader) +
            fst_record_count_ * sizeof(FstRecord);
        header.reserved[1] = fst_syscall_metadata_.size();
        header.reserved[2] = sizeof(FstSyscallMetadataV1);
    }
    std::fseek(out_fst_, 0, SEEK_SET);
    if (std::fwrite(&header, sizeof(header), 1, out_fst_) != 1) {
        throw std::runtime_error("TaoTrace failed to write FST header");
    }
    std::fseek(out_fst_, 0, SEEK_END);
}

void
TaoTrace::noteFstAddressSpace(uint64_t address_space_id)
{
    if (!out_fst_ || fst_finalized_) return;
    if (address_space_id == 0) {
        throw std::runtime_error(
            "TaoTrace cannot emit an FST record without a CR3 root");
    }
    if (!fst_address_space_transitions_.empty() &&
        fst_address_space_transitions_.back().address_space_id ==
            address_space_id) {
        return;
    }
    if (fst_address_space_transitions_.empty() && fst_record_count_ != 0) {
        throw std::runtime_error(
            "TaoTrace address-space map must begin at record zero");
    }
    if (!fst_address_space_transitions_.empty()) {
        // .imap v2 is PC-only. Once multiple CR3 roots are observed, retain
        // no cross-process PC facts; finalization suppresses the companion.
        fst_static_instructions_.clear();
        fst_static_pending_.clear();
    }
    fst_address_space_transitions_.push_back(
        FstAddressSpaceTransition{fst_record_count_, address_space_id});
}

void
TaoTrace::observeFstStaticInstruction(
    const DynInstPtr &inst, bool completes_instruction)
{
    if (!inst || !inst->staticInst) return;
    const auto si = inst->staticInst;
    const uint32_t thread_id = getTraceThreadId(inst);
    const uint64_t pc = inst->pcState().instAddr();
    auto &pending = fst_static_pending_[thread_id];
    auto suppress_static_pc = [this, &pending](uint64_t unsupported_pc) {
        if (pending.valid) {
            fst_static_unsupported_pcs_.insert(pending.pc);
            fst_static_instructions_.erase(pending.pc);
        }
        fst_static_unsupported_pcs_.insert(unsupported_pc);
        fst_static_instructions_.erase(unsupported_pc);
        pending = FstStaticInstructionPending{};
    };
    const uint8_t size = inst->pcState().as<X86ISA::PCState>().size();
    if (size == 0 || size > 15 || pc > UINT64_MAX - size) {
        if (!functional_include_kernel_) {
            throw std::runtime_error(
                "TaoTrace cannot encode invalid x86 instruction geometry");
        }
        // gem5 can commit kernel microcode/pseudo instructions whose PCState
        // has no architectural x86 length.  They remain valid dynamic FST
        // records, but cannot contribute a portable static .imap entry.
        suppress_static_pc(pc);
        return;
    }
    if (fst_static_unsupported_pcs_.count(pc) != 0) return;

    if (pending.valid && (pending.pc != pc || pending.size != size)) {
        if (!functional_include_kernel_) {
            throw std::runtime_error(
                "TaoTrace observed a new macro before completing its static "
                "map entry");
        }
        suppress_static_pc(pending.pc);
    }
    if (!pending.valid) {
        if (functional_include_kernel_ && si->isMicroop() &&
            !si->isFirstMicroop()) {
            suppress_static_pc(pc);
            return;
        }
        pending = FstStaticInstructionPending{};
        pending.valid = true;
        pending.pc = pc;
        pending.size = size;
        pending.fallthrough_pc = pc + size;
    }

    auto add_mask = [](std::array<uint64_t, 2> &mask, uint32_t id) {
        if (id >= 128) {
            throw std::runtime_error(
                "TaoTrace canonical register ID exceeds imap v2 width");
        }
        mask[id / 64] |= uint64_t(1) << (id % 64);
    };
    for (size_t index = 0; index < si->numSrcRegs(); ++index) {
        uint32_t canonical = 0;
        if (!fstCanonicalRegister(si->srcRegIdx(index), canonical)) continue;
        const uint64_t bit = uint64_t(1) << (canonical % 64);
        if ((pending.written_register_mask[canonical / 64] & bit) == 0) {
            add_mask(pending.read_register_mask, canonical);
        }
    }
    for (size_t index = 0; index < si->numDestRegs(); ++index) {
        uint32_t canonical = 0;
        if (!fstCanonicalRegister(si->destRegIdx(index), canonical)) continue;
        add_mask(pending.write_register_mask, canonical);
        add_mask(pending.written_register_mask, canonical);
    }

    auto add_flag = [&pending](FstStaticFlag flag, bool enabled) {
        if (enabled) pending.flags = uint16_t(pending.flags | flag);
    };
    add_flag(kFstStaticBranch, si->isControl());
    add_flag(kFstStaticConditional, si->isCondCtrl());
    add_flag(kFstStaticIndirect, si->isIndirectCtrl());
    add_flag(kFstStaticCall, si->isCall());
    add_flag(kFstStaticReturn, si->isReturn());
    add_flag(kFstStaticMemory,
             si->isLoad() || si->isStore() || si->isAtomic());
    if (si->isDirectCtrl()) {
        // The DynInst PCState at commit contains the resolved dynamic NPC.
        // X86MicroopBase::branchTarget adds its displacement to NPC, so feed
        // it a clean architectural fallthrough state instead of leaking the
        // taken/not-taken outcome into this static companion.
        X86ISA::PCState static_pc(pc);
        static_pc.size(size);
        static_pc.npc(pc + size);
        const auto target = si->branchTarget(static_pc);
        const uint64_t direct_target = target->instAddr();
        if ((pending.flags & kFstStaticDirectTargetValid) != 0 &&
            pending.direct_target != direct_target) {
            if (!functional_include_kernel_) {
                throw std::runtime_error(
                    "TaoTrace direct target changed within one macro");
            }
            suppress_static_pc(pc);
            return;
        }
        pending.direct_target = direct_target;
        pending.flags = uint16_t(
            pending.flags | kFstStaticBranch |
            kFstStaticDirectTargetValid);
    }

    if (!completes_instruction) return;
    FstStaticInstruction completed;
    completed.pc = pending.pc;
    completed.fallthrough_pc = pending.fallthrough_pc;
    completed.direct_target = pending.direct_target;
    completed.flags = pending.flags;
    completed.size = pending.size;
    completed.read_register_mask = pending.read_register_mask;
    completed.write_register_mask = pending.write_register_mask;
    const auto [found, inserted] =
        fst_static_instructions_.emplace(completed.pc, completed);
    if (!inserted) {
        auto &prior = found->second;
        const bool prior_target_valid =
            (prior.flags & kFstStaticDirectTargetValid) != 0;
        const bool completed_target_valid =
            (completed.flags & kFstStaticDirectTargetValid) != 0;
        if (prior.fallthrough_pc != completed.fallthrough_pc ||
            prior.size != completed.size ||
            (prior_target_valid && completed_target_valid &&
             prior.direct_target != completed.direct_target)) {
            if (functional_include_kernel_) {
                suppress_static_pc(completed.pc);
                return;
            }
            throw std::runtime_error(
                "TaoTrace static instruction geometry/target conflicts at PC " +
                std::to_string(completed.pc));
        }
        // REP and other x86 microcode paths can expose different subsets of
        // the same macro's architectural operands on different executions.
        // The portable static row is the conservative may-read/may-write
        // union, never a dynamic outcome or frequency.
        prior.flags = uint16_t(prior.flags | completed.flags);
        if (!prior_target_valid && completed_target_valid) {
            prior.direct_target = completed.direct_target;
        }
        for (size_t word = 0; word < 2; ++word) {
            prior.read_register_mask[word] |=
                completed.read_register_mask[word];
            prior.write_register_mask[word] |=
                completed.write_register_mask[word];
        }
    }
    pending = FstStaticInstructionPending{};
}

void
TaoTrace::writeFstVirtualPageMap()
{
    if (fst_path_.empty() || fst_virtual_page_mappings_.empty()) return;
    const std::string path = fst_path_ + ".vmap";
    std::FILE *output = std::fopen(path.c_str(), "wb");
    if (!output) {
        throw std::runtime_error(
            "TaoTrace failed to open FST virtual-page map");
    }
    FstVirtualPageMapHeaderV1 header;
    header.core_id = fst_core_id_ >= 0 ? uint32_t(fst_core_id_) : 0u;
    header.source_record_count = fst_record_count_;
    header.entry_count = fst_virtual_page_mappings_.size();
    if (std::fwrite(&header, sizeof(header), 1, output) != 1) {
        std::fclose(output);
        throw std::runtime_error(
            "TaoTrace failed to write FST virtual-page map header");
    }
    for (const auto &mapping : fst_virtual_page_mappings_) {
        FstVirtualPageMapEntryV1 entry;
        entry.token = mapping.token;
        entry.flags = mapping.flags;
        const auto measurement = mapping.address_space_id == initial_pte_root_
            ? measurement_pte_present_.find(mapping.virtual_page)
            : measurement_pte_present_.end();
        if (measurement != measurement_pte_present_.end()) {
            entry.flags |= kFstVirtualPageMapMeasurementPteStateValid;
            if (measurement->second) {
                entry.flags |= kFstVirtualPageMapMeasurementPtePresent;
            }
        }
        if (mapping.address_space_id == initial_pte_root_ &&
            measurement_boundary_inflight_fault_pages_.count(
                mapping.virtual_page) != 0) {
            entry.flags |=
                kFstVirtualPageMapMeasurementBoundaryInflightFault;
        }
        entry.first_record_ordinal = mapping.first_record_ordinal;
        entry.virtual_page = mapping.virtual_page;
        entry.physical_page = mapping.physical_page;
        if (std::fwrite(&entry, sizeof(entry), 1, output) != 1) {
            std::fclose(output);
            throw std::runtime_error(
                "TaoTrace failed to write FST virtual-page map entry");
        }
    }
    if (std::fflush(output) != 0 || std::fclose(output) != 0) {
        throw std::runtime_error(
            "TaoTrace failed to finalize FST virtual-page map");
    }
}

void
TaoTrace::writeFstAddressSpaceMap()
{
    if (fst_path_.empty() || fst_address_space_transitions_.empty()) return;
    if (fst_record_count_ == 0) return;
    if (fst_address_space_transitions_.front().record_ordinal != 0) {
        throw std::runtime_error(
            "TaoTrace address-space map must start at record zero");
    }
    const std::string path = fst_path_ + ".asmap";
    std::FILE *output = std::fopen(path.c_str(), "wb");
    if (!output) {
        throw std::runtime_error(
            "TaoTrace failed to open FST address-space map");
    }
    FstAddressSpaceMapHeaderV1 header;
    header.core_id = fst_core_id_ >= 0 ? uint32_t(fst_core_id_) : 0u;
    header.source_record_count = fst_record_count_;
    header.entry_count = fst_address_space_transitions_.size();
    if (std::fwrite(&header, sizeof(header), 1, output) != 1) {
        std::fclose(output);
        throw std::runtime_error(
            "TaoTrace failed to write FST address-space map header");
    }
    for (const auto &transition : fst_address_space_transitions_) {
        FstAddressSpaceMapEntryV1 entry;
        entry.record_ordinal = transition.record_ordinal;
        entry.address_space_id = transition.address_space_id;
        if (std::fwrite(&entry, sizeof(entry), 1, output) != 1) {
            std::fclose(output);
            throw std::runtime_error(
                "TaoTrace failed to write FST address-space map entry");
        }
    }
    if (std::fflush(output) != 0 || std::fclose(output) != 0) {
        throw std::runtime_error(
            "TaoTrace failed to finalize FST address-space map");
    }
}

void
TaoTrace::writeFstInstructionMap()
{
    if (fst_path_.empty() || fst_static_instructions_.empty()) return;
    if (fst_address_space_transitions_.size() > 1) {
        // .imap v2 has no ASID field. Suppressing it is the only sound
        // behavior until an AS-scoped instruction-map schema is introduced.
        std::remove((fst_path_ + ".imap").c_str());
        return;
    }
    for (auto &[thread_id, pending] : fst_static_pending_) {
        (void)thread_id;
        if (pending.valid) {
            if (!functional_include_kernel_) {
                throw std::runtime_error(
                    "TaoTrace cannot finalize an incomplete static macro "
                    "entry");
            }
            fst_static_unsupported_pcs_.insert(pending.pc);
            fst_static_instructions_.erase(pending.pc);
            pending = FstStaticInstructionPending{};
        }
    }
    const std::string path = fst_path_ + ".imap";
    std::FILE *output = std::fopen(path.c_str(), "wb");
    if (!output) {
        throw std::runtime_error(
            "TaoTrace failed to open FST instruction map");
    }
    FstInstructionMapHeaderV2 header;
    header.core_id = fst_core_id_ >= 0 ? uint32_t(fst_core_id_) : 0u;
    header.source_record_count = fst_record_count_;
    header.entry_count = fst_static_instructions_.size();
    if (std::fwrite(&header, sizeof(header), 1, output) != 1) {
        std::fclose(output);
        throw std::runtime_error(
            "TaoTrace failed to write FST instruction-map header");
    }
    for (const auto &[pc, instruction] : fst_static_instructions_) {
        (void)pc;
        FstInstructionMapEntryV2 entry;
        entry.pc = instruction.pc;
        entry.fallthrough_pc = instruction.fallthrough_pc;
        entry.direct_target = instruction.direct_target;
        entry.flags = instruction.flags;
        entry.size = instruction.size;
        std::copy(instruction.read_register_mask.begin(),
                  instruction.read_register_mask.end(),
                  std::begin(entry.read_register_mask));
        std::copy(instruction.write_register_mask.begin(),
                  instruction.write_register_mask.end(),
                  std::begin(entry.write_register_mask));
        if (std::fwrite(&entry, sizeof(entry), 1, output) != 1) {
            std::fclose(output);
            throw std::runtime_error(
                "TaoTrace failed to write FST instruction-map entry");
        }
    }
    if (std::fflush(output) != 0 || std::fclose(output) != 0) {
        throw std::runtime_error(
            "TaoTrace failed to finalize FST instruction map");
    }
}

void
TaoTrace::finalizeFst()
{
    if (!out_fst_ || fst_finalized_) return;
    const uint64_t records_end = sizeof(FstHeader) +
        fst_record_count_ * sizeof(FstRecord);
    if (std::fseek(out_fst_, long(records_end), SEEK_SET) != 0) {
        throw std::runtime_error("TaoTrace failed to seek to FST metadata");
    }
    for (const auto &source : fst_syscall_metadata_) {
        FstSyscallMetadataV1 row;
        row.record_ordinal = source.record_ordinal;
        row.syscall_ordinal = source.syscall_ordinal;
        row.thread_id = source.thread_id;
        row.syscall_number = source.number;
        std::copy(source.arguments.begin(), source.arguments.end(),
                  std::begin(row.arguments));
        row.return_value_raw = source.return_value_raw;
        row.pre_timestamp_us = source.pre_timestamp_us;
        row.post_timestamp_us = source.post_timestamp_us;
        row.errno_value = source.errno_value;
        row.pre_cpu = source.pre_cpu;
        row.post_cpu = source.post_cpu;
        row.valid_fields = source.valid_fields;
        row.argument_count = source.argument_count;
        row.flags = source.flags;
        if (std::fwrite(&row, sizeof(row), 1, out_fst_) != 1) {
            throw std::runtime_error(
                "TaoTrace failed to write FST syscall metadata");
        }
    }
    if (!fst_syscall_metadata_.empty()) {
        fst_feature_flags_ |= kFstFeatureSyscallMetadata;
    }
    fst_finalized_ = true;
    writeFstHeader();
    writeFstVirtualPageMap();
    writeFstAddressSpaceMap();
    writeFstInstructionMap();
    DPRINTF(TaoTrace,
        "FST v7 finalized records=%llu syscalls=%llu returns=%llu "
        "address_space_runs=%llu static_instructions=%llu\n",
        (unsigned long long)fst_record_count_,
        (unsigned long long)fst_syscall_metadata_.size(),
        (unsigned long long)fst_completed_syscall_returns_,
        (unsigned long long)fst_address_space_transitions_.size(),
        (unsigned long long)fst_static_instructions_.size());
}

// -------------------------------------------------------------------------
// Probe callbacks
// -------------------------------------------------------------------------

void
TaoTrace::noteWrongPathStage(const DynInstPtr &inst, const char *stage)
{
    if (!emit_wrong_path_oracle_ || !inst) return;
    const uint32_t hardware_thread_id = uint32_t(inst->threadNumber);
    const auto finalized = wrong_path_finalized_.find(uint64_t(inst->seqNum));
    if (finalized != wrong_path_finalized_.end() &&
        finalized->second == hardware_thread_id) {
        return;
    }
    const uint32_t core_id = getCoreId(inst);
    if (wrong_path_core_id_ < 0) {
        wrong_path_core_id_ = int32_t(core_id);
    }
    if (inst->cpu) wrong_path_instances_[inst->cpu] = this;
    WrongPathPending &pending = wrong_path_pending_[uint64_t(inst->seqNum)];
    pending.core_id = wrong_path_core_id_ >= 0
        ? uint32_t(wrong_path_core_id_) : core_id;
    pending.hardware_thread_id = hardware_thread_id;
    pending.context_id = getTraceThreadId(inst);
    pending.seq_num = uint64_t(inst->seqNum);
    pending.macro_pc = uint64_t(inst->pcState().instAddr());
    pending.micro_pc = uint32_t(inst->pcState().microPC());
    if (inst->staticInst) {
        const auto &si = inst->staticInst;
        auto x86 = dynamic_cast<const X86ISA::X86StaticInst *>(si.get());
        pending.cpl = x86 ? uint8_t(x86->machInst.mode.cpl)
                          : (isUserInstruction(inst) ? 3u : 0u);
        pending.op_class = int32_t(si->opClass());
        pending.n_src = uint16_t(si->numSrcRegs());
        pending.n_dst = uint16_t(si->numDestRegs());
        pending.is_microop = si->isMicroop();
        pending.is_last_microop = si->isLastMicroop();
        pending.is_load = si->isLoad();
        pending.is_store = si->isStore();
        pending.is_atomic = si->isAtomic();
        pending.is_control = si->isControl();
        pending.is_conditional = si->isCondCtrl();
        pending.is_indirect = si->isIndirectCtrl();
        pending.is_call = si->isCall();
        pending.is_return = si->isReturn();
        pending.identity_valid = true;
        if ((pending.is_load || pending.is_store || pending.is_atomic) &&
            (std::strcmp(stage, "execute") == 0 ||
             std::strcmp(stage, "to_commit") == 0)) {
            pending.vaddr = uint64_t(inst->effAddr);
            pending.paddr = uint64_t(inst->physEffAddr);
            pending.size = uint16_t(inst->effSize);
        }
    }
    if (inst->issueTick != -1) {
        pending.dyn_issue_tick = uint64_t(inst->issueTick);
    }
    if (inst->completeTick != -1) {
        pending.dyn_complete_tick = uint64_t(inst->completeTick);
    }
    const uint64_t tick = uint64_t(curTick());
    if (std::strcmp(stage, "fetch") == 0) {
        pending.fetched = true;
        pending.fetch_tick = inst->fetchTick != Tick(-1)
            ? uint64_t(inst->fetchTick) : tick;
    } else if (std::strcmp(stage, "rename") == 0) {
        pending.renamed = true;
        pending.rename_tick = tick;
    } else if (std::strcmp(stage, "dispatch") == 0) {
        pending.dispatched = true;
        pending.dispatch_tick = tick;
    } else if (std::strcmp(stage, "execute") == 0) {
        pending.execute_seen = true;
        pending.execute_tick = tick;
    } else if (std::strcmp(stage, "to_commit") == 0) {
        pending.to_commit_seen = true;
        pending.to_commit_tick = tick;
    }
}

void
TaoTrace::noteWrongPathDataComplete(const DynInstPtr &inst)
{
    if (!emit_wrong_path_oracle_ || !inst) return;
    auto it = wrong_path_pending_.find(uint64_t(inst->seqNum));
    if (it == wrong_path_pending_.end()) {
        auto late = wrong_path_late_memory_.find(uint64_t(inst->seqNum));
        if (late != wrong_path_late_memory_.end() &&
            late->second.hardware_thread_id == uint32_t(inst->threadNumber)) {
            writeWrongPathLateDataComplete(late->second, inst);
            wrong_path_late_memory_.erase(late);
        }
        return;
    }
    WrongPathPending &pending = it->second;
    pending.data_complete_seen = true;
    pending.data_complete_tick = uint64_t(curTick());
    if (inst->issueTick != -1) {
        pending.dyn_issue_tick = uint64_t(inst->issueTick);
    }
    if (inst->completeTick != -1) {
        pending.dyn_complete_tick = uint64_t(inst->completeTick);
    }
    pending.vaddr = uint64_t(inst->effAddr);
    pending.paddr = uint64_t(inst->physEffAddr);
    pending.size = uint16_t(inst->effSize);
}

void
TaoTrace::writeWrongPathLateDataComplete(
    const WrongPathLateMemory &late, const DynInstPtr &inst)
{
    if (!global_wrong_path_oracle_ || late.episode_id == 0 || !inst) return;
    const uint64_t completion_tick = uint64_t(curTick());
    std::fprintf(global_wrong_path_oracle_,
        "{\"schema\":\"taotrace-wrong-path-oracle-v3\"," 
        "\"record\":\"late_data_complete\",\"episode_id\":%lu," 
        "\"core_id\":%u,\"hardware_thread_id\":%u,\"seq_num\":%lu," 
        "\"cpl\":%u,\"squash_tick\":%lu,\"data_complete_tick\":%lu," 
        "\"measurement_gate_open\":%u,\"is_load\":%u," 
        "\"is_store\":%u,\"is_atomic\":%u,\"vaddr\":%lu," 
        "\"paddr\":%lu,\"size\":%u}\n",
        (unsigned long)late.episode_id, late.core_id,
        late.hardware_thread_id, (unsigned long)late.seq_num,
        unsigned(late.cpl), (unsigned long)late.squash_tick,
        (unsigned long)completion_tick,
        wrongPathMeasurementGateOpen() ? 1u : 0u,
        late.is_load ? 1u : 0u, late.is_store ? 1u : 0u,
        late.is_atomic ? 1u : 0u, (unsigned long)inst->effAddr,
        (unsigned long)inst->physEffAddr, unsigned(inst->effSize));
    std::fflush(global_wrong_path_oracle_);
}

void
TaoTrace::eraseWrongPathCommitted(const DynInstPtr &inst)
{
    if (!emit_wrong_path_oracle_ || !inst) return;
    const uint64_t committed_seq = uint64_t(inst->seqNum);
    const uint32_t hardware_thread_id = uint32_t(inst->threadNumber);
    for (auto it = wrong_path_pending_.begin();
         it != wrong_path_pending_.end() && it->first <= committed_seq;) {
        if (it->second.hardware_thread_id == hardware_thread_id) {
            it = wrong_path_pending_.erase(it);
        } else {
            ++it;
        }
    }
    for (auto it = wrong_path_finalized_.begin();
         it != wrong_path_finalized_.end() && it->first <= committed_seq;) {
        if (it->second == hardware_thread_id) {
            it = wrong_path_finalized_.erase(it);
        } else {
            ++it;
        }
    }
}

bool
TaoTrace::wrongPathMeasurementGateOpen() const
{
    return emit_wrong_path_oracle_ && global_wrong_path_oracle_ &&
           cplMeasurementActive() && !functionalTargetReached();
}

void
TaoTrace::writeWrongPathInstruction(uint64_t episode_id,
                                    const char *cause,
                                    uint64_t squash_tick,
                                    const WrongPathPending &pending)
{
    if (!global_wrong_path_oracle_ || !pending.identity_valid) return;
    std::fprintf(global_wrong_path_oracle_,
        "{\"schema\":\"taotrace-wrong-path-oracle-v3\"," 
        "\"record\":\"instruction\",\"episode_id\":%lu," 
        "\"cause\":\"%s\",\"core_id\":%u," 
        "\"hardware_thread_id\":%u,\"context_id\":%u," 
        "\"seq_num\":%lu,\"macro_pc\":%lu,\"micro_pc\":%u,\"cpl\":%u," 
        "\"op_class\":%d,\"is_microop\":%u,\"is_last_microop\":%u," 
        "\"is_load\":%u,\"is_store\":%u,\"is_atomic\":%u," 
        "\"is_control\":%u,\"is_conditional\":%u," 
        "\"is_indirect\":%u,\"is_call\":%u,\"is_return\":%u," 
        "\"n_src\":%u,\"n_dst\":%u," 
        "\"fetch_tick\":%lu,\"rename_tick\":%lu," 
        "\"dispatch_tick\":%lu,\"execute_probe_tick\":%lu," 
        "\"to_commit_probe_tick\":%lu,\"data_complete_tick\":%lu," 
        "\"squash_tick\":%lu,\"dyn_issue_tick\":%lu," 
        "\"dyn_complete_tick\":%lu,\"fetched\":%u,\"renamed\":%u," 
        "\"dispatched\":%u,\"execute_seen\":%u," 
        "\"to_commit_seen\":%u,\"data_complete_seen\":%u," 
        "\"vaddr\":%lu,\"paddr\":%lu,\"size\":%u}\n",
        (unsigned long)episode_id, cause,
        pending.core_id, pending.hardware_thread_id, pending.context_id,
        (unsigned long)pending.seq_num,
        (unsigned long)pending.macro_pc, pending.micro_pc,
        unsigned(pending.cpl), pending.op_class,
        pending.is_microop ? 1u : 0u, pending.is_last_microop ? 1u : 0u,
        pending.is_load ? 1u : 0u, pending.is_store ? 1u : 0u,
        pending.is_atomic ? 1u : 0u, pending.is_control ? 1u : 0u,
        pending.is_conditional ? 1u : 0u, pending.is_indirect ? 1u : 0u,
        pending.is_call ? 1u : 0u, pending.is_return ? 1u : 0u,
        unsigned(pending.n_src), unsigned(pending.n_dst),
        (unsigned long)pending.fetch_tick,
        (unsigned long)pending.rename_tick,
        (unsigned long)pending.dispatch_tick,
        (unsigned long)pending.execute_tick,
        (unsigned long)pending.to_commit_tick,
        (unsigned long)pending.data_complete_tick,
        (unsigned long)squash_tick,
        (unsigned long)pending.dyn_issue_tick,
        (unsigned long)pending.dyn_complete_tick,
        pending.fetched ? 1u : 0u, pending.renamed ? 1u : 0u,
        pending.dispatched ? 1u : 0u, pending.execute_seen ? 1u : 0u,
        pending.to_commit_seen ? 1u : 0u,
        pending.data_complete_seen ? 1u : 0u,
        (unsigned long)pending.vaddr, (unsigned long)pending.paddr,
        unsigned(pending.size));
}

void
TaoTrace::emitWrongPathEpisode(uint32_t hardware_thread_id,
                               const char *cause,
                               bool include_cause,
                               uint64_t cause_seq,
                               uint64_t cutoff_seq,
                               uint64_t rob_youngest_seq,
                               uint64_t cause_pc,
                               uint64_t redirect_pc)
{
    std::vector<uint64_t> selected;
    uint64_t fetched = 0, renamed = 0, dispatched = 0, issued = 0, completed = 0;
    uint64_t memory = 0, data_completed = 0, loads = 0, stores = 0;
    uint64_t user_records = 0, kernel_records = 0, unknown_cpl_records = 0;
    uint32_t cause_cpl = 0xff;
    const auto cause_it = wrong_path_pending_.find(cause_seq);
    if (cause_it != wrong_path_pending_.end() &&
        cause_it->second.hardware_thread_id == hardware_thread_id) {
        cause_cpl = cause_it->second.cpl;
    }
    for (auto it = wrong_path_pending_.upper_bound(cutoff_seq);
         it != wrong_path_pending_.end(); ++it) {
        const WrongPathPending &pending = it->second;
        if (pending.hardware_thread_id != hardware_thread_id) {
            continue;
        }
        selected.push_back(it->first);
        if (pending.cpl == 3) {
            ++user_records;
        } else if (pending.cpl <= 2) {
            ++kernel_records;
        } else {
            ++unknown_cpl_records;
        }
        fetched += pending.fetched ? 1 : 0;
        renamed += pending.renamed ? 1 : 0;
        dispatched += pending.dispatched ? 1 : 0;
        issued += pending.execute_seen ? 1 : 0;
        completed += pending.to_commit_seen ? 1 : 0;
        loads += pending.is_load ? 1 : 0;
        stores += pending.is_store ? 1 : 0;
        memory += (pending.is_load || pending.is_store || pending.is_atomic)
            ? 1 : 0;
        data_completed += pending.data_complete_seen ? 1 : 0;
    }

    const uint32_t core_id = wrong_path_core_id_ >= 0
        ? uint32_t(wrong_path_core_id_) : 0u;
    const bool emit = wrongPathMeasurementGateOpen();
    const uint64_t episode_id = emit ? ++global_wrong_path_episode_id_ : 0;
    const uint64_t squash_tick = uint64_t(curTick());
    if (emit) {
        std::fprintf(global_wrong_path_oracle_,
            "{\"schema\":\"taotrace-wrong-path-oracle-v3\"," 
            "\"record\":\"episode\",\"episode_id\":%lu," 
            "\"core_id\":%u,\"hardware_thread_id\":%u," 
            "\"cause\":\"%s\",\"cause_cpl\":%u,\"include_cause\":%u," 
            "\"cause_seq\":%lu,\"cutoff_seq\":%lu," 
            "\"rob_youngest_seq\":%lu,\"cause_pc\":%lu," 
            "\"redirect_pc\":%lu,\"squash_tick\":%lu," 
            "\"instruction_records\":%lu,\"fetched_instructions\":%lu," 
            "\"renamed_instructions\":%lu," 
            "\"dispatched_instructions\":%lu,\"issued_instructions\":%lu," 
            "\"completed_instructions\":%lu,\"memory_instructions\":%lu," 
            "\"data_completed_instructions\":%lu," 
            "\"load_instructions\":%lu,\"store_instructions\":%lu," 
            "\"user_instruction_records\":%lu," 
            "\"kernel_instruction_records\":%lu," 
            "\"unknown_cpl_instruction_records\":%lu}\n",
            (unsigned long)episode_id, core_id, hardware_thread_id, cause,
            cause_cpl, include_cause ? 1u : 0u, (unsigned long)cause_seq,
            (unsigned long)cutoff_seq, (unsigned long)rob_youngest_seq,
            (unsigned long)cause_pc, (unsigned long)redirect_pc,
            (unsigned long)squash_tick, (unsigned long)selected.size(),
            (unsigned long)fetched,
            (unsigned long)renamed, (unsigned long)dispatched,
            (unsigned long)issued, (unsigned long)completed,
            (unsigned long)memory, (unsigned long)data_completed,
            (unsigned long)loads, (unsigned long)stores,
            (unsigned long)user_records, (unsigned long)kernel_records,
            (unsigned long)unknown_cpl_records);
        for (const uint64_t seq : selected) {
            writeWrongPathInstruction(episode_id, cause, squash_tick,
                                      wrong_path_pending_.at(seq));
        }
        std::fflush(global_wrong_path_oracle_);
    }
    if (emit) {
        for (const uint64_t seq : selected) {
            const auto &pending = wrong_path_pending_.at(seq);
            const bool memory = pending.is_load || pending.is_store ||
                pending.is_atomic;
            if (!memory || !pending.execute_seen ||
                pending.data_complete_seen) {
                continue;
            }
            wrong_path_late_memory_[seq] = WrongPathLateMemory{
                episode_id, pending.core_id, pending.hardware_thread_id,
                pending.seq_num, squash_tick, pending.cpl,
                pending.is_load, pending.is_store, pending.is_atomic};
        }
    }
    for (const uint64_t seq : selected) {
        wrong_path_finalized_[seq] = hardware_thread_id;
    }
    for (const uint64_t seq : selected) wrong_path_pending_.erase(seq);
}

void
TaoTrace::emitWrongPathFallback(const DynInstPtr &inst)
{
    if (!emit_wrong_path_oracle_ || !inst) return;
    auto it = wrong_path_pending_.find(uint64_t(inst->seqNum));
    if (it == wrong_path_pending_.end()) return;
    const uint32_t core_id = wrong_path_core_id_ >= 0
        ? uint32_t(wrong_path_core_id_) : getCoreId(inst);
    if (wrongPathMeasurementGateOpen()) {
        const uint64_t episode_id = ++global_wrong_path_episode_id_;
        const uint64_t squash_tick = uint64_t(curTick());
        const uint64_t fallback_cutoff = uint64_t(inst->seqNum) > 0
            ? uint64_t(inst->seqNum) - 1 : 0;
        std::fprintf(global_wrong_path_oracle_,
            "{\"schema\":\"taotrace-wrong-path-oracle-v3\"," 
            "\"record\":\"episode\",\"episode_id\":%lu," 
            "\"core_id\":%u,\"hardware_thread_id\":%u," 
            "\"cause\":\"unattributed_commit_squash\"," 
            "\"cause_cpl\":%u,\"include_cause\":true,\"cause_seq\":%lu," 
            "\"cutoff_seq\":%lu,\"rob_youngest_seq\":%lu," 
            "\"cause_pc\":0,\"redirect_pc\":0," 
            "\"squash_tick\":%lu,\"instruction_records\":1," 
            "\"fetched_instructions\":%u," 
            "\"renamed_instructions\":%u,\"dispatched_instructions\":%u," 
            "\"issued_instructions\":%u,\"completed_instructions\":%u," 
            "\"memory_instructions\":%u," 
            "\"data_completed_instructions\":%u," 
            "\"load_instructions\":%u,\"store_instructions\":%u," 
            "\"user_instruction_records\":%u," 
            "\"kernel_instruction_records\":%u," 
            "\"unknown_cpl_instruction_records\":%u}\n",
            (unsigned long)episode_id, core_id, unsigned(inst->threadNumber),
            255u, (unsigned long)inst->seqNum,
            (unsigned long)fallback_cutoff,
            (unsigned long)inst->seqNum, (unsigned long)squash_tick,
            it->second.fetched ? 1u : 0u,
            it->second.renamed ? 1u : 0u,
            it->second.dispatched ? 1u : 0u,
            it->second.execute_seen ? 1u : 0u,
            it->second.to_commit_seen ? 1u : 0u,
            (it->second.is_load || it->second.is_store ||
             it->second.is_atomic) ? 1u : 0u,
            it->second.data_complete_seen ? 1u : 0u,
            it->second.is_load ? 1u : 0u,
            it->second.is_store ? 1u : 0u,
            it->second.cpl == 3 ? 1u : 0u,
            it->second.cpl <= 2 ? 1u : 0u,
            it->second.cpl > 3 ? 1u : 0u);
        writeWrongPathInstruction(episode_id, "unattributed_commit_squash",
                                  squash_tick, it->second);
        std::fflush(global_wrong_path_oracle_);
        const auto &pending = it->second;
        const bool memory = pending.is_load || pending.is_store ||
            pending.is_atomic;
        if (memory && pending.execute_seen &&
            !pending.data_complete_seen) {
            wrong_path_late_memory_[uint64_t(inst->seqNum)] =
                WrongPathLateMemory{
                    episode_id, pending.core_id,
                    pending.hardware_thread_id, pending.seq_num,
                    squash_tick, pending.cpl, pending.is_load,
                    pending.is_store, pending.is_atomic};
        }
    }
    wrong_path_finalized_[uint64_t(inst->seqNum)] =
        uint32_t(inst->threadNumber);
    wrong_path_pending_.erase(it);
}

void
TaoTrace::traceSquashEpisode(const CPU *cpu,
                             uint32_t hardware_thread_id,
                             bool branch_mispredict,
                             bool include_cause,
                             uint64_t cause_seq,
                             uint64_t cutoff_seq,
                             uint64_t rob_youngest_seq,
                             uint64_t cause_pc,
                             uint64_t redirect_pc)
{
    auto owner = wrong_path_instances_.find(cpu);
    if (owner == wrong_path_instances_.end() || !owner->second) return;
    owner->second->emitWrongPathEpisode(
        hardware_thread_id,
        branch_mispredict ? "branch_mispredict" : "memory_order",
        include_cause, cause_seq, cutoff_seq, rob_youngest_seq,
        cause_pc, redirect_pc);
}

void
TaoTrace::onFetch(const DynInstPtr &inst)
{
    maybeCaptureInitialPteState(inst);
    noteWrongPathStage(inst, "fetch");
}

void
TaoTrace::onRename(const DynInstPtr &inst)
{
    noteWrongPathStage(inst, "rename");
}

void
TaoTrace::onDispatch(const DynInstPtr &inst)
{
    noteWrongPathStage(inst, "dispatch");
}

void
TaoTrace::onExecute(const DynInstPtr &inst)
{
    noteWrongPathStage(inst, "execute");
    if (isSyscallBoundary(inst)) {
        captureSyscallState(inst);
    }
}

void
TaoTrace::onToCommit(const DynInstPtr &inst)
{
    noteWrongPathStage(inst, "to_commit");
}

void
TaoTrace::onCommitStall(const DynInstPtr & /*inst*/)
{
}

void
TaoTrace::onSquash(const DynInstPtr &inst)
{
    emitWrongPathFallback(inst);
    const uint32_t tid = getTraceThreadId(inst);
    TaoTraceNativeAccessRegistry::noteSquashed(tid, inst->seqNum);
    last_squash_tick_[tid] = curTick();

    if (isSyscallInst(inst) && pending_syscalls_.count(inst->seqNum)) {
        emitSyscallRecord(inst);
    }
    pending_syscalls_.erase(inst->seqNum);
    // Fix A: 防 pending_shared_attr_ 在 squash 路径上泄漏
    const uint64_t data_key =
        (uint64_t(tid) << 48) | uint64_t(inst->seqNum);
    pending_shared_attr_.erase(data_key);
    pending_cpl_data_class_.erase(data_key);

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
    noteWrongPathDataComplete(inst);
    auto si = inst->staticInst;
    if (!si) return;
    if (!(si->isLoad() || si->isStore() || isLockedAtomicMicro(inst))) return;

    const uint32_t tid     = getTraceThreadId(inst);
    const uint32_t core_id = getCoreId(inst);
    const bool is_store    = si->isStore() || isLockedAtomicMicro(inst);

    // ---- V10.3 A 字段：在 deriveSharedAttr（含 L3 LRU touch）之前 peek
    //   L3 set 状态。与 ref_sim simulator.hpp step() 同时机/同公式 → bit-exact。
    uint8_t pre_d_llc_set_residency = 0;
    uint8_t pre_d_llc_set_lru_pos   = 0;
    {
        const uint64_t cl_pre = uint64_t(pkt->getAddr()) & ~uint64_t(63);
        uint32_t res = 0, pos = 0;
        l3_lru_.peekSetState(cl_pre, &res, &pos);
        pre_d_llc_set_residency = (res > 31) ? 31 : uint8_t(res);
        pre_d_llc_set_lru_pos   = (pos > 31) ? 31 : uint8_t(pos);
    }

    SharedAttr a = deriveSharedAttr(pkt, core_id, is_store);
    a.d_llc_set_residency = pre_d_llc_set_residency;
    a.d_llc_set_lru_pos   = pre_d_llc_set_lru_pos;

    // V9.5 d-side：MSHR coalescing 视图（仅记录 outstanding；retire 时
    //   dispatcher 在 accumulateMicro 中调用）。
    const uint64_t cl = uint64_t(pkt->getAddr()) & ~uint64_t(63);
    // P0-A：先捕获 insert 之前 outstanding 数（= 其他在飞 mem-req 数），
    //   与 ref_sim simulator.hpp step() 中 insert+retire 之后取 size() 同语义
    //   （sequential refsim 中 size_after_retire == 本事件之前 outstanding）。
    {
        size_t md = getL1dMshr(core_id).size();
        a.d_mshr_depth = (md > 15) ? 15 : uint8_t(md);
    }
    getL1dMshr(core_id).insert(cl, /*seq=*/inst->seqNum);

    // V9.5 dTLB + page walker：把 paddr 视图也喂给 walker，让 oracle 与
    //   ref_sim 看到同一 LRU 序列。这里仅 touch；fail 时不影响 SharedAttr 推断。
    // P0-A：捕获 dtlb_hit + walker WalkResult 用于 SharedAttr 输出。
    bool dtlb_hit = getDtlb(core_id).translate(uint64_t(pkt->getAddr()));
    tao_uarch::PageWalkSim::WalkResult wr;
    if (!dtlb_hit) {
        // miss → walker 走多级页表，结果会同时 touch L1d/L2/L3 LRU
        wr = walker_.walk(uint64_t(pkt->getAddr()),
                          getL1d(core_id), getL2(core_id), l3_lru_);
    }
    a.dtlb_hit = dtlb_hit ? 1 : 0;
    {
        uint32_t wl = wr.levels;
        a.d_walker_levels = (wl > 7) ? 7 : uint8_t(wl);
        uint32_t wd = wr.miss_dram;
        a.d_walker_dram_misses = (wd > 7) ? 7 : uint8_t(wd);
    }
    {
        uint32_t bid = getL1d(core_id).bankIdOf(cl);
        a.d_bank_id = (bid > 15) ? 15 : uint8_t(bid);
    }

    // Attribute data PMU only after retirement. A normal load completes first
    // and waits in pending_shared_attr_. A late store finds the class frozen
    // by accountCplCommit. This excludes speculative/squashed accesses and
    // prevents completion-time CPL changes from moving an access across
    // user/syscall/IRQ domains.
    const uint64_t key =
        (uint64_t(tid) << 48) | uint64_t(inst->seqNum);
    const auto committed = pending_cpl_data_class_.find(key);
    if (committed != pending_cpl_data_class_.end()) {
        accountCplDataPmu(
            a, committed->second.cycle_class,
            committed->second.syscall_number,
            committed->second.line_requests,
            committed->second.dtlb_outcome);
        ++cpl_packet_attributed_uops_;
        writeNativeResponseCommit(
            inst, a, committed->second.cycle_class,
            committed->second.line_requests, false);
        pending_cpl_data_class_.erase(committed);
    } else if (!inst->isCommitted()) {
        // A split access can complete through more than one physical Request
        // before its DynInst retires.  The proxy attributes intentionally use
        // the latest packet (one PMU attribution per committed memory UOP),
        // but native Ruby facts are fragment populations and must be merged.
        const auto previous = pending_shared_attr_.find(key);
        if (previous != pending_shared_attr_.end()) {
            a.native_hierarchy_request_count +=
                previous->second.native_hierarchy_request_count;
            a.native_response_count += previous->second.native_response_count;
            a.native_external_hits += previous->second.native_external_hits;
            a.native_external_misses += previous->second.native_external_misses;
            a.native_coalesced += previous->second.native_coalesced;
            a.native_responder_machine_mask |=
                previous->second.native_responder_machine_mask;
            a.native_responder_machine_unknown +=
                previous->second.native_responder_machine_unknown;
            if (previous->second.native_has_admission_tick) {
                if (!a.native_has_admission_tick) {
                    a.native_has_admission_tick = true;
                    a.native_first_admission_tick =
                        previous->second.native_first_admission_tick;
                    a.native_last_admission_tick =
                        previous->second.native_last_admission_tick;
                } else {
                    a.native_first_admission_tick = std::min(
                        a.native_first_admission_tick,
                        previous->second.native_first_admission_tick);
                    a.native_last_admission_tick = std::max(
                        a.native_last_admission_tick,
                        previous->second.native_last_admission_tick);
                }
            }
            if (previous->second.native_has_response_tick) {
                a.native_has_response_tick = true;
                a.native_last_response_tick = std::max(
                    a.native_last_response_tick,
                    previous->second.native_last_response_tick);
            }
            a.native_hierarchy.merge(previous->second.native_hierarchy);
        }
        pending_shared_attr_[key] = a;
    } else {
        writeNativeResponseLate(inst, a);
        if (measure_cpl_ && cpl_initialized_ && !cpl_finalized_) {
            ++cpl_late_packets_after_fallback_;
        }
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

    // ---- V10.3 A 字段：在 L3-i LRU touch 之前 peek set 状态。
    //   与 ref_sim simulator.hpp stepIFetch() 同时机/同公式 → bit-exact。
    {
        uint32_t res = 0, pos = 0;
        l3_i_lru_.peekSetState(i_cl, &res, &pos);
        ia.i_llc_set_residency = (res > 31) ? 31 : uint8_t(res);
        ia.i_llc_set_lru_pos   = (pos > 31) ? 31 : uint8_t(pos);
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
    // P0-A：捕获 itlb_hit + walker WalkResult。
    bool itlb_hit = getItlb(core_id).translate(i_cl);
    tao_uarch::PageWalkSim::WalkResult iwr;
    if (!itlb_hit) {
        iwr = i_walker_.walk(i_cl,
                             getL1i(core_id), getL2i(core_id), l3_i_lru_);
    }

    // i-side MSHR：记录 outstanding，retire 由本函数自身负责
    //   （fetch 不像 commit 有显式 retire 锚点；将本次访问视作单次 insert+retire）
    // P0-A：i_mshr_depth 取 insert 之前的 outstanding 数（与 ref_sim
    //   stepIFetch insert+retire 后再取 size() 在 sequential 视角下同语义）。
    {
        size_t imd = getL1iMshr(core_id).size();
        ia.i_mshr_depth = (imd > 15) ? 15 : uint8_t(imd);
    }
    getL1iMshr(core_id).insert(i_cl, /*seq=*/global_mem_event_counter_);
    getL1iMshr(core_id).retire(i_cl);

    // P0-A：写入 ia 的 P0-A 字段。
    ia.itlb_hit = itlb_hit ? 1 : 0;
    {
        uint32_t iwl = iwr.levels;
        ia.i_walker_levels = (iwl > 7) ? 7 : uint8_t(iwl);
        uint32_t iwd = iwr.miss_dram;
        ia.i_walker_dram_misses = (iwd > 7) ? 7 : uint8_t(iwd);
    }
    {
        uint32_t ibid = getL1i(core_id).bankIdOf(i_cl);
        ia.i_bank_id = (ibid > 15) ? 15 : uint8_t(ibid);
    }

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
    if (global_mem_events_ && emitGateOpen(core_id)) {
        // V10：cacheline_addr 保留为 vaddr-line（兼容旧 ref_sim/compare_ifetch 逻辑），
        //   同时新增 cacheline_addr_v / cacheline_addr_p 双字段，让下游可双口径 join。
        // P0-A：追加 5 个 i-side 字段，让 ref_sim compare_ifetch 可逐字段 diff。
        std::fprintf(global_mem_events_,
            "{\"seq\":%lu,\"event_type\":\"ifetch\",\"core_id\":%u,"
            "\"cacheline_addr\":%lu,"
            "\"cacheline_addr_v\":%lu,\"cacheline_addr_p\":%lu,"
            "\"cache_level\":4,"
            "\"i_path_class\":%u,\"i_coh_oracle\":%u,"
            "\"i_mesi_before\":%u,"
            "\"i_mshr_depth\":%u,\"itlb_hit\":%u,"
            "\"i_walker_levels\":%u,\"i_walker_dram_misses\":%u,"
            "\"i_bank_id\":%u,"
            "\"i_llc_set_residency\":%u,\"i_llc_set_lru_pos\":%u,"
            "\"commit_tick\":%lu}\n",
            (unsigned long)global_mem_event_counter_++, core_id,
            (unsigned long)i_cl,
            (unsigned long)i_cl, (unsigned long)i_cl_paddr,
            unsigned(ia.path_class), unsigned(ia.coh),
            unsigned(ia.mesi_before),
            unsigned(ia.i_mshr_depth), unsigned(ia.itlb_hit),
            unsigned(ia.i_walker_levels), unsigned(ia.i_walker_dram_misses),
            unsigned(ia.i_bank_id),
            unsigned(ia.i_llc_set_residency), unsigned(ia.i_llc_set_lru_pos),
            (unsigned long)curTick());
    }
}

void
TaoTrace::onPreCommit(const DynInstPtr &inst)
{
    // Commit calls this before commitHead changes the architectural rename
    // map. At the first CPL3 instruction after a syscall, RAX is therefore
    // still the kernel return value even when that user instruction writes
    // RAX itself.
    //
    // Refresh syscall entry registers here for the same reason.  The old
    // Execute-only snapshot read ThreadContext while older argument-producing
    // instructions could still be in flight on O3, yielding a mixture of new
    // and stale ABI registers (for example, a successful mmap with flags=0).
    // At PreCommit all older instructions are architecturally committed and
    // this syscall's own rename-map updates have not happened yet.
    if (isSyscallBoundary(inst)) {
        captureSyscallState(inst);
    }
    maybeCompleteFunctionalSyscall(inst);
}

void
TaoTrace::onCommit(const DynInstPtr &inst)
{
    eraseWrongPathCommitted(inst);
    if (functional_warmup_ && !functional_measurement_started_ &&
        premeasurement_page_fault_pending_ && isUserInstruction(inst)) {
        // The first user commit after a precise #PF proves that its handler
        // completed before the process-wide measurement marker.
        premeasurement_page_fault_pending_ = false;
    }
    const bool syscall_boundary = isSyscallBoundary(inst);
    if (syscall_boundary && functional_warmup_ &&
        !cplMeasurementActive() && isUserInstruction(inst)) {
        emitFunctionalSyscallMarker(
            inst, uint64_t(inst->tcBase()->getReg(X86ISA::int_reg::Rax)));
    }
    if (syscall_boundary) {
        noteCplSyscallEntry(inst);
    }
    accountCplCommit(inst, syscall_boundary);
    if (functionalTraceEnabled()) {
        if (functional_user_only_ && !isUserInstruction(inst)) {
            pending_syscalls_.erase(inst->seqNum);
            pending_shared_attr_.erase(
                (uint64_t(getTraceThreadId(inst)) << 48) |
                uint64_t(inst->seqNum));
            return;
        }
        if (isSyscallMacro(inst)) {
            pending_syscalls_.erase(inst->seqNum);
            pending_shared_attr_.erase(
                (uint64_t(getTraceThreadId(inst)) << 48) |
                uint64_t(inst->seqNum));
            return;
        }
    }
    if (isSyscallInst(inst) && pending_syscalls_.count(inst->seqNum)) {
        emitSyscallRecord(inst);
        pending_syscalls_.erase(inst->seqNum);
        pending_shared_attr_.erase(
            (uint64_t(getTraceThreadId(inst)) << 48) |
            uint64_t(inst->seqNum));
        return;
    }
    accumulateMicro(inst);
    pending_syscalls_.erase(inst->seqNum);
}

} // namespace o3
} // namespace gem5
