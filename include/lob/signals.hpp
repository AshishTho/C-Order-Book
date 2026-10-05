#pragma once
// Top-of-book OFI and size-weighted midpoint (often called microprice).
// This weighted midpoint is not an estimated multi-state stochastic microprice.
// Prices use 1e-4 currency units; micro_px has another 8 fractional bits.
// Empty sides contribute their arriving/departing displayed liquidity to OFI.
// Cumulative OFI saturates on int64 overflow; callers must inspect saturations().

#include "common.hpp"
#include <limits>
#if defined(_MSC_VER) && defined(_M_X64)
#  include <intrin.h>
#endif

namespace lob {

struct alignas(CACHE_LINE) SignalUpdate {
    Price         bid_px;       // -1 means side empty
    Price         ask_px;
    Qty           bid_qty;      // aggregate size; may exceed one ITCH u32 order
    Qty           ask_qty;
    std::int64_t  ofi;          // cumulative OFI in shares/lots
    std::int64_t  micro_px;     // price units * 256; -1 means unavailable
    std::uint64_t seq;          // counts observed top transitions, including drops
    std::uint64_t origin_tsc;
};
static_assert(sizeof(SignalUpdate) == CACHE_LINE);

namespace signal_detail {

// floor(a*b/d), with a <= INT64_MAX and 0 <= b < d. The quotient fits u64.
inline std::uint64_t mul_div_portable(std::uint64_t a, std::uint64_t b,
                                     std::uint64_t d) noexcept {
    // Portable exact fallback for targets without a 128-bit multiply/divide.
    std::uint64_t quotient = 0, remainder = 0;
    for (int bit = 63; bit >= 0; --bit) {
        quotient *= 2;
        if (remainder >= d - remainder) {
            remainder -= d - remainder;
            ++quotient;
        } else {
            remainder *= 2;
        }
        if ((a >> bit) & 1) {
            if (remainder >= d - b) {
                remainder -= d - b;
                ++quotient;
            } else {
                remainder += b;
            }
        }
    }
    return quotient;
}

inline std::uint64_t mul_div(std::uint64_t a, std::uint64_t b,
                             std::uint64_t d) noexcept {
#if defined(__SIZEOF_INT128__)
    return static_cast<std::uint64_t>((static_cast<__uint128_t>(a) * b) / d);
#elif defined(_MSC_VER) && defined(_M_X64)
    unsigned __int64 high, remainder;
    const auto low = _umul128(a, b, &high);
    return _udiv128(high, low, d, &remainder);
#else
    return mul_div_portable(a, b, d);
#endif
}

inline std::int64_t saturated_add(std::int64_t a, std::int64_t b,
                                  std::uint64_t& saturations) noexcept {
    constexpr auto hi = (std::numeric_limits<std::int64_t>::max)();
    constexpr auto lo = (std::numeric_limits<std::int64_t>::min)();
    if (b > 0 && a > hi - b) { ++saturations; return hi; }
    if (b < 0 && a < lo - b) { ++saturations; return lo; }
    return a + b;
}

// Bid contribution: a new/improved quote adds current size, a removed/worse
// quote removes old size, and an unchanged price contributes the size delta.
inline Qty bid_flow(Price px, Qty qty, Price old_px, Qty old_qty) noexcept {
    if (px < 0) return -old_qty;
    if (old_px < 0 || px > old_px) return qty;
    if (px < old_px) return -old_qty;
    return qty - old_qty;
}
inline Qty ask_flow(Price px, Qty qty, Price old_px, Qty old_qty) noexcept {
    if (px < 0) return old_qty;
    if (old_px < 0 || px < old_px) return -qty;
    if (px > old_px) return old_qty;
    return old_qty - qty;
}
} // namespace signal_detail

// Exact floor of (bid*ask_size + ask*bid_size)*256/(bid_size+ask_size).
// Restrict to a valid, two-sided, non-crossed market and representable Q price.
inline std::int64_t weighted_midpoint_q8(Price bp, Price ap, Qty bq, Qty aq) noexcept {
    constexpr auto max_price = (std::numeric_limits<std::int64_t>::max)() / 256;
    if (bp < 0 || ap < bp || ap > max_price || bq <= 0 || aq <= 0) return -1;
    const auto base = std::uint64_t(bp) * 256;
    const auto spread = std::uint64_t(ap - bp) * 256;
    const auto total = std::uint64_t(bq) + std::uint64_t(aq);
    return static_cast<std::int64_t>(base + signal_detail::mul_div(
        spread, std::uint64_t(bq), total));
}

template <typename Book, typename Ring>
class SignalPublisher {
public:
    SignalPublisher(const Book& book, Ring& ring) noexcept : book_(book), ring_(ring) {}

    // Call after each book event. An unchanged top also retries a pending update.
    LOB_FORCE_INLINE void on_message_applied(std::uint64_t origin_tsc) noexcept {
        const Price bp = book_.best_bid(), ap = book_.best_ask();
        const Qty bq = bp >= 0 ? book_.level_qty(bp) : 0;
        const Qty aq = ap >= 0 ? book_.level_qty(ap) : 0;
        if (bp == last_.bid_px && ap == last_.ask_px &&
            bq == last_.bid_qty && aq == last_.ask_qty) {
            if (pending_) flush_pending();
            return;
        }
        const auto bid = signal_detail::bid_flow(bp, bq, last_.bid_px, last_.bid_qty);
        const auto ask = signal_detail::ask_flow(ap, aq, last_.ask_px, last_.ask_qty);
        const auto delta = signal_detail::saturated_add(bid, ask, saturations_);
        ofi_ = signal_detail::saturated_add(ofi_, delta, saturations_);
        last_ = {bp, ap, bq, aq, ofi_, weighted_midpoint_q8(bp, ap, bq, aq),
                 ++seq_, origin_tsc};
        pending_ = true;
        flush_pending();
    }

    // Producer-thread polling allows the latest snapshot to reach a recovering
    // subscriber even if the feed goes quiet after a full-ring drop.
    bool flush_pending() noexcept {
        if (!pending_) return true;
        if (!ring_.try_push(last_)) { ++dropped_; return false; }
        pending_ = false;
        ++published_;
        return true;
    }
    std::int64_t ofi() const noexcept { return ofi_; }
    std::uint64_t published() const noexcept { return published_; }
    std::uint64_t dropped() const noexcept { return dropped_; } // failed attempts
    std::uint64_t saturations() const noexcept { return saturations_; }
    bool pending() const noexcept { return pending_; }

private:
    const Book& book_;
    Ring& ring_;
    SignalUpdate last_{-1, -1, 0, 0, 0, -1, 0, 0};
    std::int64_t ofi_ = 0;
    std::uint64_t seq_ = 0, published_ = 0, dropped_ = 0, saturations_ = 0;
    bool pending_ = false;
};

} // namespace lob
