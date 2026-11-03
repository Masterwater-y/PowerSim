#include "fastsim/trace.hpp"
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
    try {
        if (argc < 2) throw std::runtime_error("expected one or more FST paths");
        std::uint64_t records = 0, spans = 0, slots = 0, partial = 0, pseudo = 0;
        for (int file = 1; file < argc; ++file) {
            fastsim::BinaryTraceSource source(argv[file]);
            fastsim::TraceRecord record;
            while (source.next(record)) {
                const auto* macro = source.current_frontend_macro();
                const auto ordinal = source.current_frontend_record_ordinal();
                if (!macro || !ordinal || macro->known_flags != fastsim::fst::kFrontendRequiredKnown)
                    throw std::runtime_error("missing or unknown retired frontend facts");
                if (!record.retires() || !(macro->flags & fastsim::fst::kFrontendStartsMacro))
                    throw std::runtime_error("frontend input lacks retirement or retained functional start");
                ++records;
                if (*ordinal == macro->begin_record) {
                    ++spans;
                    slots += macro->flags & fastsim::fst::kFrontendSharedAllocation ? 1 : macro->observed_uops;
                    partial += !(macro->flags & fastsim::fst::kFrontendComplete);
                    pseudo += bool(macro->flags & fastsim::fst::kFrontendPseudo);
                }
            }
        }
        std::cout << "{\"status\":\"passed\",\"scope\":\"full-retired-functional-stream\","
                  << "\"files\":" << argc-1 << ",\"records\":" << records
                  << ",\"macro_spans\":" << spans << ",\"allocation_slots\":" << slots
                  << ",\"partial_spans\":" << partial << ",\"pseudo_spans\":" << pseudo << "}\n";
    } catch (const std::exception& error) {
        std::cerr << "retired frontend audit: " << error.what() << '\n';
        return 1;
    }
}
