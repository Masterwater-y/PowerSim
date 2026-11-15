// Functional exception companions are independent of memory/serialize bits.
#include "test_spr_store_group_runtime.cpp"

namespace {
enum class Marker { kNone, kHardwareEntry, kPreciseFault };

class ExceptionSource final : public TraceSource {
 public:
    ExceptionSource(std::vector<Step> steps, Marker marker,
                    std::uint64_t marker_index, std::uint64_t open_index)
        : source_(std::move(steps)), marker_(marker), marker_index_(marker_index),
          open_index_(open_index) {}
    bool next(TraceRecord& record) override {
        if (!source_.next(record)) return false;
        const auto ordinal=*source_.current_source_record_ordinal();
        if (marker_==Marker::kPreciseFault && ordinal==open_index_)
            record.flags |= kMicroOp; // open macro prefix for the precise-fault transition
        return true;
    }
    std::optional<std::uint64_t> current_source_record_ordinal() const override {
        return source_.current_source_record_ordinal();
    }
    const fst::StoreRequestFacts* current_store_facts() const override {
        return source_.current_store_facts();
    }
    const fst::StoreRequestFacts* current_memory_facts() const override {
        return source_.current_memory_facts();
    }
    bool current_hardware_entry() const override {
        return marker_==Marker::kHardwareEntry &&
               source_.current_source_record_ordinal()==marker_index_;
    }
    bool current_precise_fault_abandonment() const override {
        return marker_==Marker::kPreciseFault &&
               source_.current_source_record_ordinal()==marker_index_;
    }
    std::string description() const override { return "functional exception-boundary fixture"; }
 private:
    FunctionalStoreSource source_;
    Marker marker_;
    std::uint64_t marker_index_,open_index_;
};

SimulationStats run_exception(Marker marker,bool peer,bool enabled,const std::string& dram) {
    const std::vector<Step> group = peer
        ? std::vector<Step>{{Kind::kStore,0x40000},{Kind::kStore,0x40008}}
        : std::vector<Step>{{Kind::kStore,0x40000},{Kind::kAlu,0},
                            {Kind::kAlu,0},{Kind::kStore,0x40008}};
    auto c=group_config(enabled,128,16);
    c.cores=peer?2:1;
    c.dram.scheduler=dram;
    if (dram=="frfcfs") c.dram.frfcfs_selection_window=4;
    std::vector<std::unique_ptr<TraceSource>> traces;
    if (peer) {
        traces.push_back(std::make_unique<FunctionalStoreSource>(group));
        traces.push_back(std::make_unique<ExceptionSource>(
            std::vector<Step>{{Kind::kAlu,0},{Kind::kAlu,0}},marker,1,0));
    } else traces.push_back(std::make_unique<ExceptionSource>(group,marker,2,1));
    Simulator sim(c,std::move(traces)); return sim.run();
}

void exercise(Marker marker,bool peer,const std::string& dram) {
    const auto off=run_exception(marker,peer,false,dram);
    const auto on=run_exception(marker,peer,true,dram);
    check(off.total_core().retired_uops==on.total_core().retired_uops &&
          off.total_core().memory_accesses==on.total_core().memory_accesses,
          "exception companion must not change the retired functional population");
    if (marker==Marker::kNone) {
        check(on.spr_store_group_committed==1 &&
              on.total_sequencer().requests+1==off.total_sequencer().requests,
              "control must actually group the same ordinary UOP pattern");
    } else {
        check(on.spr_store_group_committed==0 &&
              on.total_sequencer().requests==off.total_sequencer().requests,
              "hardware-entry or precise-fault companion must cut the group");
    }
}

void endpoint_marker(Marker marker,bool owner,const std::string& dram) {
    std::vector<Step> steps;
    std::uint64_t marker_index=0, open_index=0;
    if (marker==Marker::kPreciseFault && owner) {
        steps={{Kind::kAlu,0},{Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kStore,0x40008}};
        marker_index=1; open_index=0;
    } else if (marker==Marker::kPreciseFault) {
        steps={{Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kAlu,0},{Kind::kStore,0x40008}};
        marker_index=3; open_index=2;
    } else {
        steps={{Kind::kStore,0x40000},{Kind::kAlu,0},{Kind::kStore,0x40008}};
        marker_index=owner?0:2;
    }
    const auto run_marked = [&](bool enabled) {
        auto c=group_config(enabled,128,16); c.dram.scheduler=dram;
        if(dram=="frfcfs") c.dram.frfcfs_selection_window=4;
        std::vector<std::unique_ptr<TraceSource>> traces;
        traces.push_back(std::make_unique<ExceptionSource>(steps,marker,marker_index,open_index));
        Simulator sim(c,std::move(traces)); return sim.run();
    };
    const auto off=run_marked(false),on=run_marked(true);
    check(on.spr_store_group_committed==0 &&
          on.total_sequencer().requests==off.total_sequencer().requests &&
          on.total_core().retired_uops==off.total_core().retired_uops,
          "exception-marked owner/follower must not enter an ordinary store group");
}
}

int main() {
    try {
        for(const std::string dram : {"fcfs","frfcfs"})
            for(const bool peer : {false,true})
                for(const Marker marker : {Marker::kNone,Marker::kHardwareEntry,Marker::kPreciseFault})
                    exercise(marker,peer,dram);
        for(const std::string dram : {"fcfs","frfcfs"})
            for(const bool owner : {false,true})
                for(const Marker marker : {Marker::kHardwareEntry,Marker::kPreciseFault})
                    endpoint_marker(marker,owner,dram);
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
    std::cout << "SPR store exception boundary tests passed\n";
    return 0;
}
