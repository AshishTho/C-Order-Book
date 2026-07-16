#pragma once
// ============================================================================
// flat_hash.hpp -- Zero-allocation sparse hash map (u64 -> u64).
//                  PHASE 6: production reality check.
//
// WHY THIS EXISTS
// ---------------
// The gateway must translate EXCHANGE order references (the 64-bit ids in
// ITCH Add/Execute/Cancel/Delete) into ENGINE order ids. A dense array
// indexed by reference works only for synthetic feeds with sequential
// refs; real exchange references are SPARSE 64-bit integers -- session-
// scoped counters with huge gaps, multiplexed across matching engine
// shards. Production needs a real map. std::unordered_map is banned on
// sight: node-per-element heap allocation, a pointer chase per lookup
// (guaranteed cache miss), and rehash spikes mid-session.
//
// DESIGN: OPEN-ADDRESSED, LINEAR PROBING, BACKWARD-SHIFT DELETION
// ---------------------------------------------------------------
// * ONE CONTIGUOUS BLOCK. All state is a single cache-line-aligned array
//   of 16-byte {key, val} slots, allocated ONCE at construction (cold
//   path, same policy as the order arena). No pointers, no nodes, no
//   indirection: a lookup touches ONLY this block. Zero allocation after
//   construction -- there is no rehash, by design: capacity is sized for
//   the instrument's worst-case live-order count (a known number you
//   provision for, exactly like the order pool), and insert() rejects
//   beyond the load cap rather than ever allocating.
//
// * LINEAR PROBING, NOT ROBIN HOOD. Both are O(1) at our load factor; we
//   probe LINEARLY because the next slot is on the same or the next cache
//   line -- the prefetcher's favourite access pattern. Robin Hood's
//   displacement bookkeeping earns its keep above ~80% load; we cap load
//   at 50% (enforced in the ctor via MaxLoadPct), where linear probe
//   chains average ~1.5 slots = usually ONE cache line touched per op.
//   4 slots per line: a miss probe often resolves without a second line.
//
// * HASH: multiply-shift (Fibonacci hashing). One IMUL + one SHR -- ~4
//   cycles -- and the shift takes the TOP bits of the product, which mix
//   the entire key (low-bit-only patterns in refs cannot cluster). For
//   already-sparse keys this is as good as murmur at a third of the cost.
//
// * DELETION: BACKWARD SHIFT, NOT TOMBSTONES. This is the load-bearing
//   choice for trading: an ITCH session deletes orders at nearly the rate
//   it adds them (every fill/cancel erases a mapping). Tombstones never
//   shrink probe chains -- by afternoon every lookup would crawl through
//   morning's graves. Backward-shift deletion restores the table to the
//   exact state as if the erased key had never been inserted: probe
//   lengths CANNOT degrade over the session, so the P99 at 15:59 equals
//   the P99 at 09:31. Erase pays a short shift (bounded by the probe
//   chain, ~1-2 slots at 50% load) to keep every future lookup optimal.
//
// * KEY 0 IS THE EMPTY SENTINEL. ITCH order references are non-zero, so
//   no per-slot occupancy flag is needed -- the key field doubles as the
//   metadata, keeping slots at exactly 16 bytes / 4-per-line.
// ============================================================================

#include "common.hpp"
#include <cstdlib>
#include <cstring>

namespace lob {

template <std::size_t CapacityPow2, unsigned MaxLoadPct = 50>
class FlatHashMap {
    static_assert((CapacityPow2 & (CapacityPow2 - 1)) == 0 && CapacityPow2 >= 4,
                  "capacity must be a power of two");
    static_assert(MaxLoadPct >= 10 && MaxLoadPct <= 90, "sane load cap only");
    static constexpr std::uint64_t MASK = CapacityPow2 - 1;

    // log2(CapacityPow2), computed at compile time for the hash shift.
    static constexpr unsigned LOG2 = [] {
        unsigned n = 0;
        for (std::size_t c = CapacityPow2; c > 1; c >>= 1) ++n;
        return n;
    }();

    struct Entry { std::uint64_t key, val; };   // 16B -> 4 per cache line
    static_assert(sizeof(Entry) == 16);

public:
    // COLD PATH -- the one and only allocation, zeroed (key 0 == empty).
    FlatHashMap() {
#if defined(_WIN32)
        slots_ = static_cast<Entry*>(
            _aligned_malloc(CapacityPow2 * sizeof(Entry), CACHE_LINE));
#else
        slots_ = static_cast<Entry*>(
            std::aligned_alloc(CACHE_LINE, CapacityPow2 * sizeof(Entry)));
#endif
        if (!slots_) std::abort();
        std::memset(slots_, 0, CapacityPow2 * sizeof(Entry)); // + pre-touch
    }
    ~FlatHashMap() {
#if defined(_WIN32)
        _aligned_free(slots_);
#else
        std::free(slots_);
#endif
    }
    FlatHashMap(const FlatHashMap&)            = delete;
    FlatHashMap& operator=(const FlatHashMap&) = delete;

    // ------------------------------------------------------------------
    // Fibonacci multiply-shift: top LOG2 bits of key * 2^64/phi. The top
    // bits of the product depend on EVERY input bit (carries propagate
    // upward), so sequential or strided refs spread uniformly.
    // ------------------------------------------------------------------
    LOB_FORCE_INLINE static std::uint64_t slot_of(std::uint64_t key) noexcept {
        return (key * 0x9E3779B97F4A7C15ull) >> (64 - LOG2);
    }

    // ========================================================================
    // HOT PATH -- insert / overwrite. Returns false only if the load cap
    // is hit (provisioning failure => reject the order upstream; NEVER
    // allocate or rehash mid-session).
    // ========================================================================
    LOB_FORCE_INLINE bool insert(std::uint64_t key, std::uint64_t val) noexcept {
        if (LOB_UNLIKELY(size_ >= CapacityPow2 * MaxLoadPct / 100)) return false;
        std::uint64_t i = slot_of(key);
        // Common case: home slot free or holds this key -- zero iterations.
        while (slots_[i].key != 0) {
            if (slots_[i].key == key) { slots_[i].val = val; return true; }
            i = (i + 1) & MASK;                 // next slot, same/next line
        }
        slots_[i] = { key, val };
        ++size_;
        return true;
    }

    // ========================================================================
    // HOT PATH -- lookup. Returns the value or 0 (0 is never a valid
    // engine id, mirroring the book's "rejected" sentinel).
    // ========================================================================
    LOB_FORCE_INLINE std::uint64_t find(std::uint64_t key) const noexcept {
        std::uint64_t i = slot_of(key);
        while (slots_[i].key != 0) {
            if (LOB_LIKELY(slots_[i].key == key)) return slots_[i].val;
            i = (i + 1) & MASK;
        }
        return 0;                               // empty slot ends the chain
    }

    // ========================================================================
    // HOT PATH -- erase with backward-shift compaction. Restores the table
    // to "key never existed": probe chains can never grow over a session.
    // ========================================================================
    LOB_FORCE_INLINE bool erase(std::uint64_t key) noexcept {
        std::uint64_t i = slot_of(key);
        while (slots_[i].key != key) {
            if (slots_[i].key == 0) return false;        // not present
            i = (i + 1) & MASK;
        }
        // Shift successors back while doing so keeps them reachable from
        // their home slot. Entry at j (home h) may move into hole i iff i
        // lies on its probe path, i.e. cyclic-dist(h -> j) covers (i -> j).
        std::uint64_t j = i;
        for (;;) {
            j = (j + 1) & MASK;
            if (slots_[j].key == 0) break;               // chain ends
            const std::uint64_t h = slot_of(slots_[j].key);
            if (((j - h) & MASK) >= ((j - i) & MASK)) {  // i on j's path
                slots_[i] = slots_[j];                   // move back
                i = j;                                   // hole moves to j
            }
        }
        slots_[i].key = 0;                               // final hole
        --size_;
        return true;
    }

    LOB_FORCE_INLINE std::size_t size() const noexcept { return size_; }
    static constexpr std::size_t capacity() noexcept { return CapacityPow2; }

private:
    Entry*      slots_;         // the single contiguous block
    std::size_t size_ = 0;
};

} // namespace lob
