#ifndef __ARCH_X86_QEMU_RECORD_READER_HH__
#define __ARCH_X86_QEMU_RECORD_READER_HH__

#include <array>
#include <filesystem>
#include <fstream>
#include <vector>

#include "arch/x86/qemu_fst_types.hh"

struct gzFile_s;

namespace gem5
{
namespace X86ISA
{

class QemuRecordReader
{
  public:
    explicit QemuRecordReader(const std::filesystem::path &path);
    ~QemuRecordReader();

    QemuRecordReader(const QemuRecordReader &) = delete;
    QemuRecordReader &operator=(const QemuRecordReader &) = delete;

    bool next(CommitRecord &record);
    uint32_t coreId() const;
    bool coreIdKnown() const { return coreIdValid; }

  private:
    std::filesystem::path path;
    bool compressed = false;
    std::ifstream inputFile;
    gzFile_s *gzipFile = nullptr;
    QemuX86State currentState;
    std::vector<uint64_t> stateValues;
    bool requireFullState = true;
    bool roiActive = false;
    bool roiEnded = false;
    bool measurementActive = false;
    bool footerSeen = false;
    bool coreIdValid = false;
    uint32_t logicalCoreId = 0;

    void consumeState(
        const qemu_fst_record::Header &header, CommitRecord &record);
    void validateRecord(const CommitRecord &record) const;
    bool readExact(void *destination, size_t bytes, bool allowEof);
    bool readWireRecord(
        qemu_fst_record::Header &header,
        std::vector<uint64_t> &values,
        std::vector<qemu_fst_record::Access> &recordAccesses);
};

} // namespace X86ISA
} // namespace gem5

#endif
