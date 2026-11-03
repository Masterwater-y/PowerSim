#include "fastsim/fst_dynamic_translations.hpp"
#include "fastsim/trace.hpp"
#undef NDEBUG
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <zlib.h>
using namespace fastsim;
using Bytes = std::vector<unsigned char>;
static void le(Bytes &b, uint64_t v, int n) {
  for (int i = 0; i < n; i++)
    b.push_back(static_cast<unsigned char>(v >> (8 * i)));
}
static void uv(Bytes &b, uint64_t v) {
  do {
    auto x = static_cast<unsigned char>(v & 127);
    v >>= 7;
    b.push_back(x | (v ? 128 : 0));
  } while (v);
}
static uint64_t hash(const void *ptr, size_t size, bool wide) {
  uint64_t h = wide ? 14695981039346656037ull : 2166136261u;
  for (size_t i = 0; i < size; i++) {
    h = (h ^ static_cast<const unsigned char *>(ptr)[i]) *
        (wide ? 1099511628211ull : 16777619u);
    if (!wide)
      h &= 0xffffffff;
  }
  return h;
}
struct Fixture {
  std::string path;
  std::vector<TraceRecord> records;
  Bytes body;
  uint64_t defs = 0, binds = 0, previous = 0;
  unsigned version = 1;
  explicit Fixture(std::string name)
      : path("/tmp/fastsim-xlat-reader-" + name + ".fst") {}
  void mapping(unsigned bits = 21, unsigned kind = 0, uint64_t pa = 0,
               uint64_t va = 0, uint64_t virtual_base = 0x200000,
               uint64_t physical_base = 0x400000) {
    body.push_back(1);
    for (auto x : {++defs, uint64_t(1), bits ? uint64_t(0x1000) : 0,
                   virtual_base, bits ? physical_base : 0, uint64_t(0)})
      uv(body, x);
    body.push_back(static_cast<unsigned char>(bits));
    body.push_back(bits ? 3 : 0);
    body.push_back(bits ? 3 : 0);
    if (bits)
      for (auto p : {0x1000, 0x2000, 0x3000})
        uv(body, p);
    if (version == 2) {
      body.push_back(static_cast<unsigned char>(kind));
      uv(body, pa);
      uv(body, va);
    }
  }
  void row(size_t ordinal, unsigned mid, uint64_t va = 0x200020,
           unsigned size = 8, unsigned token = 7) {
    body.push_back(2);
    uv(body, ordinal - previous);
    previous = ordinal;
    le(body, hash(&records[ordinal], 64, false), 4);
    for (auto x : {uint64_t(token), va, uint64_t(size), uint64_t(mid)})
      uv(body, x);
    binds++;
  }
  void save(uint64_t unknown = 0, bool trailing = false) {
    Bytes header;
    for (auto c : std::string("FSTRC01", 7))
      header.push_back(c);
    header.push_back(0);
    for (auto x : {7u, 72u, 64u, 0u})
      le(header, x, 4);
    le(header, records.size(), 8);
    le(header, 512, 8);
    for (int i = 0; i < 4; i++)
      le(header, 0, 8);
    std::ofstream out(path, std::ios::binary);
    out.write((char *)header.data(), header.size());
    out.write((char *)records.data(), records.size() * 64);
    out.close();
    Bytes side;
    for (auto c : std::string("FSTXLT1", 7))
      side.push_back(c);
    side.push_back(0);
    for (auto x : {version, 32u, 0u, 1u})
      le(side, x, 4);
    le(side, 0, 8);
    side.insert(side.end(), body.begin(), body.end());
    side.push_back(255);
    for (auto x : {uint64_t(records.size()), uint64_t(records.size()), defs,
                   binds, unknown})
      uv(side, x);
    le(side, hash(records.data(), records.size() * 64, true), 8);
    if (trailing)
      side.push_back(0);
    auto g = gzopen((path + ".xlat.gz").c_str(), "wb");
    assert(g);
    assert(gzwrite(g, side.data(), static_cast<unsigned>(side.size())) ==
           int(side.size()));
    assert(gzclose(g) == Z_OK);
  }
  ~Fixture() {
    std::filesystem::remove(path);
    std::filesystem::remove(path + ".xlat.gz");
  }
};
static TraceRecord memory(uint64_t pa = 0x400020, unsigned token = 7,
                          unsigned size = 8) {
  TraceRecord r;
  r.address = pa;
  r.size = static_cast<uint16_t>(size);
  r.flags = kRetires | kLoad | kVirtualPageToken;
  r.reserved = token;
  return r;
}
template <class F> static void rejected(F f) {
  bool bad = false;
  try {
    f();
  } catch (const std::exception &) {
    bad = true;
  }
  assert(bad);
}
int main() {
  {
    Fixture f("split");
    f.records = {memory(), memory(0x400028), memory(), memory(0x400028)};
    f.mapping();
    f.row(0, 1);
    f.mapping(12);
    f.row(2, 2);
    f.save();
    BinaryTraceSource source(f.path);
    assert(source.dynamic_translations());
    TraceRecord r;
    for (int i = 0; i < 4; i++) {
      assert(source.next(r));
      auto facts = source.current_translation_fragments();
      assert(facts.size() == 1);
      assert(facts[0].mapping.page_bits == (i < 2 ? 21 : 12));
      assert(facts[0].virtual_address ==
             0x200020ull + static_cast<unsigned>(i % 2) * 8);
    }
    assert(!source.next(r));
  }
  {
    Fixture f("slice");
    f.records = {memory(), memory(0x400028), memory(0x400030)};
    f.mapping();
    f.row(0, 1);
    f.save();
    InstructionSliceTraceSource s(std::make_unique<BinaryTraceSource>(f.path),
                                  1, 1);
    TraceRecord r;
    assert(s.dynamic_translations());
    assert(s.next(r));
    assert(*s.current_source_record_ordinal() == 1);
    assert(s.current_translation_fragments()[0].virtual_address == 0x200028);
  }
  {
    Fixture f("zero");
    f.records = {memory(0x400020, 0)};
    f.mapping();
    f.row(0, 1, 0x200020, 8, 0);
    f.save();
    BinaryTraceSource s(f.path);
    TraceRecord r;
    assert(s.next(r));
    assert(s.current_translation_fragments().size() == 1);
    std::filesystem::remove(f.path + ".xlat.gz");
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("unknown");
    f.records = {memory()};
    f.row(0, 0);
    f.save(1);
    BinaryTraceSource s(f.path);
    TraceRecord r;
    assert(s.next(r));
    assert(s.current_translation_fragments()[0].mapping.flags == 0);
  }
  {
    Fixture f("special");
    f.version = 2;
    f.records = {memory(0xa00028), memory(0xa00020)};
    f.mapping(0, 4, 0xa00028, 0x200028);
    f.row(0, 1, 0x200028);
    f.save();
    BinaryTraceSource s(f.path);
    TraceRecord r;
    assert(s.next(r));
    assert(s.next(r));
    assert(s.current_translation_fragments()[0].virtual_address == 0x200020);
  }
  {
    Fixture f("hash");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::fstream hot(f.path, std::ios::binary | std::ios::in | std::ios::out);
    hot.seekp(72);
    hot.put(1);
    hot.close();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("crc");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::fstream gz(f.path + ".xlat.gz",
                    std::ios::binary | std::ios::in | std::ios::out);
    gz.seekp(-8, std::ios::end);
    gz.put(0);
    gz.close();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }

  {
    Fixture f("multi");
    f.records = {memory(), memory(0x400028, 8), memory(0x400030),
                 memory(0x400038, 8)};
    f.mapping();
    f.row(0, 1);
    f.row(1, 1, 0x200028, 8, 8);
    f.save();
    BinaryTraceSource s(f.path);
    TraceRecord r;
    for (unsigned i = 0; i < 4; i++) {
      assert(s.next(r));
      assert(s.current_translation_fragments()[0].virtual_address ==
             0x200020 + i * 8);
    }
  }
  {
    Fixture f("fragments");
    f.records = {memory(), memory(), memory(), memory(0x400028)};
    f.mapping();
    f.row(0, 1);
    f.row(1, 1, 0x200020, 4);
    f.row(1, 1, 0x200024, 4);
    f.row(2, 1);
    f.save();
    BinaryTraceSource s(f.path);
    TraceRecord r;
    for (unsigned i = 0; i < 4; i++) {
      assert(s.next(r));
      assert(s.current_translation_fragments().size() == (i == 1 ? 2 : 1));
    }
  }
  {
    Fixture f("split-missing-return");
    f.records = {memory(), memory(), memory()};
    f.mapping();
    f.row(0, 1);
    f.row(1, 1, 0x200020, 4);
    f.row(1, 1, 0x200024, 4);
    f.save();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("cross-page");
    f.records = {memory(0x400ffc, 0, 8)};
    f.mapping(12);
    f.mapping(12, 0, 0, 0, 0x201000, 0x800000);
    f.row(0, 1, 0x200ffc, 4, 0);
    f.row(0, 2, 0x201000, 4, 0);
    f.save();
    BinaryTraceSource s(f.path);
    TraceRecord r;
    assert(s.next(r));
    assert(s.current_translation_fragments().size() == 2);
    assert(s.current_translation_fragments()[1].physical_address == 0x800000);
  }
  {
    Fixture f("warmup");
    f.records = {memory(), memory(0x400028), memory(0x400030)};
    f.mapping();
    f.row(0, 1);
    f.save();
    WarmupInstructionTraceSource s(std::make_unique<BinaryTraceSource>(f.path),
                                   1, 2);
    TraceRecord r;
    assert(s.dynamic_translations());
    assert(s.next(r));
    assert(!s.next(r));
    s.start_measurement();
    assert(s.next(r));
    assert(*s.current_source_record_ordinal() == 1);
    assert(s.current_translation_fragments()[0].virtual_address == 0x200028);
  }
  {
    Fixture f("trailing");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::ofstream out(f.path + ".xlat.gz", std::ios::app | std::ios::binary);
    out.put('x');
    out.close();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("token-identity");
    f.records = {memory(), memory(0x401020)};
    f.mapping(12);
    f.mapping(12, 0, 0, 0, 0x201000, 0x401000);
    f.row(0, 1);
    f.row(1, 2, 0x201020);
    f.save();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("footer-population");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save(1);
    rejected([&] { BinaryTraceSource bad(f.path); });
  }

  {
    Fixture f("full-hot");
    f.records = {memory(), memory(0x400028)};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::fstream hot(f.path, std::ios::binary | std::ios::in | std::ios::out);
    hot.seekp(72 + 64);
    hot.put(1);
    hot.close();
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("footer-eof");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save(0, true);
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("truncated");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::filesystem::resize_file(
        f.path + ".xlat.gz",
        std::filesystem::file_size(f.path + ".xlat.gz") - 4);
    rejected([&] { BinaryTraceSource bad(f.path); });
  }
  {
    Fixture f("asid");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    rejected([&] {
      fst::DynamicTranslations bad(f.path, 0, 1,
                                   [](uint64_t) { return uint64_t(2); });
    });
  }
  {
    Fixture f("legacy");
    f.records = {memory()};
    f.mapping();
    f.row(0, 1);
    f.save();
    std::fstream hot(f.path, std::ios::binary | std::ios::in | std::ios::out);
    hot.seekp(32);
    uint64_t flags = 0;
    hot.write(reinterpret_cast<const char *>(&flags), 8);
    hot.close();
    std::filesystem::remove(f.path + ".xlat.gz");
    BinaryTraceSource source(f.path);
    TraceRecord record;
    assert(!source.dynamic_translations());
    assert(source.next(record));
    assert(source.current_translation_fragments().empty());
  }
  std::cout << "dynamic translation reader tests passed\n";
}
