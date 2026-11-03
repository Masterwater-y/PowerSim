// Read-only accelerator for independent functional-companion stream binding.
#include <array>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
int main(int argc,char**argv) {
    try {
        if (argc!=4) return 2;
        std::size_t n=0;const std::string count_text=argv[2],hash_text=argv[3];
        const auto count=std::stoull(count_text,&n);
        if (n!=count_text.size() || count>(UINT64_MAX-72)/64) return 2;
        const auto expected=std::stoull(hash_text,&n);
        if (n!=hash_text.size()) return 2;
        std::ifstream in(argv[1],std::ios::binary);
        std::array<char,72> header{};in.read(header.data(),header.size());
        if (!in || std::string(header.data(),8)!=std::string("FSTRC01\0",8)) return 2;
        std::uint64_t records=0;
        for (unsigned i=0;i<8;++i) records|=std::uint64_t(static_cast<unsigned char>(header[24+i]))<<(8*i);
        if (records!=count) return 2;
        std::array<unsigned char,1<<20> buffer{};
        std::uint64_t left=count*64,hash=14695981039346656037ull;
        while (left) {
            const auto bytes=left<buffer.size()?left:buffer.size();
            in.read(reinterpret_cast<char*>(buffer.data()),static_cast<std::streamsize>(bytes));
            if (in.gcount()!=static_cast<std::streamsize>(bytes)) return 2;
            for (std::uint64_t i=0;i<bytes;++i) hash=(hash^buffer[i])*1099511628211ull;
            left-=bytes;
        }
        if (hash!=expected) {std::cerr<<"functional retirement stream identity mismatch";return 1;}
        return 0;
    } catch (const std::exception &e) { std::cerr<<e.what();return 2; }
}
