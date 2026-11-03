#include "fastsim/fst_dynamic_translations.hpp"
#include <array>
#include <cstring>
#include <fstream>
#include <limits>
#include <optional>
#include <stdexcept>
#include <tuple>
#include <zlib.h>
namespace fastsim::fst {
namespace {
[[noreturn]] void bad(const char *reason) {
  throw std::runtime_error(std::string("dynamic translation companion: ") +
                           reason);
}
constexpr auto max64 = std::numeric_limits<std::uint64_t>::max();
std::uint64_t add(std::uint64_t a, std::uint64_t b) {
  if (b > max64 - a)
    bad("integer overflow");
  return a + b;
}
// Compute base+(value-anchor) without unsigned wrap or a signed narrowing.
std::uint64_t anchored(std::uint64_t base, std::uint64_t value,
                       std::uint64_t anchor) {
  if (value >= anchor)
    return add(base, value - anchor);
  if (anchor - value > base)
    bad("negative anchored physical/virtual address");
  return base - (anchor - value);
}
std::uint64_t hash(const void *ptr, std::size_t size, std::uint64_t h,
                   bool wide) {
  for (std::size_t i = 0; i < size; i++) {
    h = (h ^ static_cast<const unsigned char *>(ptr)[i]) *
        (wide ? 1099511628211ull : 16777619u);
    if (!wide)
      h &= 0xffffffff;
  }
  return h;
}
// Unlike gzread, reject concatenated gzip members and trailing compressed junk.
class Reader {
  std::ifstream input_;
  z_stream z_{};
  std::array<unsigned char, 65536> input_buffer_{}, output_{};
  std::size_t cursor_ = 0, end_ = 0;
  bool done_ = false;

public:
  explicit Reader(const std::string &p) : input_(p, std::ios::binary) {
    if (!input_)
      bad("missing .xlat.gz");
    if (inflateInit2(&z_, 31) != Z_OK)
      bad("gzip initialization failed");
  }
  ~Reader() { inflateEnd(&z_); }
  bool refill() {
    if (cursor_ < end_)
      return true;
    if (done_)
      return false;
    cursor_ = end_ = 0;
    while (!end_ && !done_) {
      if (!z_.avail_in) {
        input_.read(reinterpret_cast<char *>(input_buffer_.data()),
                    input_buffer_.size());
        auto count = input_.gcount();
        if (!count)
          bad("truncated gzip/CRC/footer");
        z_.next_in = input_buffer_.data();
        z_.avail_in = static_cast<uInt>(count);
      }
      z_.next_out = output_.data();
      z_.avail_out = static_cast<uInt>(output_.size());
      int status = inflate(&z_, Z_NO_FLUSH);
      end_ = output_.size() - z_.avail_out;
      if (status == Z_STREAM_END) {
        done_ = true;
        if (z_.avail_in || input_.peek() != std::char_traits<char>::eof())
          bad("trailing gzip member/bytes");
      } else if (status != Z_OK)
        bad("invalid gzip/CRC");
    }
    return end_ != 0;
  }
  unsigned char byte() {
    if (!refill())
      bad("truncated field/footer");
    return output_[cursor_++];
  }
  std::uint64_t le(unsigned n) {
    std::uint64_t v = 0;
    for (unsigned i = 0; i < n; i++)
      v |= std::uint64_t(byte()) << (8 * i);
    return v;
  }
  std::uint64_t uv() {
    std::uint64_t v = 0;
    for (unsigned i = 0; i < 10; i++) {
      auto b = byte();
      if (i == 9 && b > 1)
        bad("ULEB128 overflow");
      v |= std::uint64_t(b & 127) << (i * 7);
      if (!(b & 128))
        return v;
    }
    bad("ULEB128 overflow");
  }
  void eof() {
    if (refill())
      bad("trailing decompressed bytes");
  }
};
bool complete(const TranslationMapping &m) {
  return (m.flags & 3) == 3 ||
         (m.kind != TranslationKind::kPaged && m.flags == 0);
}
std::uint64_t physical(const TranslationMapping &m, std::uint64_t va,
                       std::uint32_t size) {
  if (!(m.flags & 1) && m.kind == TranslationKind::kPaged)
    bad("binding has no known leaf");
  auto limit = std::uint64_t(1) << (m.page_bits ? m.page_bits : 12);
  if (va < m.virtual_base || va - m.virtual_base >= limit ||
      size > limit - (va - m.virtual_base))
    bad("fragment exceeds leaf");
  auto pa = m.kind == TranslationKind::kPaged
                ? add(m.physical_base, va - m.virtual_base)
                : anchored(m.request_physical_base, va, m.request_virtual_base);
  if (size - 1 > max64 - pa)
    bad("physical range overflow");
  return pa;
}
} // namespace
DynamicTranslations::DynamicTranslations(
    const std::string &path, std::uint32_t core, std::uint64_t records,
    const std::function<std::uint64_t(std::uint64_t)> &asid) {
  Reader r(path + ".xlat.gz");
  const char magic[8] = {'F', 'S', 'T', 'X', 'L', 'T', '1', 0};
  for (char c : magic)
    if (r.byte() != static_cast<unsigned char>(c))
      bad("invalid magic");
  auto version = r.le(4);
  if ((version != 1 && version != 2) || r.le(4) != 32 || r.le(4) != core ||
      r.le(4) != 1 || r.le(8))
    bad("invalid header/version/core/flags");
  std::uint64_t previous = 0, memories = 0, unknown = 0, expected_hash = 0;
  std::map<std::uint32_t,
           std::tuple<std::uint64_t, std::uint64_t, std::uint64_t>>
      identities;
  while (true) {
    auto tag = r.byte();
    if (tag == 1) {
      if (r.uv() != definitions_.size() + 1)
        bad("nonsequential dictionary ID");
      TranslationMapping m;
      m.address_space_id = r.uv();
      m.walk_root = r.uv();
      m.virtual_base = r.uv();
      m.physical_base = r.uv();
      auto pcid = r.uv();
      m.page_bits = r.byte();
      m.levels = r.byte();
      m.flags = r.byte();
      if (pcid > 4095 || m.levels > 8 || (m.flags & ~7))
        bad("invalid PCID/levels/flags");
      m.pcid = static_cast<std::uint16_t>(pcid);
      if (m.flags & 1) {
        if (m.page_bits != 12 && m.page_bits != 21 && m.page_bits != 22 &&
            m.page_bits != 30)
          bad("invalid leaf size");
        auto mask = (std::uint64_t(1) << m.page_bits) - 1;
        if ((m.virtual_base | m.physical_base) & mask)
          bad("unaligned leaf");
      } else if (m.page_bits || (m.flags & 2))
        bad("unknown leaf with path/size");
      if (m.flags & 2) {
        if (!m.levels || !m.walk_root)
          bad("missing path root/levels");
      } else if (m.levels || m.walk_root)
        bad("unknown path has root/levels");
      for (unsigned i = 0; i < m.levels; i++) {
        m.pte_addresses[i] = r.uv();
        if (m.pte_addresses[i] & 3)
          bad("unaligned PTE");
      }
      if (version == 2) {
        auto kind = r.byte();
        if (kind > 6)
          bad("unknown translation kind");
        m.kind = static_cast<TranslationKind>(kind);
        m.request_physical_base = r.uv();
        m.request_virtual_base = r.uv();
        if (!kind && (m.request_physical_base || m.request_virtual_base))
          bad("paged RAM with special anchors");
        if (kind && !(m.flags & 1) &&
            (m.flags || m.page_bits || m.levels || m.walk_root ||
             m.physical_base || (m.virtual_base & 4095)))
          bad("invalid nonpaged special mapping");
      }
      definitions_.push_back(m);
    } else if (tag == 2) {
      Binding b{};
      b.ordinal = add(previous, r.uv());
      previous = b.ordinal;
      b.hash = static_cast<std::uint32_t>(r.le(4));
      auto token = r.uv();
      b.va = r.uv();
      auto size = r.uv();
      b.mid = r.uv();
      if (token > 0x7fffffff || !size || size > 65535 ||
          b.mid > definitions_.size() || b.ordinal >= records ||
          size - 1 > max64 - b.va)
        bad("invalid binding");
      b.token = static_cast<std::uint32_t>(token);
      b.size = static_cast<std::uint32_t>(size);
      if (b.mid) {
        auto &m = definitions_[b.mid - 1];
        auto pa = physical(m, b.va, b.size);
        if (token) {
          auto identity =
              std::make_tuple(m.address_space_id, b.va >> 12, pa >> 12);
          auto found = identities.emplace(b.token, identity);
          if (!found.second && found.first->second != identity)
            bad("token identity changed ASID/VA/PA");
        }
      }
      bindings_.push_back(b);
    } else if (tag == 255) {
      if (r.uv() != records)
        bad("record population mismatch");
      memories = r.uv();
      if (r.uv() != definitions_.size() || r.uv() != bindings_.size())
        bad("dictionary/binding population mismatch");
      unknown = r.uv();
      expected_hash = r.le(8);
      if (unknown > memories || memories > records)
        bad("invalid footer population");
      r.eof();
      break;
    } else
      bad("invalid tag/missing footer");
  }
  std::ifstream hot(path, std::ios::binary);
  hot.seekg(72);
  std::uint64_t h = 14695981039346656037ull, actual_mem = 0, actual_unknown = 0;
  TraceRecord rec;
  for (std::uint64_t i = 0; i < records; i++) {
    if (!hot.read(reinterpret_cast<char *>(&rec), sizeof(rec)))
      bad("truncated hot stream");
    h = hash(&rec, sizeof(rec), h, true);
    auto facts = resolve(i, rec);
    if (rec.is_memory() && rec.size) {
      actual_mem++;
      bool known = true;
      auto expected_asid = asid(i);
      std::optional<std::uint64_t> row_asid;
      for (auto &f : facts) {
        known &= complete(f.mapping);
        if (f.mapping.address_space_id || f.mapping.flags ||
            f.mapping.kind != TranslationKind::kPaged) {
          if (expected_asid && f.mapping.address_space_id != expected_asid)
            bad("mapping disagrees with source ASID");
          if (row_asid && *row_asid != f.mapping.address_space_id)
            bad("mixed ASID in memory record");
          row_asid = f.mapping.address_space_id;
        }
      }
      actual_unknown += !known;
    }
  }
  if (cursor_ != bindings_.size() || h != expected_hash ||
      actual_mem != memories || actual_unknown != unknown)
    bad("full hot hash/coverage/population mismatch");
  cursor_ = 0;
  states_.clear();
}
std::vector<TranslationFragment>
DynamicTranslations::resolve(std::uint64_t ordinal, const TraceRecord &record) {
  std::vector<TranslationFragment> result;
  bool memory = record.is_memory() && record.size;
  auto token = has_flag(record.flags, kVirtualPageToken)
                   ? record.virtual_page_token()
                   : 0u;
  if (cursor_ < bindings_.size() && bindings_[cursor_].ordinal < ordinal)
    bad("skipped binding ordinal");
  auto begin = cursor_;
  std::uint64_t covered = 0;
  while (cursor_ < bindings_.size() && bindings_[cursor_].ordinal == ordinal) {
    auto &b = bindings_[cursor_++];
    if (!memory ||
        b.hash != hash(&record, sizeof(record), 2166136261u, false) ||
        b.token != token)
      bad("binding hot row/hash/token mismatch");
    if (!result.empty() &&
        b.va != add(result.back().virtual_address, result.back().size))
      bad("noncontiguous fragments");
    TranslationFragment f;
    f.virtual_address = b.va;
    f.size = b.size;
    if (b.mid) {
      f.mapping = definitions_[b.mid - 1];
      f.physical_address = physical(f.mapping, b.va, b.size);
      if (result.empty() && f.physical_address != record.address)
        bad("binding PA disagrees with hot row");
    } else
      f.physical_address = result.empty() ? record.address : 0;
    covered = add(covered, b.size);
    result.push_back(f);
  }
  if (cursor_ != begin) {
    if (covered != record.size)
      bad("incomplete fragment coverage");
    if (token) {
      if (result.size() == 1)
        states_[token] = {bindings_[begin].mid, result[0].virtual_address,
                          record.address};
      else
        states_.erase(token);
    }
  } else if (memory) {
    auto found = states_.find(token);
    if (!token || found == states_.end())
      bad("missing explicit/sparse binding");
    auto s = found->second;
    TranslationFragment f;
    f.size = record.size;
    if (s.mid) {
      if ((record.address >> 12) != (s.pa >> 12) ||
          record.size > 4096 - (record.address & 4095))
        bad("sparse token outside physical page");
      f.virtual_address = anchored(s.va, record.address, s.pa);
      f.mapping = definitions_[s.mid - 1];
      f.physical_address = physical(f.mapping, f.virtual_address, f.size);
      if (f.physical_address != record.address)
        bad("inherited physical address mismatch");
    } else {
      f.virtual_address = s.va;
      f.physical_address = record.address;
    }
    result.push_back(f);
  }
  return result;
}
} // namespace fastsim::fst
