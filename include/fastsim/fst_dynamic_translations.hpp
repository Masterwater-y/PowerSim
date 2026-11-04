#pragma once
#include "fastsim/fst_translation_facts.hpp"
#include "fastsim/types.hpp"
#include <functional>
#include <map>
#include <optional>
#include <string>
namespace fastsim::fst {
// Compressed companion decoding is incremental; only dictionary and sparse
// bindings are retained. Construction verifies the entire hot stream, including
// records outside a replay slice, before exposing any functional facts.
class DynamicTranslations {
public:
  DynamicTranslations(const std::string &path, std::uint32_t core,
                      std::uint64_t records,
                      const std::function<std::uint64_t(std::uint64_t)> &asid,
                      const std::string &policy = {},
                      const std::function<void(std::uint32_t,std::uint64_t,std::uint64_t,std::uint64_t,std::uint64_t)> &token_check = {});
  unsigned version() const { return version_; }
  std::optional<std::uint64_t> retirement_asid() const { return retirement_asid_; }
  std::optional<std::uint64_t> source_ordinal() const { return source_ordinal_; }
  std::vector<TranslationFragment> resolve(std::uint64_t ordinal,
                                           const TraceRecord &record);

private:
  struct Binding {
    std::uint64_t ordinal, va, mid, retirement_asid;
    std::uint32_t hash, token, size;
  };
  struct State {
    std::uint64_t mid, va, pa, retirement_asid;
  };
  unsigned version_=0;
  std::optional<std::uint64_t> retirement_asid_, source_ordinal_;
  std::function<std::uint64_t(std::uint64_t)> asid_;
  std::vector<TranslationMapping> definitions_;
  std::vector<Binding> bindings_;
  std::map<std::uint32_t, State> states_;
  std::size_t cursor_ = 0;
};
} // namespace fastsim::fst
