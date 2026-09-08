#include "arch/x86/qemu_raw_trace_reader.hh"

#include <algorithm>

#include <zlib.h>

#include "base/logging.hh"

namespace gem5
{
namespace X86ISA
{

std::vector<std::filesystem::path>
QemuRawTraceReader::discover(const std::filesystem::path &input)
{
    std::vector<std::filesystem::path> files;
    if (std::filesystem::is_regular_file(input)) {
        files.push_back(input);
    } else {
        for (const auto &entry :
             std::filesystem::recursive_directory_iterator(input)) {
            if (entry.is_regular_file() &&
                (entry.path().extension() == ".trace" ||
                 entry.path().extension() == ".gz")) {
                files.push_back(entry.path());
            }
        }
        std::sort(files.begin(), files.end());
    }
    fatal_if(files.empty(), "QEMU-FST input has no .trace files: %s",
             input.c_str());
    return files;
}

QemuRawTraceReader::QemuRawTraceReader(const std::filesystem::path &path)
    : path(path), compressed(path.extension() == ".gz")
{
    if (compressed) {
        gzipFile = gzopen(path.c_str(), "rb");
        fatal_if(!gzipFile, "failed to open QEMU-FST gzip trace: %s",
                 path.c_str());
        gzbuffer(gzipFile, static_cast<unsigned int>(
            EntriesPerBlock * sizeof(dynamorio::drmemtrace::trace_entry_t)));
    } else {
        inputFile.open(path, std::ios::binary);
        fatal_if(!inputFile, "failed to open QEMU-FST trace file: %s",
                 path.c_str());
    }
}

QemuRawTraceReader::~QemuRawTraceReader()
{
    if (gzipFile) {
        fatal_if(gzclose(gzipFile) != Z_OK,
                 "failed closing QEMU-FST gzip trace: %s", path.c_str());
    }
}

bool
QemuRawTraceReader::nextBlock(Block &block)
{
    size_t count = 0;
    if (compressed) {
        const int bytes = gzread(gzipFile, buffer.data(),
            static_cast<unsigned int>(buffer.size() * sizeof(buffer[0])));
        fatal_if(bytes < 0, "failed reading QEMU-FST gzip trace: %s",
                 path.c_str());
        fatal_if(bytes != 0 && bytes % static_cast<int>(sizeof(buffer[0])) != 0,
                 "QEMU-FST gzip trace has a partial trailing record: %s",
                 path.c_str());
        count = static_cast<size_t>(bytes) / sizeof(buffer[0]);
    } else {
        inputFile.read(reinterpret_cast<char *>(buffer.data()),
            static_cast<std::streamsize>(buffer.size() * sizeof(buffer[0])));
        const auto bytes = inputFile.gcount();
        fatal_if(bytes != 0 &&
                     bytes % static_cast<std::streamsize>(sizeof(buffer[0])) != 0,
                 "QEMU-FST trace has a partial trailing record: %s",
                 path.c_str());
        count = static_cast<size_t>(bytes) / sizeof(buffer[0]);
    }
    if (count == 0) {
        block = {};
        return false;
    }
    block = {buffer.data(), count};
    return true;
}

} // namespace X86ISA
} // namespace gem5
