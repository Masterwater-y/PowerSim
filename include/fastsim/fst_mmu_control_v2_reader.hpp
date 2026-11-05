#pragma once
#include "fastsim/fst_architectural_boundary_reader.hpp"
#include "fastsim/fst_mmu_controls_v2.hpp"
#include <memory>
namespace fastsim::fst {
class MmuControlV2Reader {
  struct Impl;
  std::unique_ptr<Impl> impl_;

public:
  MmuControlV2Reader(const std::string &, std::uint32_t, std::uint64_t,
                     const std::string &, ArchitecturalBoundaryReader &);
  ~MmuControlV2Reader();
  void observe(std::uint64_t ordinal, const void *hot);
  void clear_current();
  const mmuctl_v2::Group *current() const;
  const mmuctl_v2::Header &header() const;
  const mmuctl_v2::Baseline &baseline() const;
  const mmuctl_v2::Footer &coverage() const;
  bool ready() const;
  const std::vector<ArchitecturalBoundaryEvent> &initial_boundaries() const;
  const std::vector<ArchitecturalBoundaryEvent> &
  boundaries_before(std::uint64_t ordinal) const;
};
} // namespace fastsim::fst
