#include "fastsim/fst_architectural_boundary_reader.hpp"
#include "fastsim/fst_mmu_control_v2_reader.hpp"
#include "fastsim/fst_retirement.hpp"
#include "fst_bundle_input.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
using namespace fastsim::fst;
void parser_size_bounds() {
  std::filesystem::create_directories("tmp/p4-architectural-bundle");
  char name[] = "tmp/p4-architectural-bundle/fix1-parser-XXXXXX";
  const char *directory = mkdtemp(name);
  assert(directory);
  const std::array<unsigned, 7> sizes{0, 96, 64, 96, 48, 0, 320};
  for (const auto &[kind, size] :
       std::vector<std::pair<unsigned, unsigned>>{{5, 0},
                                                  {5, 8},
                                                  {1, 0},
                                                  {1, 7},
                                                  {1, 8},
                                                  {1, UINT32_MAX},
                                                  {0, 0},
                                                  {7, 8}}) {
    auto bytes = archb::prefix(kind, size);
    const auto path = std::string(directory) + "/frame-" +
                      std::to_string(kind) + "-" + std::to_string(size) + ".gz";
    auto out = gzopen(path.c_str(), "wb1");
    assert(out);
    assert(gzwrite(out, bytes.data(), bytes.size()) == int(bytes.size()));
    assert(gzclose(out) == Z_OK);
    const auto child = fork();
    assert(child >= 0);
    if (!child) {
      const rlimit limit{64 * 1024 * 1024, 64 * 1024 * 1024};
      if (setrlimit(RLIMIT_AS, &limit))
        _exit(10);
      try {
        bundle_detail::BundleInput input(path);
        input.frame(sizes);
      } catch (const std::runtime_error &e) {
        _exit(std::string(e.what()).find("frame kind/size") != std::string::npos
                  ? 0
                  : 11);
      } catch (...) {
        _exit(12);
      }
      _exit(13);
    }
    int status = 0;
    assert(waitpid(child, &status, 0) == child);
    if (!WIFEXITED(status) || WEXITSTATUS(status))
      std::cerr << "bounded parser rejected incorrectly: kind=" << kind
                << " size=" << size << " status=" << status << '\n';
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
  }
  auto valid = archb::prefix(4, 48);
  valid.resize(48);
  const auto path = std::string(directory) + "/minimum-enabled-frame.gz";
  auto out = gzopen(path.c_str(), "wb1");
  assert(out);
  assert(gzwrite(out, valid.data(), valid.size()) == int(valid.size()));
  assert(gzclose(out) == Z_OK);
  bundle_detail::BundleInput input(path);
  assert(input.frame(sizes) == valid);
  input.eof();
}
int main() {
  parser_size_bounds();
  // Full-original admission: a valid sidecar alone cannot certify a bundle.
  bool rejected = false;
  try {
    ArchitecturalBoundaryReader input("tmp/p4-architectural-bundle/test", 0, 0,
                                      std::string(64, '0'));
  } catch (const std::runtime_error &) {
    rejected = true;
  }
  assert(rejected);
  const std::string path = "tmp/p4-architectural-bundle/reader_case";
  std::array<std::uint8_t, 32> policy{};
  policy[0] = 1;
  mmuctl::Bytes hot(72);
  const unsigned char magic[] = {'F', 'S', 'T', 'R', 'C', '0', '1', 0};
  std::copy(std::begin(magic), std::end(magic), hot.begin());
  hot[8] = 7;
  hot[12] = 72;
  hot[16] = 64;
  auto flags = archb::RequiredFeatures;
  for (unsigned i = 0; i < 8; ++i)
    hot[32 + i] = flags >> (8 * i);
  {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char *>(hot.data()), hot.size());
  }
  {
    std::ofstream f(path + ".fsem", std::ios::binary);
    RetirementHeader h;
    f.write(reinterpret_cast<const char *>(&h), sizeof(h));
  }
  CoreFunctionalHeader ch;
  ch.policy_sha256 = policy;
  ch.architectural_registers = {39, 48, 1, 2, 1, 0, 6, 4096};
  ch.version = 2;
  ch.header_size = 128;
  ch.magic[6] = '2';
  {
    CoreFunctionalWriter w(path + ".core.gz", ch);
    w.finish(0);
  }
  {
    RetiredFrontendWriter w(path + ".ffe.gz", 0);
    w.finish(0);
  }
  archb::Header ah;
  ah.policy = policy;
  ah.coverage = 255;
  {
    archb::Writer w(path + ".archb.gz", ah);
    archb::Epoch e;
    e.generation = 1;
    e.proof_flags = e.state_known = 15;
    w.epoch(e);
    archb::Footer f;
    f.status = 7;
    f.epoch_rows = 1;
    w.finish(f);
  }
  ArchitecturalBoundaryReader reader(path, 0, 0, mmuctl::hex(policy));
  assert(reader.ready());
  assert(!reader.current());
  reader.clear_current();
  mmuctl_v2::Header mh;
  mh.policy = policy;
  mh.coverage = 255;
  {
    mmuctl_v2::Writer w(path + ".mmuctl.gz", mh);
    mmuctl_v2::Baseline baseline;
    baseline.generation = 1;
    baseline.cut_kind = 1;
    baseline.known = baseline.proof_flags = 15;
    baseline.epoch_reference = 1;
    w.baseline(baseline);
    mmuctl_v2::Footer footer;
    footer.status = 7;
    w.finish(footer);
  }
  MmuControlV2Reader controls(path, 0, 0, mmuctl::hex(policy), reader);
  assert(controls.ready());
  assert(!controls.current());
  controls.clear_current();
  {
    std::ofstream f(path + ".archb.gz", std::ios::binary | std::ios::app);
    f.put('x');
  }
  rejected = false;
  try {
    ArchitecturalBoundaryReader bad(path, 0, 0, mmuctl::hex(policy));
  } catch (const std::runtime_error &) {
    rejected = true;
  }
  assert(rejected);
}
