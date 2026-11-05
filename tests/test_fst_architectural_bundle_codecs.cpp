#include "fastsim/fst_architectural_boundaries.hpp"
#include "fastsim/fst_core_functional_facts.hpp"
#include "fastsim/fst_mmu_controls_v2.hpp"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
using namespace fastsim::fst;
template <class F> void rejects(F f) {
  bool caught = false;
  try {
    f();
  } catch (const std::runtime_error &) {
    caught = true;
  }
  assert(caught);
}
std::string file(const char *p) {
  std::ifstream f(p);
  return {std::istreambuf_iterator<char>(f), {}};
}
int main() {
  namespace a = archb;
  namespace m = mmuctl_v2;
  assert(a::layout_descriptor() ==
         file("docs/superpowers/specs/layouts/spr-archb-v1.layout"));
  assert(m::layout_descriptor() ==
         file("docs/superpowers/specs/layouts/spr-mmuctl-v2.layout"));
  a::Header ah;
  ah.policy[0] = 1;
  ah.coverage = 255;
  auto h = a::encode(ah);
  assert(h.size() == 160);
  assert(a::encode(a::decode_header(h)) == h);
  h[48] ^= 1;
  rejects([&] { a::decode_header(h); });
  h = a::encode(ah);
  h[159] = 1;
  rejects([&] { a::decode_header(h); });
  a::Epoch e;
  e.generation = 1;
  e.proof_flags = 15;
  e.state_known = 15;
  auto eb = a::encode(e);
  assert(eb.size() == 96);
  assert(a::encode(a::decode_epoch(eb)) == eb);
  a::Accepted acc;
  acc.event = 1;
  acc.generation = 1;
  acc.cause = a::Cause::ExternalInterrupt;
  acc.carrier = 2;
  acc.mode = 1;
  acc.predecessor_kind = 2;
  acc.known = 15;
  acc.epoch_generation = 1;
  acc.vector = 33;
  auto ab = a::encode(acc);
  assert(ab.size() == 192);
  assert(a::encode(a::decode_accepted(ab)) == ab);
  acc.fact_flags = 1;
  rejects([&] { a::encode(acc); });
  acc.fact_flags = 0;
  a::InvocationSeal s;
  s.event = 1;
  s.generation = 1;
  s.status = 1;
  s.known = 103;
  s.mode = 1;
  s.entry_kind = 1;
  s.vector_arg = 33;
  s.op_hash = m::operations_seed();
  auto sb = a::encode(s);
  assert(sb.size() == 128);
  assert(a::encode(a::decode_invocation_seal(sb)) == sb);
  a::EntryMember member;
  member.event = 1;
  member.generation = 1;
  member.rom_kind = 1;
  member.member_flags = 1;
  auto mb = a::encode(member);
  assert(mb.size() == 64);
  assert(a::encode(a::decode_entry_member(mb)) == mb);
  member.hot_hash = 1ull << 32;
  rejects([&] { a::encode(member); });
  member.hot_hash = 0;
  a::Terminal t;
  t.event = 1;
  t.generation = 1;
  t.status = 2;
  t.reason_mask = 1u << 9;
  auto tb = a::encode(t);
  assert(tb.size() == 160);
  assert(a::encode(a::decode_terminal(tb)) == tb);
  t.status = 1;
  rejects([&] { a::encode(t); });
  a::Footer af;
  auto fb = a::encode(af);
  assert(fb.size() == 320);
  assert(a::encode(a::decode_footer(fb)) == fb);
  m::Header mh;
  mh.policy[0] = 1;
  mh.coverage = 255;
  auto mhb = m::encode(mh);
  assert(mhb.size() == 160);
  assert(m::encode(m::decode_header(mhb)) == mhb);
  rejects([&] { mmuctl::decode_header(mhb); });
  m::Baseline bl;
  bl.generation = 1;
  bl.cut_kind = 1;
  bl.known = 15;
  bl.proof_flags = 15;
  bl.epoch_reference = 1;
  auto blb = m::encode(bl);
  assert(blb.size() == 96);
  assert(m::encode(m::decode_baseline(blb)) == blb);
  m::Owner o;
  o.owner_kind = m::OwnerKind::ArchitecturalBoundary;
  o.owner_id = 1;
  o.generation = 1;
  o.status = 1;
  o.op_hash = m::operations_seed();
  auto ob = m::encode(o);
  assert(ob.size() == 64);
  assert(m::encode(m::decode_owner(ob)) == ob);
  o.hot_hash = 1;
  rejects([&] { m::encode(o); });
  o.hot_hash = 0;
  o.causal_event = 1;
  rejects([&] { m::encode(o); });
  m::Footer mf;
  auto mfb = m::encode(mf);
  assert(mfb.size() == 320);
  assert(m::encode(m::decode_footer(mfb)) == mfb);
  mmuctl::Command c;
  c.kind = mmuctl::CommandKind::DemapPage;
  c.va = 0x1234;
  std::vector<mmuctl::Operation> ops{c};
  assert(m::operation_hash(ops) != m::operations_seed());
  namespace cv = core_v2;
  assert(cv::layout_descriptor() ==
         file("docs/superpowers/specs/layouts/spr-core-facts-v2.layout"));
  CoreFunctionalHeader ch;
  ch.policy_sha256[0] = 1;
  ch.architectural_registers = {39, 48, 1, 2, 1, 0, 6, 4096};
  ch.version = 2;
  ch.header_size = 128;
  ch.magic[6] = '2';
  auto chb = cv::encode_header(ch);
  assert(chb.size() == 128);
  assert(cv::encode_header(cv::decode_header(chb)) == chb);
  CoreFunctionalRow cr;
  cr.known_flags = 234;
  cr.frontend_origin = CoreFrontendOrigin::ObserverRecord;
  cr.normalization_status = CoreNormalizationStatus::Complete;
  cv::Extension ext;
  ext.not_applicable_flags = 21;
  ext.semantic_origin = cv::Origin::HardwareEntryRom;
  ext.allocation_policy = cv::AllocationPolicy::IndependentRomMember;
  ext.rom_kind = cv::RomKind::Other;
  ext.extension_known = 7;
  ext.boundary_event = 1;
  ext.boundary_generation = 1;
  cv::set_extension(cr, ext);
  cv::validate_row(ch, cr, {});
  auto crb = cv::encode_row(cr);
  assert(crb.size() == 128);
  assert(crb[96] == 21 && crb[100] == 2 && crb[101] == 2 && crb[103] == 7);
  assert(cv::encode_row(cv::decode_row(crb)) == crb);
  rejects([&] { validate_core_row(ch, cr, {}); });
  cr.known_flags |= 1;
  rejects([&] { cv::validate_row(ch, cr, {}); });
  cr.known_flags = 234;
  ext.semantic_origin = cv::Origin::UnsupportedRom;
  cv::set_extension(cr, ext);
  rejects([&] { cv::validate_row(ch, cr, {}); });
  {
    a::Writer writer("tmp/p4-architectural-bundle/test.archb.gz", ah);
    writer.epoch(e);
    writer.accepted(acc);
    writer.invocation_seal(s);
    a::Terminal partial;
    partial.event = 1;
    partial.generation = 1;
    partial.status = 2;
    partial.reason_mask = 1u << 7;
    partial.seal_flags = 3;
    partial.control_op_hash = m::operations_seed();
    writer.terminal(partial);
    a::Footer footer;
    footer.accepted = footer.accepted_irq = footer.terminal_partial =
        footer.invocation_sealed = footer.event_rows = footer.epoch_rows = 1;
    footer.reasons[7] = 1;
    writer.finish(footer);
    rejects([&] { writer.accepted(acc); });
  }
  {
    m::Writer writer("tmp/p4-architectural-bundle/test.mmuctl.gz", mh);
    writer.baseline(bl);
    o.causal_event = 0;
    writer.owner(o, {});
    m::Footer footer;
    footer.boundary_groups = footer.event_rows =
        footer.invocation_sealed_boundary = 1;
    writer.finish(footer);
    rejects([&] { writer.owner(o, {}); });
  }
  for (auto offset : {8u, 80u, 96u, 100u, 104u, 109u, 112u, 136u}) {
    auto bad = a::encode(ah);
    bad[offset] = 255;
    rejects([&] { a::decode_header(bad); });
  }
  for (auto offset :
       {24u, 27u, 28u, 34u, 72u, 73u, 74u, 75u, 76u, 160u, 164u, 168u}) {
    auto bad = a::encode(acc);
    bad[offset] = 255;
    rejects([&] { a::decode_accepted(bad); });
  }
  for (auto offset : {24u, 28u, 112u, 114u, 115u}) {
    auto bad = a::encode(s);
    bad[offset] = 255;
    rejects([&] { a::decode_invocation_seal(bad); });
  }
  for (auto offset : {56u, 60u, 61u}) {
    auto bad = a::encode(member);
    bad[offset] = 255;
    rejects([&] { a::decode_entry_member(bad); });
  }
  ext.semantic_origin = cv::Origin::HardwareEntryRom;
  cv::set_extension(cr, ext);
  for (auto offset : {96u, 100u, 101u, 102u, 103u, 120u}) {
    auto bad = cv::encode_row(cr);
    bad[offset] = 255;
    rejects([&] { cv::validate_row(ch, cv::decode_row(bad), {}); });
  }
  {
    a::StreamValidator unknown_epoch(ah);
    auto unknown = e;
    unknown.proof_flags = 0;
    unknown_epoch.accept(unknown);
    auto invalid = acc;
    invalid.reason_mask = a::reason(a::Reason::InitialEpochUnknown);
    rejects([&] { unknown_epoch.accept(invalid); });
  }
  std::cout << "architectural bundle codecs passed\n";
}
