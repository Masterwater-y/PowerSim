#pragma once
#include "fastsim/fst_translation_facts.hpp"
#include "fastsim/types.hpp"
#include <functional>
#include <map>
#include <string>
namespace fastsim::fst {
// Compressed companion decoding is incremental; only dictionary and sparse
// bindings are retained. Construction verifies the entire hot stream, including
// records outside a replay slice, before exposing any functional facts.
class DynamicTranslations {
public:
  DynamicTranslations(const std::string &path, std::uint32_t core,
                      std::uint64_t records,
                      const std::function<std::uint64_t(std::uint64_t)> &asid);
  std::vector<TranslationFragment> resolve(std::uint64_t ordinal,
                                           const TraceRecord &record);

private:
  struct Binding {
    std::uint64_t ordinal, va, mid;
    std::uint32_t hash, token, size;
  };
  struct State {
    std::uint64_t mid, va, pa;
  };
  std::vector<TranslationMapping> definitions_;
  std::vector<Binding> bindings_;
  std::map<std::uint32_t, State> states_;
  std::size_t cursor_ = 0;
};
} // namespace fastsim::fst
