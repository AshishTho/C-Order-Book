#pragma once
// ============================================================================
// level_bitmap.hpp -- Hierarchical occupancy bitmap over the price ladder.
//
// WHY THIS EXISTS
// ---------------
// The book stores price levels in a FLAT ARRAY indexed by tick. That makes
// insert/lookup O(1), but "find the next non-empty level" (needed when a
// sweep empties the best level) would naively be a linear scan -- O(gap).
// A sparse book with a 10,000-tick gap would blow the P99.9 budget.
//
// Instead we keep a 3-level bitmap: one bit per price level (L0), one bit
// per L0 word (L1), one bit per L1 word (L2). "Next set bit >= i" is then
// at most 5 TZCNT instructions and 5 loads regardless of gap size --
// CONSTANT TIME, which is exactly what bounds the tail.
//
// TZCNT/LZCNT are 3-cycle instructions on modern x86; std::countr_zero /
// std::countl_zero compile straight to them with -march=native.
//
// Capacity 65,536 ticks => L0 = 1024 words (8 KB, fits in L1d cache),
// L1 = 16 words, L2 = 1 word. The whole index is ~8.2 KB.
// ============================================================================

#include "common.hpp"
#include <bit>
#include <cstring>

namespace lob {

template <std::size_t NumBits>
class LevelBitmap {
    static_assert(NumBits % 64 == 0);
    static constexpr std::size_t L0_WORDS = NumBits / 64;
    static constexpr std::size_t L1_WORDS = (L0_WORDS + 63) / 64;
    static constexpr std::size_t L2_WORDS = (L1_WORDS + 63) / 64;
    static_assert(L2_WORDS == 1, "three levels are enough for <= 256K ticks");

public:
    static constexpr std::size_t NPOS = ~std::size_t{0};

    LevelBitmap() noexcept { clear(); }

    void clear() noexcept {
        std::memset(l0_, 0, sizeof l0_);
        std::memset(l1_, 0, sizeof l1_);
        l2_ = 0;
    }

    // Mark level `i` occupied. Setting summary bits unconditionally is
    // cheaper than testing first (no branch, stores to already-hot lines).
    LOB_FORCE_INLINE void set(std::size_t i) noexcept {
        l0_[i >> 6]        |= 1ull << (i & 63);
        l1_[i >> 12]       |= 1ull << ((i >> 6) & 63);
        l2_                |= 1ull << (i >> 12);
    }

    // Mark level `i` empty; propagate emptiness up only when a word zeroes.
    LOB_FORCE_INLINE void reset(std::size_t i) noexcept {
        if ((l0_[i >> 6] &= ~(1ull << (i & 63))) == 0) {
            if ((l1_[i >> 12] &= ~(1ull << ((i >> 6) & 63))) == 0) {
                l2_ &= ~(1ull << (i >> 12));
            }
        }
    }

    LOB_FORCE_INLINE bool test(std::size_t i) const noexcept {
        return (l0_[i >> 6] >> (i & 63)) & 1u;
    }

    // Lowest set bit >= i, or NPOS. Used to find the next best ASK.
    LOB_FORCE_INLINE std::size_t next_set(std::size_t i) const noexcept {
        if (LOB_UNLIKELY(i >= NumBits)) return NPOS;
        std::size_t w = i >> 6;
        // 1) tail of the current L0 word
        std::uint64_t m = l0_[w] & (~0ull << (i & 63));
        if (LOB_LIKELY(m)) return (w << 6) + std::countr_zero(m);
        // 2) rest of the current L1 word (skips 64 levels per bit)
        std::size_t w1 = w >> 6;
        m = l1_[w1] & shl_safe(~0ull, (w & 63) + 1);
        if (m) { w = (w1 << 6) + std::countr_zero(m);
                 return (w << 6) + std::countr_zero(l0_[w]); }
        // 3) L2 (skips 4096 levels per bit)
        m = l2_ & shl_safe(~0ull, w1 + 1);
        if (m) { w1 = std::countr_zero(m);
                 w  = (w1 << 6) + std::countr_zero(l1_[w1]);
                 return (w << 6) + std::countr_zero(l0_[w]); }
        return NPOS;
    }

    // Highest set bit <= i, or NPOS. Used to find the next best BID.
    LOB_FORCE_INLINE std::size_t prev_set(std::size_t i) const noexcept {
        std::size_t w = i >> 6;
        std::uint64_t m = l0_[w] & shr_safe(~0ull, 63 - (i & 63));
        if (LOB_LIKELY(m)) return (w << 6) + bsr(m);
        std::size_t w1 = w >> 6;
        m = l1_[w1] & shr_safe(~0ull, 64 - (w & 63));
        if (m) { w = (w1 << 6) + bsr(m);
                 return (w << 6) + bsr(l0_[w]); }
        m = l2_ & shr_safe(~0ull, 63 - w1 + 1);
        if (m) { w1 = bsr(m);
                 w  = (w1 << 6) + bsr(l1_[w1]);
                 return (w << 6) + bsr(l0_[w]); }
        return NPOS;
    }

private:
    // x << 64 / x >> 64 are UB in C++; these helpers make the boundary case
    // (mask covering zero bits) well-defined without a branch mispredict.
    static LOB_FORCE_INLINE std::uint64_t shl_safe(std::uint64_t v, std::size_t s) noexcept {
        return s >= 64 ? 0ull : (v << s);
    }
    static LOB_FORCE_INLINE std::uint64_t shr_safe(std::uint64_t v, std::size_t s) noexcept {
        return s >= 64 ? 0ull : (v >> s);
    }
    static LOB_FORCE_INLINE std::size_t bsr(std::uint64_t v) noexcept {
        return 63 - static_cast<std::size_t>(std::countl_zero(v));
    }

    alignas(CACHE_LINE) std::uint64_t l0_[L0_WORDS];
    alignas(CACHE_LINE) std::uint64_t l1_[L1_WORDS];
    std::uint64_t l2_;
};

} // namespace lob
