#include "fastsim/config.hpp"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <iostream>
using namespace fastsim;
int main() {
    SimulatorConfig plain;
    plain.validate();
    assert(!plain.spr_profile.enabled);
    const auto file=std::filesystem::path("tmp/spr-two-stage-implementation-20261104/profile-parse.cfg");
    std::filesystem::create_directories(file.parent_path());
    { std::ofstream out(file); out << "spr.profile.enabled=false\nspr.profile.ordinary_requests=16\nspr.profile.l1_data_fills=16\nspr.profile.l2_data_fills=48\nspr.profile.l1_tbes=256\nspr.profile.iq_execution=128\nspr.profile.iq_memory=72\nspr.profile.iq_store_data=48\n"; }
    auto cfg=load_simulator_config(file.string());
    assert(!cfg.spr_profile.enabled && cfg.spr_profile.limits.ordinary_requests==16);
    assert(cfg.spr_profile.limits.l2_data_fills==48 && cfg.spr_profile.limits.l1_tbes==256);
    assert((cfg.spr_profile.iq_entries==std::array<std::uint32_t,3>{128,72,48}));
    { std::ofstream out(file); out << "core.causal_static_integer=false\nspr.profile.integer_nominal_shape_verified=true\nspr.profile.static_uop_policy_sha256=4242424242424242424242424242424242424242424242424242424242424242\n"; }
    auto static_config=load_simulator_config(file.string());
    assert(!static_config.causal_static_integer && static_config.spr_profile.integer_nominal_shape_verified);
    assert(static_config.spr_profile.static_uop_policy_sha256=="4242424242424242424242424242424242424242424242424242424242424242");
    { std::ofstream out(file); out << "spr.profile.support=pretend-perfect\n"; }
    bool rejected=false; try { (void)load_simulator_config(file.string()); } catch(const std::invalid_argument&) { rejected=true; }
    assert(rejected);
    // A user string cannot certify an unintegrated runtime as implemented.
    cfg.spr_profile.enabled=true;cfg.spr_profile.support=fastsim::spr::SupportStatus::Modeled;
    cfg.spr_profile.limits={16,16,16,48,256,256,256,112,4,4};
    cfg.spr_profile.iq_entries={128,72,48};
    cfg.spr_profile.cache_load_ports=3;cfg.spr_profile.cache_store_ports=2;
    cfg.spr_profile.load_banks=16;cfg.spr_profile.load_bank_ports=2;
    rejected=false;try{cfg.validate();}catch(const std::invalid_argument&){rejected=true;}
    assert(rejected);
    std::filesystem::remove(file);
    std::cout << "SPR profile parser and qualification gates passed\n";
}
