#pragma once
#include "fastsim/fst_mmu_controls.hpp"
#include <algorithm>
#include <fstream>
namespace fastsim::fst::bundle_detail {
using namespace mmuctl;
class BundleInput {
  std::ifstream file_;
  z_stream z_{};
  std::array<unsigned char, 65536> in_{}, out_{};
  std::size_t pos_ = 0, end_ = 0;
  bool done_ = false;
  bool refill() {
    if (pos_ < end_)
      return true;
    if (done_)
      return false;
    pos_ = end_ = 0;
    while (!end_ && !done_) {
      if (!z_.avail_in) {
        file_.read(reinterpret_cast<char *>(in_.data()), in_.size());
        check(file_.gcount() > 0, "truncated gzip");
        z_.next_in = in_.data();
        z_.avail_in = static_cast<uInt>(file_.gcount());
      }
      z_.next_out = out_.data();
      z_.avail_out = static_cast<uInt>(out_.size());
      int status = inflate(&z_, Z_NO_FLUSH);
      end_ = out_.size() - z_.avail_out;
      if (status == Z_STREAM_END) {
        done_ = true;
        check(!z_.avail_in && file_.peek() == std::char_traits<char>::eof(),
              "physical trailing bytes/member");
      } else
        check(status == Z_OK, "gzip integrity");
    }
    return end_ != 0;
  }

public:
  explicit BundleInput(const std::string &path)
      : file_(path, std::ios::binary) {
    check(bool(file_), "missing companion");
    check(inflateInit2(&z_, 31) == Z_OK, "inflate init");
  }
  ~BundleInput() { inflateEnd(&z_); }
  Bytes read(unsigned n) {
    Bytes b(n);
    unsigned at = 0;
    while (at < n) {
      check(refill(), "truncated stream/footer");
      auto take = std::min<std::size_t>(n - at, end_ - pos_);
      std::memcpy(b.data() + at, out_.data() + pos_, take);
      at += static_cast<unsigned>(take);
      pos_ += take;
    }
    return b;
  }
  Bytes frame(const std::array<unsigned, 7> &sizes) {
    auto b = read(8);
    Cursor c{b};
    auto kind = c.get(2);
    check(c.get(2) == 0, "frame flags");
    auto size = c.get(4);
    check(kind >= 1 && kind <= 6 && sizes[kind] >= 8 && size >= 8 &&
              size == sizes[kind],
          "frame kind/size");
    auto rest = read(static_cast<unsigned>(size - 8));
    b.insert(b.end(), rest.begin(), rest.end());
    return b;
  }
  void eof() { check(!refill(), "trailing decoded bytes"); }
};

inline unsigned kind(const Bytes &b) { return b[0] | unsigned(b[1]) << 8; }
inline void fold(std::uint64_t &h, const Bytes &b) {
  h = hash_bytes(h, b.data(), b.size());
}
} // namespace fastsim::fst::bundle_detail
