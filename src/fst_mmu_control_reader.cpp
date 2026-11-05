#include "fastsim/fst_mmu_control_reader.hpp"
#include <algorithm>
#include <fstream>
#include <optional>
namespace fastsim::fst {
using namespace mmuctl;
namespace {
// Exactly one gzip member and no trailing physical or decoded bytes.
class Input {
    std::ifstream file_;
    z_stream z_{};
    std::array<unsigned char, 65536> in_{}, out_{};
    std::size_t pos_ = 0, end_ = 0;
    bool done_ = false;
    bool refill() {
        if (pos_ < end_)
            return true;
        if (done_)
            return false;
        pos_ = end_ = 0;
        while (!end_ && !done_) {
            if (!z_.avail_in) {
                file_.read(reinterpret_cast<char *>(in_.data()), in_.size());
                check(file_.gcount() > 0, "truncated gzip");
                z_.next_in = in_.data();
                z_.avail_in = static_cast<uInt>(file_.gcount());
            }
            z_.next_out = out_.data();
            z_.avail_out = static_cast<uInt>(out_.size());
            int status = inflate(&z_, Z_NO_FLUSH);
            end_ = out_.size() - z_.avail_out;
            if (status == Z_STREAM_END) {
                done_ = true;
                check(!z_.avail_in && file_.peek() == std::char_traits<char>::eof(),
                      "physical trailing bytes/member");
            } else
                check(status == Z_OK, "gzip integrity");
        }
        return end_ != 0;
    }

  public:
    explicit Input(const std::string &path) : file_(path, std::ios::binary) {
        check(bool(file_), "missing companion");
        check(inflateInit2(&z_, 31) == Z_OK, "inflate init");
    }
    ~Input() { inflateEnd(&z_); }
    Bytes read(unsigned n) {
        Bytes b(n);
        unsigned at = 0;
        while (at < n) {
            check(refill(), "truncated stream/footer");
            auto take = std::min<std::size_t>(n - at, end_ - pos_);
            std::memcpy(b.data() + at, out_.data() + pos_, take);
            at += static_cast<unsigned>(take);
            pos_ += take;
        }
        return b;
    }
    Bytes frame() {
        auto b = read(8);
        Cursor c{b};
        auto kind = c.get(2);
        check(c.get(2) == 0, "frame flags");
        auto size = c.get(4);
        constexpr unsigned sizes[] = {0, 80, 48, 96, 48, 48, 256};
        check(kind >= 1 && kind <= 6 && size == sizes[kind], "frame kind/size");
        auto rest = read(static_cast<unsigned>(size - 8));
        b.insert(b.end(), rest.begin(), rest.end());
        return b;
    }
    void eof() { check(!refill(), "trailing decoded bytes"); }
};
Frame kind(const Bytes &b) { return Frame(b[0] | unsigned(b[1]) << 8); }
void hash_frame(std::uint64_t &hash, const Bytes &b) {
    hash = hash_bytes(hash, b.data(), b.size());
}
std::vector<Operation> operations(Input &in, unsigned n, const Header &header,
                                  std::uint64_t &hash) {
    check(n <= header.max_ops, "group operation cap");
    std::vector<Operation> ops;
    ops.reserve(n);
    while (n--) {
        auto b = in.frame();
        hash_frame(hash, b);
        if (kind(b) == Frame::Write)
            ops.emplace_back(decode_write(b));
        else if (kind(b) == Frame::Command)
            ops.emplace_back(decode_command(b));
        else
            bad("expected operation");
    }
    validate_operations(ops, header.max_ops);
    return ops;
}
void count_ops(const std::vector<Operation> &ops, std::uint64_t &writes, std::uint64_t &commands) {
    for (auto &op : ops) {
        auto &value = std::holds_alternative<Write>(op) ? writes : commands;
        check(value < UINT64_MAX, "operation count overflow");
        ++value;
    }
}
void conserved(std::uint64_t total, std::uint64_t retained, std::uint64_t unbound) {
    check(retained <= total && unbound == total - retained, "operation conservation");
}
} // namespace
struct MmuControlReader::Impl {
    Header header;
    Baseline baseline;
    Footer footer;
    std::unique_ptr<Input> replay;
    std::optional<Group> current;
    std::uint64_t next = 0, replay_hash = body_seed();
};
MmuControlReader::~MmuControlReader() = default;
MmuControlReader::MmuControlReader(const std::string &path, std::uint32_t context,
                                   std::uint64_t records, const std::string &policy)
    : impl_(std::make_unique<Impl>()) {
    auto &p = *impl_;
    Input in(path + ".mmuctl.gz");
    p.header = decode_header(in.read(160));
    check(p.header.context == context && hex(p.header.policy) == policy, "context/policy binding");
    auto bb = in.frame();
    p.baseline = decode_baseline(bb);
    std::uint64_t body = body_seed();
    hash_frame(body, bb);
    std::ifstream hot(path, std::ios::binary);
    check(bool(hot), "missing hot stream");
    hot.seekg(72);
    std::uint64_t count = 0, sealed = 0, rw = 0, rc = 0, dw = 0, dc = 0, groups = 0, details = 0,
                  hot_hash = HashSeed;
    bool unsupported = false;
    State logical_state = p.baseline.state;
    std::array<std::uint64_t, 16> diagnostic_reasons{};
    for (;;) {
        auto b = in.frame();
        auto type = kind(b);
        if (type == Frame::Footer) {
            p.footer = decode_footer(b);
            in.eof();
            break;
        }
        hash_frame(body, b);
        if (type == Frame::Member) {
            auto m = decode_member(b);
            check(m.ordinal == count && count < records && m.context == context,
                  "member ordinal/context");
            check(m.generation == p.baseline.generation || m.status & Unsupported,
                  "member lifecycle");
            auto ops = operations(in, m.count, p.header, body);
            std::array<unsigned char, 64> row{};
            hot.read(reinterpret_cast<char *>(row.data()), 64);
            check(bool(hot) && m.hash == row_hash(row.data()), "hot member hash");
            if (!count)
                check(p.baseline.hash == m.hash && !(p.baseline.cut & 2),
                      "first retained baseline binding");
            hot_hash = hash_bytes(hot_hash, row.data(), 64);
            ++count;
            sealed += bool(m.status & Sealed);
            unsupported |= bool(m.status & Unsupported) || !(m.status & Sealed);
            check((m.status & Sealed) || ops.empty(), "unsealed retained effects");
            count_ops(ops, rw, rc);
            for (auto &op : ops)
                if (auto w = std::get_if<Write>(&op)) {
                    unsupported |= w->before != logical_state;
                    logical_state = w->after;
                    unsupported |= w->before_known != 15 || w->after_known != 15 ||
                                   w->parent != NoParent || w->application == Application::External;
                } else
                    unsupported |= std::get<Command>(op).domain != Domain::ContextAllTranslations;
        } else if (type == Frame::Diagnostic) {
            auto d = decode_diagnostic(b);
            check(d.context == context, "diagnostic context");
            auto ops = operations(in, d.count, p.header, body);
            details += d.count;
            check(details <= p.header.max_diagnostics, "diagnostic detail cap");
            count_ops(ops, dw, dc);
            ++groups;
            ++diagnostic_reasons[unsigned(d.reason)];
        } else
            bad("unexpected body frame");
    }
    auto &f = p.footer;
    check(f.hot == records && count == records && f.sealed == sealed && f.retained_writes == rw &&
              f.retained_commands == rc && f.groups == groups && f.hot_hash == hot_hash &&
              f.body_hash == body,
          "footer population/hash");
    conserved(f.total_writes, f.retained_writes, f.unbound_writes);
    conserved(f.total_commands, f.retained_commands, f.unbound_commands);
    check(dw <= f.unbound_writes && dc <= f.unbound_commands &&
              f.suppressed == (f.unbound_writes - dw) + (f.unbound_commands - dc),
          "diagnostic conservation");
    check(f.pending <= f.hot - f.sealed, "pending owner count");
    for (unsigned i = 0; i < 16; ++i)
        check(f.reasons[i] >= diagnostic_reasons[i], "diagnostic reason count");
    check(bool(f.status & Empty) == !records && bool(p.baseline.cut & 2) == !records,
          "empty status");
    if (f.status & Complete) {
        check(records && p.header.coverage == RequiredCoverage && p.baseline.known == 15 &&
                  p.baseline.cut == 1 && p.baseline.generation && f.sealed == records &&
                  !f.pending && !f.unbound_writes && !f.unbound_commands && !f.suppressed &&
                  !unsupported,
              "false completeness");
        for (auto r : f.reasons)
            check(!r, "complete with failures");
    } else if (records) {
        bool reason = false;
        for (auto r : f.reasons)
            reason |= bool(r);
        check(reason || p.header.coverage != RequiredCoverage || p.baseline.known != 15,
              "unexplained incomplete stream");
    }
    p.replay = std::make_unique<Input>(path + ".mmuctl.gz");
    check(encode(decode_header(p.replay->read(160))) == encode(p.header),
          "header changed on reopen");
    auto b = p.replay->frame();
    check(b == encode(p.baseline), "baseline changed on reopen");
    hash_frame(p.replay_hash, b);
}
void MmuControlReader::observe(std::uint64_t ordinal, const void *hot) {
    auto &p = *impl_;
    clear_current();
    check(ordinal == p.next && ordinal < p.footer.hot, "replay ordinal");
    for (;;) {
        auto b = p.replay->frame();
        hash_frame(p.replay_hash, b);
        if (kind(b) == Frame::Diagnostic) {
            auto d = decode_diagnostic(b);
            operations(*p.replay, d.count, p.header, p.replay_hash);
            continue;
        }
        auto m = decode_member(b);
        check(m.ordinal == ordinal && m.hash == row_hash(hot) && m.context == p.header.context,
              "replay binding");
        auto ops = operations(*p.replay, m.count, p.header, p.replay_hash);
        p.current = Group{m, std::move(ops)};
        ++p.next;
        break;
    }
    if (p.next == p.footer.hot) {
        for (;;) {
            auto b = p.replay->frame();
            if (kind(b) == Frame::Footer) {
                check(b == encode(p.footer) && p.replay_hash == p.footer.body_hash,
                      "replay footer/source changed");
                p.replay->eof();
                break;
            }
            hash_frame(p.replay_hash, b);
            auto d = decode_diagnostic(b);
            operations(*p.replay, d.count, p.header, p.replay_hash);
        }
    }
}
void MmuControlReader::clear_current() { impl_->current.reset(); }
const Group *MmuControlReader::current() const {
    return impl_->current ? &*impl_->current : nullptr;
}
const Header &MmuControlReader::header() const { return impl_->header; }
const Baseline &MmuControlReader::baseline() const { return impl_->baseline; }
const Footer &MmuControlReader::coverage() const { return impl_->footer; }
bool MmuControlReader::ready() const { return bool(impl_->footer.status & Complete); }
std::string MmuControlReader::policy_identity() const { return hex(impl_->header.policy); }
} // namespace fastsim::fst
