#pragma once

#include <cstdint>
#include <fstream>
#include <map>
#include <memory>
#include <stdexcept>
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
    virtual bool has_measurement_boundary() const { return false; }
    virtual bool measurement_boundary_pending() const { return false; }
    virtual void start_measurement() {
        throw std::logic_error(
            "trace source has no measurement boundary");
    }
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

// Exposes a macro-instruction-aligned subrange of another functional trace.
// This is used to remove functional warmup prefixes without copying large FST
// files. The wrapper fails closed if the source ends before either boundary.
class InstructionSliceTraceSource final : public TraceSource {
  public:
    InstructionSliceTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t skip_instructions,
        std::uint64_t take_instructions);
    bool next(TraceRecord& record) override;
    std::string description() const override;

  private:
    static bool completes_instruction(const TraceRecord& record);
    void skip_prefix();

    std::unique_ptr<TraceSource> source_;
    std::uint64_t skip_instructions_ = 0;
    std::uint64_t take_instructions_ = 0;
    std::uint64_t skipped_instructions_ = 0;
    std::uint64_t emitted_instructions_ = 0;
    bool prefix_skipped_ = false;
};

// Replays a functional warmup prefix, pauses at its macro-instruction
// boundary, then resumes for a bounded measurement interval. The simulator
// releases the pause only after every active stream reaches the same global
// barrier, so producer lookahead cannot decode ROI UOPs before statistics are
// reset.
class WarmupInstructionTraceSource final : public TraceSource {
  public:
    WarmupInstructionTraceSource(
        std::unique_ptr<TraceSource> source,
        std::uint64_t warmup_instructions,
        std::uint64_t take_instructions);
    bool next(TraceRecord& record) override;
    std::string description() const override;
    bool has_measurement_boundary() const override { return true; }
    bool measurement_boundary_pending() const override {
        return boundary_pending_;
    }
    void start_measurement() override;

  private:
    static bool completes_instruction(const TraceRecord& record);

    std::unique_ptr<TraceSource> source_;
    std::uint64_t warmup_instructions_ = 0;
    std::uint64_t take_instructions_ = 0;
    std::uint64_t warmup_emitted_ = 0;
    std::uint64_t measurement_emitted_ = 0;
    bool boundary_pending_ = false;
    bool measuring_ = false;
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
    std::uint64_t skip_instructions = 0;
    std::uint64_t warmup_instructions = 0;
    std::uint64_t take_instructions = 0;
    bool has_instruction_slice = false;
    bool has_measurement_warmup = false;
};

std::vector<TraceManifestEntry> read_trace_manifest(
    const std::string& manifest_path);
std::vector<std::unique_ptr<TraceSource>> open_trace_manifest(
    const std::string& manifest_path, std::uint32_t maximum_cores);

void convert_gem5_jsonl_to_binary(const std::string& input_path,
                                  const std::string& output_path,
                                  std::uint32_t core_id);

}  // namespace fastsim
