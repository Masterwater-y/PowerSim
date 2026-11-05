#pragma once
#include "fastsim/fst_architectural_boundaries.hpp"
#include "fastsim/fst_core_functional_facts.hpp"
#include <memory>
#include <optional>
namespace fastsim::fst {
struct ArchitecturalBoundaryEvent {
  archb::Accepted accepted;
  std::optional<archb::InvocationSeal> invocation;
  archb::Terminal terminal;
  std::optional<std::vector<mmuctl::Operation>> operations;
};
class ArchitecturalBoundaryReader {
  struct Impl;
  std::unique_ptr<Impl> impl_;

public:
  ArchitecturalBoundaryReader(const std::string &hot_path, std::uint32_t core,
                              std::uint64_t records, const std::string &policy);
  ~ArchitecturalBoundaryReader();
  void observe(std::uint64_t ordinal, const void *hot,
               const CoreFunctionalRow &core);
  void clear_current();
  const archb::EntryMember *current() const;
  const archb::Header &header() const;
  const archb::Epoch &epoch() const;
  const archb::Footer &coverage() const;
  bool ready() const;
  // Independent monotonic metadata cursor for the controls cross-file join.
  // Event rows are sparse and bounded; entry members are never buffered.
  ArchitecturalBoundaryEvent next_event(std::uint64_t event);
  ArchitecturalBoundaryEvent replay_control_event(std::uint64_t event);
  const std::vector<ArchitecturalBoundaryEvent> &
  boundaries_before(std::uint64_t ordinal) const;
};
} // namespace fastsim::fst
