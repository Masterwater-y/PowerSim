#pragma once
#include "fastsim/fst_architectural_boundaries.hpp"
#include "fastsim/fst_core_functional_facts.hpp"

namespace fastsim::fst::bundle_detail {
// A partial terminal may describe an observed prefix of a later completed
// frontend macro. Its missing suffix stays unqualified; it is never invented.
inline bool terminal_matches_frontend(const archb::Terminal &t,
                                      const RetiredFrontendMacro &m) {
  return t.entry_begin == m.begin_record && t.entry_end > t.entry_begin &&
         (t.entry_end == m.end_record ||
          (t.status == 2 && t.entry_end < m.end_record)) &&
         t.ffe_semantic_hash == m.semantic_identity &&
         t.ffe_code_hash == m.code_identity;
}
}
