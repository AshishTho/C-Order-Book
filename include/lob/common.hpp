#pragma once
// ============================================================================
// common.hpp -- Core types and hardware constants for the ULL matching engine.
//
// DESIGN NOTES
// ------------
// * All prices and quantities are FIXED-POINT INTEGERS. Floating point is
//   banned on the hot path: FP math has variable latency (denormals can cost
//   100+ cycles), is not associative (non-deterministic across compilers /
//   FMA contraction), and comparisons are unsafe. We represent price as an
//   integer number of "ticks" and expose helpers to convert to/from a human
//   readable decimal ONLY for logging (never called on the hot path).
//
// * CACHE_LINE = 64 bytes on every mainstream x86-64 part. Structures that
//   are written by one core and read by another are alignas(64)-padded so
//   two unrelated fields never share a line (false sharing turns a ~1ns L1
//   hit into a ~40-100ns cross-core coherence stall).
// ============================================================================

#include <cstdint>
#include <cstddef>

namespace lob {

// ---------------------------------------------------------------------------
// Hardware constants
// ---------------------------------------------------------------------------
inline constexpr std::size_t CACHE_LINE = 64;

// Branch-prediction hints. The hot path is written so the *common* case falls
// through untaken branches; these macros document intent and help code layout.
#if defined(__GNUC__) || defined(__clang__)
#  define LOB_LIKELY(x)   (__builtin_expect(!!(x), 1))
#  define LOB_UNLIKELY(x) (__builtin_expect(!!(x), 0))
#  define LOB_FORCE_INLINE inline __attribute__((always_inline))
#else
#  define LOB_LIKELY(x)   (x)
#  define LOB_UNLIKELY(x) (x)
#  define LOB_FORCE_INLINE __forceinline
#endif

// ---------------------------------------------------------------------------
// Fixed-point price / quantity types
// ---------------------------------------------------------------------------
// Price is an integer count of ticks. E.g. with PRICE_SCALE = 10'000 a tick
// of 1 == $0.0001. int64 gives +/- 9.2e18 ticks: overflow is impossible for
// any real instrument.
using Price = std::int64_t;   // price in ticks (fixed point, scale below)
using Qty   = std::int64_t;   // quantity in integer lots
using OrderId = std::uint64_t;

inline constexpr std::int64_t PRICE_SCALE = 10'000;  // 4 implied decimals

// Conversion helpers -- FOR LOGGING/TESTS ONLY, never on the hot path.
inline double price_to_double(Price p) noexcept {
    return static_cast<double>(p) / static_cast<double>(PRICE_SCALE);
}
inline Price price_from_double(double d) noexcept {
    // round-half-away used only in test fixtures
    return static_cast<Price>(d * static_cast<double>(PRICE_SCALE) + (d >= 0 ? 0.5 : -0.5));
}

// Side encoding kept to 1 byte so Order stays inside one cache line.
enum class Side : std::uint8_t { Buy = 0, Sell = 1 };

} // namespace lob
