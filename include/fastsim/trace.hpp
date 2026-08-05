#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "fastsim/types.hpp"

namespace fastsim {

class TraceSource {
  public:
    virtual ~TraceSource() = default;
    virtual bool next(TraceRecord& record) = 0;
    virtual std::string description() const = 0;
};

class Gem5JsonlTraceSource final : public TraceSource {
  public:
    explicit Gem5JsonlTraceSource(std::string path);
    bool next(TraceRecord& record) override;
    std::string description() const override;

  private:
    std::string path_;
    std::ifstream input_;
    std::uint64_t line_number_ = 0;
    std::map<std::pair<std::uint64_t, std::uint64_t>, std::uint32_t>
        virtual_page_tokens_;
    std::uint32_t next_virtual_page_token_ = 1;
};

class BinaryTraceSource final : public TraceSource {
  public:
    explicit BinaryTraceSource(std::string path);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    std::uint32_t core_id() const { return core_id_; }
    std::uint64_t record_count() const { return record_count_; }

  private:
    std::string path_;
    std::ifstream input_;
    std::uint32_t core_id_ = 0;
    std::uint64_t record_count_ = 0;
    std::uint64_t records_read_ = 0;
    bool legacy_v2_ = false;
    std::vector<TraceRecord> buffer_;
    std::size_t buffer_cursor_ = 0;
    std::size_t buffer_size_ = 0;
};

class BinaryTraceWriter {
  public:
    BinaryTraceWriter(std::string path, std::uint32_t core_id);
    ~BinaryTraceWriter();
    BinaryTraceWriter(const BinaryTraceWriter&) = delete;
    BinaryTraceWriter& operator=(const BinaryTraceWriter&) = delete;

    void append(const TraceRecord& record);
    void close();
    std::uint64_t record_count() const { return record_count_; }

  private:
    void write_header();
    std::string path_;
    std::fstream output_;
    std::uint32_t core_id_ = 0;
    std::uint64_t record_count_ = 0;
    std::uint64_t feature_flags_ = 0;
    bool closed_ = false;
};

struct SyntheticTraceConfig {
    std::uint32_t core_id = 0;
    std::uint64_t instructions = 1'000'000;
    std::uint64_t working_set_lines = 1ull << 18;
    std::uint32_t memory_percent = 30;
    std::uint32_t write_percent = 20;
    std::uint32_t branch_percent = 15;
    std::uint32_t taken_percent = 60;
    std::uint32_t shared_percent = 5;
    std::uint64_t seed = 1;
};

class SyntheticTraceSource final : public TraceSource {
  public:
    explicit SyntheticTraceSource(SyntheticTraceConfig config);
    bool next(TraceRecord& record) override;
    std::string description() const override;

  private:
    std::uint64_t random();
    SyntheticTraceConfig config_;
    std::uint64_t cursor_ = 0;
    std::uint64_t state_ = 0;
};

struct TraceManifestEntry {
    std::uint32_t core_id = 0;
    std::string format;
    std::string path;
    std::uint32_t source_core_id = 0;
    bool has_source_core_id = false;
};

std::vector<TraceManifestEntry> read_trace_manifest(
    const std::string& manifest_path);
std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores);

void convert_gem5_jsonl_to_binary(const std::string& input_path,
                                  const std::string& output_path,
                                  std::uint32_t core_id);

}  // namespace fastsim
