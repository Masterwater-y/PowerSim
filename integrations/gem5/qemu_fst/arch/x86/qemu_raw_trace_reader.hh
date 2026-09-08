/* Block-oriented transport reader for QEMU trace_entry_t raw shards. */

#ifndef __ARCH_X86_QEMU_RAW_TRACE_READER_HH__
#define __ARCH_X86_QEMU_RAW_TRACE_READER_HH__

#include <array>
#include <cstddef>
#include <filesystem>
#include <fstream>
#include <vector>

#include "lib/drmemtrace/trace_entry.h"

struct gzFile_s;

namespace gem5
{
namespace X86ISA
{

class QemuRawTraceReader
{
  public:
    using Entry = dynamorio::drmemtrace::trace_entry_t;

    struct Block
    {
        const Entry *data = nullptr;
        size_t size = 0;

        const Entry *begin() const { return data; }
        const Entry *end() const { return data + size; }
    };

    static std::vector<std::filesystem::path> discover(
        const std::filesystem::path &input);

    explicit QemuRawTraceReader(const std::filesystem::path &path);
    ~QemuRawTraceReader();

    QemuRawTraceReader(const QemuRawTraceReader &) = delete;
    QemuRawTraceReader &operator=(const QemuRawTraceReader &) = delete;

    bool nextBlock(Block &block);

  private:
    static constexpr size_t EntriesPerBlock = 16 * 1024;

    const std::filesystem::path path;
    const bool compressed;
    std::ifstream inputFile;
    gzFile_s *gzipFile = nullptr;
    std::array<Entry, EntriesPerBlock> buffer = {};
};

} // namespace X86ISA
} // namespace gem5

#endif // __ARCH_X86_QEMU_RAW_TRACE_READER_HH__
