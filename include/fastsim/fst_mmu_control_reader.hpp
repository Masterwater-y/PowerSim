#pragma once
#include "fastsim/fst_mmu_controls.hpp"
#include <memory>
namespace fastsim::fst {
class MmuControlReader {
    struct Impl;
    std::unique_ptr<Impl> impl_;

  public:
    MmuControlReader(const std::string &hot_path, std::uint32_t context, std::uint64_t records,
                     const std::string &policy);
    ~MmuControlReader();
    void observe(std::uint64_t ordinal, const void *hot);
    void clear_current();
    const mmuctl::Group *current() const;
    const mmuctl::Header &header() const;
    const mmuctl::Baseline &baseline() const;
    const mmuctl::Footer &coverage() const;
    bool ready() const;
    std::string policy_identity() const;
};
} // namespace fastsim::fst
