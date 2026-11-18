#include "fst_v7.hpp"
#include <iostream>
int main(int argc, char **argv) try {
    if (argc != 2)
        return 2;
    fstv7::FstReader reader;
    reader.open(argv[1]);
    fstv7::FstRecord r;
    uint64_t records = 0, memory = 0, extra = 0;
    int first_op = 0;
    while (reader.next(r)) {
        if (!records) first_op = r.canonical_op_class();
        ++records;
        memory += r.is_memory();
        extra += reader.current_dependency_extensions().size();
    }
    std::cout << "{\"records\":" << records << ",\"memory\":" << memory
              << ",\"extra_dependencies\":" << extra << ",\"first_op\":" << first_op
              << ",\"semantic_hash\":\"" << reader.semantic_hash() << "\"}\n";
} catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
}
