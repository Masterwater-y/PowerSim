#ifndef FASTSIM_FST_MMU_CONTROLS_V1_CODEC
#define FASTSIM_FST_MMU_CONTROLS_V1_CODEC
// Explicit little-endian, pure functional MMU controls. Mirrored byte-for-byte
// in taogen/shared/fst_mmu_controls.hh; no native scheduling types belong here.
#include <array>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>
#include <zlib.h>
namespace fastsim::fst {
inline constexpr std::uint64_t kLogicalMmuControls = 1ull << 17;
namespace mmuctl {
using Bytes = std::vector<std::uint8_t>;
using State = std::array<std::uint64_t, 4>;
inline constexpr std::uint32_t NoParent = UINT32_MAX;
inline constexpr std::uint64_t HashSeed = 14695981039346656037ull;
inline std::uint64_t hash_bytes(std::uint64_t h, const void *data, std::size_t n) {
    const auto *p = static_cast<const std::uint8_t *>(data);
    while (n--)
        h = (h ^ *p++) * 1099511628211ull;
    return h;
}
inline std::uint32_t row_hash(const void *data) {
    const auto *p = static_cast<const std::uint8_t *>(data);
    std::uint32_t h = 2166136261u;
    for (unsigned i = 0; i < 64; ++i)
        h = (h ^ p[i]) * 16777619u;
    return h;
}
// Returns false when exact scalar evidence can no longer be represented.
inline bool saturating_add(std::uint64_t &value, std::uint64_t increment = 1) {
    if (increment > UINT64_MAX - value) {
        value = UINT64_MAX;
        return false;
    }
    value += increment;
    return true;
}
inline std::uint64_t body_seed() {
    static constexpr char domain[] = "fst-mmuctl-body-v1";
    return hash_bytes(HashSeed, domain, sizeof(domain));
}
enum class Frame : std::uint16_t {
    Baseline = 1,
    Member = 2,
    Write = 3,
    Command = 4,
    Diagnostic = 5,
    Footer = 6
};
enum class Register : std::uint16_t { Cr0 = 0, Cr3 = 1, Cr4 = 2, Efer = 3 };
enum class Mode : std::uint8_t { WithEffect = 0, NoEffect = 1 };
enum class Application : std::uint8_t { Deferred = 0, Immediate = 1, External = 2 };
enum class CommandKind : std::uint16_t { FlushAll = 0, FlushNonGlobal = 1, DemapPage = 2 };
enum class Domain : std::uint16_t {
    ContextAllTranslations = 0,
    Instruction = 1,
    Data = 2,
    Unified = 3,
    Unknown = 4
};
enum class Reason : std::uint32_t {
    InitialCutUnknown,
    UnboundExternal,
    UnboundPageFault,
    EffectfulSquash,
    FilteredInterior,
    UnsealedOwner,
    EffectOrderUnsupported,
    OwnerOverflow,
    OperationOverflow,
    PropagationUnknown,
    DomainUnsupported,
    LifecycleInWindow,
    StateUnknown,
    CrossContextEffect,
    DiagnosticTruncated,
    CounterOverflow
};
inline constexpr std::array<const char *, 16> ReasonNames = {
    "InitialCutUnknown", "UnboundExternal",    "UnboundPageFault",       "EffectfulSquash",
    "FilteredInterior",  "UnsealedOwner",      "EffectOrderUnsupported", "OwnerOverflow",
    "OperationOverflow", "PropagationUnknown", "DomainUnsupported",      "LifecycleInWindow",
    "StateUnknown",      "CrossContextEffect", "DiagnosticTruncated",    "CounterOverflow"};
inline constexpr std::uint32_t Sealed = 1, Unsupported = 2;
inline constexpr std::uint64_t Finalized = 1, Complete = 2, Empty = 4;
// Coverage: setters, MMU roots, TLB entries, owner scopes, seal, lifecycle.
inline constexpr std::uint32_t RequiredCoverage = 63;
inline const std::string &layout_descriptor() {
    static const std::string s =
        "fst-mmuctl-v1;LE;header160:magic8,version16,size16,endian32,policy256,layout256,"
        "required64,context32,coverage32,limits3x32,reserved32,sizes6x32,reserved192;"
        "baseline80:prefix,ordinal64,hash64,state4x64,known32,generation32,cut64,reserved64;"
        "retained48:prefix,ordinal64,hash64,context32,generation32,count32,status32,reserved64;"
        "write96:prefix,index32,parent32,requested64,before4x64,after4x64,register16,mode8,"
        "application8,known2x8,reserved16;"
        "command48:prefix,index32,parent32,kind16,domain16,flags32,va64,asn64,reserved64;"
        "diagnostic48:prefix,context32,generation32,reason32,count32,zero-owner128,reserved64;"
        "footer256:prefix,status64,hot64,sealed64,total-write64,retained-write64,unbound-write64,"
        "total-command64,retained-command64,unbound-command64,visits64,groups64,suppressed64,"
        "pending64,hot-hash64,body-hash64,reasons16x64;"
        "prefix=kind16,flags16,bytes32;kinds=baseline1,retained2,write3,command4,diagnostic5,"
        "footer6;"
        "register=cr0:0,cr3:1,cr4:2,efer:3;mode=with:0,no:1;application=deferred:0,immediate:1,"
        "external:2;"
        "command=all:0,nonglobal:1,demap:2;domain=context:0,instruction:1,data:2,unified:3,unknown:"
        "4;"
        "reasons=initial-cut,external,pagefault,squash,filtered,unsealed,order,owner-cap,op-cap,"
        "propagation,domain,lifecycle,state,cross-context,truncated,counter;"
        "required=1;coverage=63;known=15;cut=proven:1,empty:2;member=sealed:1,unsupported:2;footer="
        "finalized:1,complete:2,empty:4;"
        "limits=owners4096,ops256,diagnostics256;row-hash=zero-extended-fnv32-64bytes;hot-hash="
        "fnv64;body=fnv64-domain-nul-fst-mmuctl-body-v1;"
        "indices=contiguous-local;parent=earlier-write-or-ffffffff;flush-operands=zero;owner="
        "retained-only;initial-tlb=unknown;footer=terminal;reserved=zero";
    return s;
}
// SHA256 of the canonical descriptor above, generated and checked by tests.
inline constexpr std::array<std::uint8_t, 32> LayoutDigest = {
    0x4b, 0x90, 0xf7, 0x3a, 0x1e, 0x79, 0x70, 0x0a, 0xca, 0x98, 0x34, 0x0d, 0x56, 0xf8, 0xaa, 0xd2,
    0x09, 0x1e, 0xad, 0x18, 0x53, 0x54, 0x2a, 0xfa, 0xc0, 0x28, 0x1c, 0x29, 0x11, 0x3e, 0x24, 0x22};
struct Header {
    std::array<std::uint8_t, 32> policy{};
    std::uint32_t context = 0, coverage = 0, max_owners = 4096, max_ops = 256,
                  max_diagnostics = 256;
};
struct Baseline {
    std::uint64_t ordinal = 0, hash = 0;
    State state{};
    std::uint32_t known = 0, generation = 0;
    std::uint64_t cut = 0;
};
struct Member {
    std::uint64_t ordinal = 0, hash = 0;
    std::uint32_t context = 0, generation = 0, count = 0, status = 0;
};
struct Write {
    std::uint32_t index = 0, parent = NoParent;
    std::uint64_t requested = 0;
    State before{}, after{};
    Register reg = Register::Cr0;
    Mode mode = Mode::WithEffect;
    Application application = Application::Deferred;
    std::uint8_t before_known = 0, after_known = 0;
};
struct Command {
    std::uint32_t index = 0, parent = NoParent;
    CommandKind kind = CommandKind::FlushAll;
    Domain domain = Domain::ContextAllTranslations;
    std::uint64_t va = 0, asn = 0;
};
struct Diagnostic {
    std::uint32_t context = 0, generation = 0;
    Reason reason = Reason::UnboundExternal;
    std::uint32_t count = 0;
};
struct Footer {
    std::uint64_t status = Finalized;
    std::uint64_t hot = 0, sealed = 0, total_writes = 0, retained_writes = 0, unbound_writes = 0,
                  total_commands = 0, retained_commands = 0, unbound_commands = 0, visits = 0,
                  groups = 0, suppressed = 0, pending = 0, hot_hash = HashSeed,
                  body_hash = body_seed();
    std::array<std::uint64_t, 16> reasons{};
};
using Operation = std::variant<Write, Command>;
struct Group {
    Member member;
    std::vector<Operation> operations;
};
[[noreturn]] inline void bad(const char *message) {
    throw std::runtime_error(std::string("MMU controls: ") + message);
}
inline void check(bool ok, const char *message) {
    if (!ok)
        bad(message);
}
inline void put(Bytes &b, std::uint64_t v, unsigned n) {
    while (n--) {
        b.push_back(static_cast<std::uint8_t>(v & 255));
        v >>= 8;
    }
}
struct Cursor {
    const Bytes &b;
    std::size_t p = 0;
    std::uint64_t get(unsigned n) {
        check(p + n <= b.size(), "truncated field");
        std::uint64_t v = 0;
        for (unsigned i = 0; i < n; ++i)
            v |= std::uint64_t(b[p++]) << (8 * i);
        return v;
    }
    void zero(unsigned n) {
        while (n--)
            check(get(1) == 0, "nonzero reserved");
    }
};
inline Bytes prefix(Frame kind, unsigned size) {
    Bytes b;
    put(b, unsigned(kind), 2);
    put(b, 0, 2);
    put(b, size, 4);
    return b;
}
inline Cursor frame(const Bytes &b, Frame kind, unsigned size) {
    check(b.size() == size, "frame size");
    Cursor c{b};
    check(c.get(2) == unsigned(kind) && c.get(2) == 0 && c.get(4) == size, "frame prefix");
    return c;
}
inline Bytes encode(const Header &h) {
    check(h.policy != std::array<std::uint8_t, 32>{} && !(h.coverage & ~RequiredCoverage) &&
              h.max_owners && h.max_owners <= 4096 && h.max_ops && h.max_ops <= 256 &&
              h.max_diagnostics <= 256,
          "header fields");
    Bytes b{'F', 'S', 'T', 'M', 'M', 'U', '1', 0};
    put(b, 1, 2);
    put(b, 160, 2);
    put(b, 0x01020304, 4);
    b.insert(b.end(), h.policy.begin(), h.policy.end());
    b.insert(b.end(), LayoutDigest.begin(), LayoutDigest.end());
    put(b, 1, 8);
    put(b, h.context, 4);
    put(b, h.coverage, 4);
    put(b, h.max_owners, 4);
    put(b, h.max_ops, 4);
    put(b, h.max_diagnostics, 4);
    put(b, 0, 4);
    for (auto n : {80, 48, 96, 48, 48, 256})
        put(b, n, 4);
    b.resize(160);
    return b;
}
inline Header decode_header(const Bytes &b) {
    check(b.size() == 160, "header size");
    Cursor c{b};
    const char magic[8] = {'F', 'S', 'T', 'M', 'M', 'U', '1', 0};
    for (auto x : magic)
        check(c.get(1) == unsigned(x), "magic");
    check(c.get(2) == 1 && c.get(2) == 160 && c.get(4) == 0x01020304, "version/endian");
    Header h;
    for (auto &v : h.policy)
        v = static_cast<std::uint8_t>(c.get(1));
    for (auto v : LayoutDigest)
        check(c.get(1) == v, "layout digest");
    check(c.get(8) == 1, "required protocol flags");
    h.context = static_cast<std::uint32_t>(c.get(4));
    h.coverage = static_cast<std::uint32_t>(c.get(4));
    h.max_owners = static_cast<std::uint32_t>(c.get(4));
    h.max_ops = static_cast<std::uint32_t>(c.get(4));
    h.max_diagnostics = static_cast<std::uint32_t>(c.get(4));
    c.zero(4);
    for (auto n : {80, 48, 96, 48, 48, 256})
        check(c.get(4) == unsigned(n), "frame sizes");
    c.zero(24);
    encode(h);
    return h;
}
inline Bytes encode(const Baseline &r) {
    check(r.hash <= UINT32_MAX && r.ordinal == 0 && r.known <= 15 && r.cut <= 3 &&
              (!(r.cut & 2) || (r.hash == 0 && r.known == 0 && r.state == State{})),
          "baseline fields");
    auto b = prefix(Frame::Baseline, 80);
    put(b, r.ordinal, 8);
    put(b, r.hash, 8);
    for (auto v : r.state)
        put(b, v, 8);
    put(b, r.known, 4);
    put(b, r.generation, 4);
    put(b, r.cut, 8);
    put(b, 0, 8);
    return b;
}
inline Baseline decode_baseline(const Bytes &b) {
    auto c = frame(b, Frame::Baseline, 80);
    Baseline r;
    r.ordinal = c.get(8);
    r.hash = c.get(8);
    for (auto &v : r.state)
        v = c.get(8);
    r.known = static_cast<std::uint32_t>(c.get(4));
    r.generation = static_cast<std::uint32_t>(c.get(4));
    r.cut = c.get(8);
    c.zero(8);
    encode(r);
    return r;
}
inline Bytes encode(const Member &r) {
    check(r.hash <= UINT32_MAX && r.count <= 256 && !(r.status & ~3u), "member fields");
    auto b = prefix(Frame::Member, 48);
    put(b, r.ordinal, 8);
    put(b, r.hash, 8);
    put(b, r.context, 4);
    put(b, r.generation, 4);
    put(b, r.count, 4);
    put(b, r.status, 4);
    put(b, 0, 8);
    return b;
}
inline Member decode_member(const Bytes &b) {
    auto c = frame(b, Frame::Member, 48);
    Member r;
    r.ordinal = c.get(8);
    r.hash = c.get(8);
    r.context = static_cast<std::uint32_t>(c.get(4));
    r.generation = static_cast<std::uint32_t>(c.get(4));
    r.count = static_cast<std::uint32_t>(c.get(4));
    r.status = static_cast<std::uint32_t>(c.get(4));
    c.zero(8);
    encode(r);
    return r;
}
inline Bytes encode(const Write &r) {
    check(unsigned(r.reg) < 4 && unsigned(r.mode) < 2 && unsigned(r.application) < 3 &&
              r.before_known <= 15 && r.after_known <= 15,
          "write enums");
    check(r.parent == NoParent || r.parent < r.index, "write ancestry");
    auto b = prefix(Frame::Write, 96);
    put(b, r.index, 4);
    put(b, r.parent, 4);
    put(b, r.requested, 8);
    for (auto v : r.before)
        put(b, v, 8);
    for (auto v : r.after)
        put(b, v, 8);
    put(b, unsigned(r.reg), 2);
    put(b, unsigned(r.mode), 1);
    put(b, unsigned(r.application), 1);
    put(b, r.before_known, 1);
    put(b, r.after_known, 1);
    put(b, 0, 2);
    return b;
}
inline Write decode_write(const Bytes &b) {
    auto c = frame(b, Frame::Write, 96);
    Write r;
    r.index = static_cast<std::uint32_t>(c.get(4));
    r.parent = static_cast<std::uint32_t>(c.get(4));
    r.requested = c.get(8);
    for (auto &v : r.before)
        v = c.get(8);
    for (auto &v : r.after)
        v = c.get(8);
    r.reg = Register(c.get(2));
    r.mode = Mode(c.get(1));
    r.application = Application(c.get(1));
    r.before_known = static_cast<std::uint8_t>(c.get(1));
    r.after_known = static_cast<std::uint8_t>(c.get(1));
    c.zero(2);
    encode(r);
    return r;
}
inline Bytes encode(const Command &r) {
    check(unsigned(r.kind) < 3 && unsigned(r.domain) < 5 &&
              (r.kind == CommandKind::DemapPage || (!r.va && !r.asn)) &&
              (r.parent == NoParent || r.parent < r.index),
          "command fields");
    auto b = prefix(Frame::Command, 48);
    put(b, r.index, 4);
    put(b, r.parent, 4);
    put(b, unsigned(r.kind), 2);
    put(b, unsigned(r.domain), 2);
    put(b, 0, 4);
    put(b, r.va, 8);
    put(b, r.asn, 8);
    put(b, 0, 8);
    return b;
}
inline Command decode_command(const Bytes &b) {
    auto c = frame(b, Frame::Command, 48);
    Command r;
    r.index = static_cast<std::uint32_t>(c.get(4));
    r.parent = static_cast<std::uint32_t>(c.get(4));
    r.kind = CommandKind(c.get(2));
    r.domain = Domain(c.get(2));
    c.zero(4);
    r.va = c.get(8);
    r.asn = c.get(8);
    c.zero(8);
    encode(r);
    return r;
}
inline Bytes encode(const Diagnostic &r) {
    check(unsigned(r.reason) < 16 && r.count <= 256, "diagnostic fields");
    auto b = prefix(Frame::Diagnostic, 48);
    put(b, r.context, 4);
    put(b, r.generation, 4);
    put(b, unsigned(r.reason), 4);
    put(b, r.count, 4);
    b.resize(48);
    return b;
}
inline Diagnostic decode_diagnostic(const Bytes &b) {
    auto c = frame(b, Frame::Diagnostic, 48);
    Diagnostic r;
    r.context = static_cast<std::uint32_t>(c.get(4));
    r.generation = static_cast<std::uint32_t>(c.get(4));
    r.reason = Reason(c.get(4));
    r.count = static_cast<std::uint32_t>(c.get(4));
    c.zero(24);
    encode(r);
    return r;
}
inline Bytes encode(const Footer &r) {
    check((r.status & Finalized) && !(r.status & ~7ull), "footer status");
    auto b = prefix(Frame::Footer, 256);
    put(b, r.status, 8);
    for (auto v : {r.hot, r.sealed, r.total_writes, r.retained_writes, r.unbound_writes,
                   r.total_commands, r.retained_commands, r.unbound_commands, r.visits, r.groups,
                   r.suppressed, r.pending, r.hot_hash, r.body_hash})
        put(b, v, 8);
    for (auto v : r.reasons)
        put(b, v, 8);
    return b;
}
inline Footer decode_footer(const Bytes &b) {
    auto c = frame(b, Frame::Footer, 256);
    Footer r;
    r.status = c.get(8);
    for (auto *v : {&r.hot, &r.sealed, &r.total_writes, &r.retained_writes, &r.unbound_writes,
                    &r.total_commands, &r.retained_commands, &r.unbound_commands, &r.visits,
                    &r.groups, &r.suppressed, &r.pending, &r.hot_hash, &r.body_hash})
        *v = c.get(8);
    for (auto &v : r.reasons)
        v = c.get(8);
    encode(r);
    return r;
}
inline Bytes encode(const Operation &o) {
    return std::visit([](const auto &v) { return encode(v); }, o);
}
inline void validate_operations(const std::vector<Operation> &ops, unsigned limit) {
    check(ops.size() <= limit, "operation bound");
    for (unsigned i = 0; i < ops.size(); ++i)
        std::visit(
            [&](const auto &op) {
                check(op.index == i, "operation index");
                check(op.parent == NoParent ||
                          (op.parent < i && std::holds_alternative<Write>(ops[op.parent])),
                      "parent write");
                encode(op);
            },
            ops[i]);
}
inline std::string hex(const std::array<std::uint8_t, 32> &a) {
    const char *d = "0123456789abcdef";
    std::string s;
    for (auto v : a) {
        s += d[v >> 4];
        s += d[v & 15];
    }
    return s;
}
// A writer only writes explicitly supplied metadata; it never manufactures a
// baseline, owner seal, native outcome, or footer on destruction.
class Writer {
    gzFile out_ = nullptr;
    std::uint64_t body_ = body_seed();
    bool baseline_ = false, finished_ = false;
    void write(const Bytes &b, bool body) {
        check(out_ && gzwrite(out_, b.data(), unsigned(b.size())) == int(b.size()), "write failed");
        if (body)
            body_ = hash_bytes(body_, b.data(), b.size());
    }

  public:
    explicit Writer(const std::string &path, const Header &h) {
        auto bytes = encode(h);
        out_ = gzopen(path.c_str(), "wb1");
        check(out_, "open failed");
        try {
            write(bytes, false);
        } catch (...) {
            gzclose(out_);
            out_ = nullptr;
            throw;
        }
    }
    ~Writer() {
        if (out_)
            gzclose(out_);
    }
    Writer(const Writer &) = delete;
    Writer &operator=(const Writer &) = delete;
    void baseline(const Baseline &b) {
        check(!baseline_ && !finished_, "duplicate baseline");
        write(encode(b), true);
        baseline_ = true;
    }
    void member(const Member &m, const std::vector<Operation> &ops) {
        check(baseline_ && !finished_ && m.count == ops.size(), "member order/count");
        validate_operations(ops, 256);
        write(encode(m), true);
        for (auto &op : ops)
            write(encode(op), true);
    }
    void diagnostic(const Diagnostic &d, const std::vector<Operation> &ops) {
        check(baseline_ && !finished_ && d.count == ops.size(), "diagnostic order/count");
        validate_operations(ops, 256);
        write(encode(d), true);
        for (auto &op : ops)
            write(encode(op), true);
    }
    void finish(Footer f) {
        check(baseline_ && !finished_, "footer order");
        f.body_hash = body_;
        write(encode(f), false);
        finished_ = true;
        auto output = out_;
        out_ = nullptr;
        check(gzclose(output) == Z_OK, "close failed");
    }
};
} // namespace mmuctl
} // namespace fastsim::fst

#endif // FASTSIM_FST_MMU_CONTROLS_V1_CODEC
