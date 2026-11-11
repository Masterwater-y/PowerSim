#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <utility>
#include <vector>

namespace fastsim {

// Value snapshots share immutable pages. A write detaches only its directory
// and leaf; abandoned feedback never has to mutate or undo the accepted root.
// Logical sizes remain bounded by the owning service/resource budget.
template<class T, std::size_t PageSize = 64>
class ServiceSnapshotVector {
 public:
  struct Cost {
    std::uint64_t leaves = 0, directories = 0, bytes = 0;
    Cost& operator+=(const Cost& b) {
      leaves += b.leaves; directories += b.directories; bytes += b.bytes;
      return *this;
    }
  };
  std::size_t size() const { return size_; }
  bool empty() const { return !size_; }
  void reserve(std::size_t n) { roots_.reserve((n + span - 1) / span); }
  void assign(std::size_t n, const T& value) {
    roots_.clear(); size_ = n;
    roots_.resize((n + span - 1) / span);
    // Empty pages represent value-initialized cells without allocating them.
    if (!(value == T{}))
      for (std::size_t i = 0; i < n; ++i) set(i, value);
  }
  const T& at(std::size_t i) const {
    if (i >= size_) throw std::out_of_range("service snapshot index");
    const auto& directory = roots_[i / span];
    if (!directory || !(*directory)[(i / PageSize) % fanout]) {
      static const T zero{};
      return zero;
    }
    return (*(*directory)[(i / PageSize) % fanout])[i % PageSize];
  }
  T& write(std::size_t i) {
    if (i >= size_) throw std::out_of_range("service snapshot index");
    auto& directory = roots_[i / span];
    if (!directory) directory = std::make_shared<Directory>();
    else if (!directory.unique()) {
      directory = std::make_shared<Directory>(*directory);
      ++cost_.directories; cost_.bytes += sizeof(Directory);
    }
    auto& page = (*directory)[(i / PageSize) % fanout];
    if (!page) page = std::make_shared<Page>();
    else if (!page.unique()) {
      page = std::make_shared<Page>(*page);
      ++cost_.leaves; cost_.bytes += sizeof(Page);
    }
    return (*page)[i % PageSize];
  }
  void set(std::size_t i, const T& value) { write(i) = value; }
  void push_back(const T& value) {
    const auto i = size_++;
    if (i / span == roots_.size()) roots_.push_back(nullptr);
    set(i, value);
  }
  T pop_back() {
    const T value = at(size_ - 1);
    --size_;
    return value;
  }
  const Cost& cost() const { return cost_; }
 private:
  static constexpr std::size_t fanout = 64, span = fanout * PageSize;
  using Page = std::array<T, PageSize>;
  using Directory = std::array<std::shared_ptr<Page>, fanout>;
  std::vector<std::shared_ptr<Directory>> roots_;
  std::size_t size_ = 0;
  Cost cost_;
};

// Heap operations follow std::push_heap/pop_heap's child choice, including
// equal keys. Read comparisons do not detach pages.
template<class T>
class ServiceSnapshotHeap {
 public:
  using Cost = typename ServiceSnapshotVector<T>::Cost;
  std::size_t size() const { return values_.size(); }
  bool empty() const { return values_.empty(); }
  const T& top() const { return values_.at(0); }
  const T& at(std::size_t i) const { return values_.at(i); }
  void reserve(std::size_t n) { values_.reserve(n); }
  void assign(std::size_t n, const T& value) { values_.assign(n, value); }
  void swap(ServiceSnapshotHeap& b) { std::swap(values_, b.values_); }
  template<class Less>
  void push(const T& value, Less less) {
    auto hole = values_.size();
    values_.push_back(value);
    rise(hole, 0, value, less);
  }
  template<class Less>
  T pop(Less less) {
    const T first = top();
    const T value = values_.pop_back();
    const auto n = values_.size();
    if (!n) return first;
    std::size_t hole = 0, child = 2;
    while (child < n) {
      if (less(values_.at(child - 1), values_.at(child))) --child;
      values_.set(hole, values_.at(child));
      hole = child;
      child = 2 * (hole + 1);
    }
    if (child == n) {
      values_.set(hole, values_.at(child - 1));
      hole = child - 1;
    }
    rise(hole, 0, value, less);
    return first;
  }
  const Cost& cost() const { return values_.cost(); }
 private:
  template<class Less>
  void rise(std::size_t hole, std::size_t top, const T& value, Less less) {
    while (hole > top) {
      const auto parent = (hole - 1) / 2;
      if (!less(value, values_.at(parent))) break;
      values_.set(hole, values_.at(parent));
      hole = parent;
    }
    values_.set(hole, value);
  }
  ServiceSnapshotVector<T> values_;
};

class ServiceSnapshotLineIndex {
 public:
  using Id = std::uint64_t;
  using Cost = typename ServiceSnapshotVector<Id>::Cost;
  void configure(std::size_t capacity) {
    slots_ = 2;
    while (slots_ < capacity * 2) slots_ *= 2;
    cells_.assign(slots_, Cell{});
    size_ = tombstones_ = 0;
  }
  Id get(std::uint64_t key) const {
    if (!slots_) return 0;
    for (std::size_t i = hash(key) & (slots_ - 1), n = 0; n < slots_; ++n, i = (i + 1) & (slots_ - 1)) {
      const auto& cell = cells_.at(i);
      if (!cell.kind) return 0;
      if (cell.kind == 1 && cell.key == key) return cell.id;
    }
    return 0;
  }
  void put(std::uint64_t key, Id id) {
    if (!slots_) configure(16384);
    // Amortized compaction prevents ROI-length tombstone accumulation.
    if (tombstones_ >= slots_ / 4 && tombstones_) rebuild();
    std::size_t deleted = slots_;
    for (std::size_t i = hash(key) & (slots_ - 1), n = 0; n < slots_; ++n, i = (i + 1) & (slots_ - 1)) {
      const auto& cell = cells_.at(i);
      if (cell.kind == 1 && cell.key == key) {
        cells_.set(i, Cell{key, id, 1}); return;
      }
      if (cell.kind == 2 && deleted == slots_) deleted = i;
      if (!cell.kind) {
        const auto at = deleted == slots_ ? i : deleted;
        cells_.set(at, Cell{key, id, 1});
        ++size_;
        tombstones_ -= deleted != slots_;
        return;
      }
    }
    throw std::logic_error("service line index exceeded its bounded load");
  }
  void erase(std::uint64_t key) {
    if (!slots_) return;
    for (std::size_t i = hash(key) & (slots_ - 1), n = 0; n < slots_; ++n, i = (i + 1) & (slots_ - 1)) {
      const auto& cell = cells_.at(i);
      if (!cell.kind) return;
      if (cell.kind == 1 && cell.key == key) {
        cells_.write(i).kind = 2; --size_; ++tombstones_; return;
      }
    }
  }
  template<class F> void each(F f) const {
    for (std::size_t i = 0; i < slots_; ++i) {
      const auto& cell = cells_.at(i);
      if (cell.kind == 1) f(cell.key, cell.id);
    }
  }
  std::size_t size() const { return size_; }
  Cost cost() const {
    const auto& c = cells_.cost();
    return {c.leaves, c.directories, c.bytes};
  }
  std::uint64_t rebuilds() const { return rebuilds_; }
 private:
  struct Cell {
    std::uint64_t key = 0, id = 0;
    std::uint8_t kind = 0;
    bool operator==(const Cell& b) const {
      return key == b.key && id == b.id && kind == b.kind;
    }
  };
  static std::uint64_t hash(std::uint64_t x) {
    x ^= x >> 30; x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27; x *= 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
  }
  void rebuild() {
    const auto old = cells_;
    cells_.assign(slots_, Cell{}); size_ = tombstones_ = 0; ++rebuilds_;
    for (std::size_t i = 0; i < slots_; ++i) {
      const auto& cell = old.at(i);
      if (cell.kind == 1) put(cell.key, cell.id);
    }
  }
  ServiceSnapshotVector<Cell> cells_;
  std::size_t slots_ = 0, size_ = 0, tombstones_ = 0;
  std::uint64_t rebuilds_ = 0;
};
}  // namespace fastsim
