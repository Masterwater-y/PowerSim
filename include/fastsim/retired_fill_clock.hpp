#pragma once
#include <cstdint>
#include <limits>
namespace fastsim {
// Observer clocks only. Selected owner service and final child admission are
// distinct edges; zero is valid, UINT64_MAX means not recorded.
struct RetiredFillClock {
 static constexpr std::uint64_t unavailable=std::numeric_limits<std::uint64_t>::max();
 enum class Source : unsigned { Selected, Canonical, FinalAdmission };
 std::uint64_t reference_issue=0;Source source=Source::Canonical;
 static RetiredFillClock select(std::uint64_t canonical,std::uint64_t selected,
                               bool available,std::uint64_t final=unavailable){
  if(final!=unavailable)return {final,Source::FinalAdmission};
  return available?RetiredFillClock{selected,Source::Selected}:RetiredFillClock{canonical,Source::Canonical};
 }
};
}
