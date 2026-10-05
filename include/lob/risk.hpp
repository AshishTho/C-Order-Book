#pragma once
// ============================================================================
// risk.hpp -- Pre-trade risk gateway (PHASE 4: hardening).
//
// WHAT / WHY
// ----------
// A simulation of a checkpoint between a strategy and an exchange-facing
// gateway. It is not a complete regulatory market-access control system.
// Every outbound order passes through check() before entering the order ring:
//
//     strategy decide -> RiskGateway::check() -> SPSC order ring -> engine
//
// All checks and report updates are bounded O(1). Measure their cost on the
// target machine; historical benchmark numbers are not latency guarantees.
//
// DESIGN
// -----------------------
// * SINGLE-THREADED BY DESIGN. The gateway lives ON the strategy thread,
//   in front of the ring. One owner => plain loads/stores, no atomics, no
//   contention, no coherence traffic. (Each strategy thread gets its own
//   gateway; firm-wide aggregation belongs on a slow-path supervisor that
//   samples per-thread counters -- never on the order path.)
//
// * VERDICT ACCUMULATION reports every violated check in one reason mask.
//   Exposure arithmetic uses unsigned intermediates so malformed quantities
//   cannot overflow signed counters before their rejection is recorded.
//
// * CACHE-LOCAL STATE, SPLIT BY MUTABILITY. Line 1: read-only limits
//   (loaded once, then always L1-hot, never invalidated because nothing
//   writes it). Line 2: the mutable counters (open exposure, sent-count).
//   alignas(64) on both keeps them off anyone else's lines (false-sharing
//   proof even if adjacent objects are hot). The rate-limiter ring is a
//   separate power-of-two array indexed by bitwise AND.
//
// * EXACT SLIDING-WINDOW RATE LIMIT IN O(1). Token buckets need refill
//   arithmetic; naive sliding windows scan. Instead: a circular buffer of
//   the last W accept-timestamps. Invariant: if the accept W messages ago
//   happened less than `window` ago, admitting this one would make W+1
//   messages inside the window => reject. One indexed load, one subtract,
//   one compare -- and it is EXACT, not an approximation.
//
// POSITION MODEL
// --------------
// check() gates on WORST-CASE exposure: open (resting) buy and sell lots
// are tracked separately, and an order is rejected if its side's open
// total would exceed the limit -- i.e. "if everything resting on this side
// filled, would we be over?" Cancel-and-replace strategies call
// on_cancel() when pulling a quote; fills reported back (drop copy / exec
// report) land in on_fill(), which converts open exposure into realised
// net position, itself capped by the same limit. All O(1), all on the one
// mutable line.
//
// ZERO HEAP: the gateway is a flat POD-ish object -- embed it by value.
// ============================================================================

#include "common.hpp"
#include <limits>

namespace lob {

// ---------------------------------------------------------------------------
// Reject reasons -- a bitmask so one pass reports every violated check.
// ---------------------------------------------------------------------------
enum RiskReject : std::uint32_t {
    RJ_NONE       = 0,
    RJ_QTY        = 1u << 0,   // fat finger: size <= 0 or > per-order max
    RJ_PRICE_BAND = 1u << 1,   // price collar: outside [px_lo, px_hi]
    RJ_POSITION   = 1u << 2,   // worst-case open exposure would breach limit
    RJ_RATE       = 1u << 3,   // > W accepted messages inside the window
    RJ_SIDE       = 1u << 4,   // side is neither Buy nor Sell
    RJ_CLOCK      = 1u << 5,   // timestamp predates the last accepted order
    RJ_CONFIG     = 1u << 6,   // invalid quantity, position or collar limits
};

// ---------------------------------------------------------------------------
// RiskGateway< W > : W = rate-limit window depth (messages), power of two.
// Default 256 msgs / window => the stamp ring is 2KB (32 cache lines) and
// stays resident in L1 under any realistic quoting load.
// ---------------------------------------------------------------------------
template <std::size_t RateWindowMsgs = 256>
class RiskGateway {
    static_assert(RateWindowMsgs > 0 && (RateWindowMsgs & (RateWindowMsgs - 1)) == 0,
                  "window depth must be a power of two");
    static constexpr std::uint64_t MASK = RateWindowMsgs - 1;

public:
    // COLD PATH. `window_ticks` = rate window in TSC ticks (e.g. 100us).
    RiskGateway(Qty max_order_qty, Price px_lo, Price px_hi,
                Qty position_limit, std::uint64_t window_ticks) noexcept
        : max_qty_(max_order_qty), px_lo_(px_lo), px_hi_(px_hi),
          pos_limit_(position_limit), window_ticks_(window_ticks) {
        for (auto& s : stamps_) s = 0;   // pre-touch: no faults on hot path
    }

    // ========================================================================
    // HOT PATH -- gate one order. Returns RJ_NONE (accept, state updated)
    // or the OR of every violated check (reject, only rejected_ changes).
    // now must be monotonic; timestamp zero is valid. A zero window disables
    // rate throttling while preserving timestamp validation.
    // `now` = caller's RDTSC read (the strategy already has one in hand for
    // its tick-to-trade stamp -- reuse it, don't pay for another).
    // ========================================================================
    LOB_FORCE_INLINE std::uint32_t check(Side side, Price price, Qty qty,
                                         std::uint64_t now) noexcept {
        // ---- fat finger + collar: pure compares, cast to 0/1, OR'd in ----
        std::uint32_t rj = 0;
        rj |= RJ_QTY        * std::uint32_t((qty <= 0) | (qty > max_qty_));
        rj |= RJ_PRICE_BAND * std::uint32_t((price < px_lo_) | (price > px_hi_));
        rj |= RJ_SIDE       * std::uint32_t(!valid_side(side));
        rj |= RJ_CONFIG     * std::uint32_t(max_qty_ <= 0 || pos_limit_ < 0 ||
                                           px_lo_ > px_hi_);

        // ---- worst-case exposure if this order rests and fills ----
        // Each operand is <= INT64_MAX, so these unsigned sums cannot wrap.
        // Modular subtraction expresses limit +/- signed net position even
        // when the valid headroom exceeds INT64_MAX.
        const auto positive_qty = static_cast<std::uint64_t>(qty > 0 ? qty : 0);
        const auto pb = static_cast<std::uint64_t>(open_buy_) +
                        positive_qty * std::uint64_t(side == Side::Buy);
        const auto ps = static_cast<std::uint64_t>(open_sell_) +
                        positive_qty * std::uint64_t(side == Side::Sell);
        const auto buy_limit = static_cast<std::uint64_t>(pos_limit_) -
                               static_cast<std::uint64_t>(net_pos_);
        const auto sell_limit = static_cast<std::uint64_t>(pos_limit_) +
                                static_cast<std::uint64_t>(net_pos_);
        const auto qty_max = static_cast<std::uint64_t>(std::numeric_limits<Qty>::max());
        rj |= RJ_POSITION * std::uint32_t(pb > buy_limit || ps > sell_limit ||
                                          pb > qty_max || ps > qty_max);

        // ---- exact sliding window: W-th previous accept still inside? ----
        rj |= RJ_RATE * std::uint32_t(stamp_count_ == RateWindowMsgs &&
                                     now - stamps_[n_sent_ & MASK] < window_ticks_);
        rj |= RJ_CLOCK * std::uint32_t(stamp_count_ != 0 &&
                                      now < stamps_[(n_sent_ - 1) & MASK]);

        // ---- Commit exposure and rate state only on acceptance. ----
        if (LOB_LIKELY(rj == 0)) {
            open_buy_  = static_cast<Qty>(pb);
            open_sell_ = static_cast<Qty>(ps);
            stamps_[n_sent_++ & MASK] = now;
            if (stamp_count_ < RateWindowMsgs) ++stamp_count_;
        } else {
            ++rejected_;
        }
        return rj;
    }

    // ------------------------------------------------------------------
    // HOT PATH -- exposure bookkeeping (cancel-and-replace / exec reports).
    // ------------------------------------------------------------------
    // Invalid reports fail without changing state. The caller must correlate
    // and deduplicate reports by order ID; aggregate counters cannot do so.
    // Call on_cancel after an acknowledged cancel or an engine rejection of
    // an already-admitted order to release its reserved exposure.
    LOB_FORCE_INLINE bool on_cancel(Side side, Qty qty) noexcept {
        if (LOB_UNLIKELY(!valid_report(side, qty))) return false;
        (side == Side::Buy ? open_buy_ : open_sell_) -= qty;
        return true;
    }
    // A resting order filled: open exposure converts to realised position.
    LOB_FORCE_INLINE bool on_fill(Side side, Qty qty) noexcept {
        if (LOB_UNLIKELY(!valid_report(side, qty))) return false;
        (side == Side::Buy ? open_buy_ : open_sell_) -= qty;
        // Accepted worst-case exposure bounds this result within pos_limit_.
        if (side == Side::Buy) net_pos_ += qty; else net_pos_ -= qty;
        return true;
    }

    // ---- observers (slow path / supervision) ----
    Qty           net_position() const noexcept { return net_pos_; }
    Qty           open_buy()     const noexcept { return open_buy_; }
    Qty           open_sell()    const noexcept { return open_sell_; }
    std::uint64_t accepted()     const noexcept { return n_sent_; }
    std::uint64_t rejected()     const noexcept { return rejected_; }

private:
    static constexpr bool valid_side(Side side) noexcept {
        return side == Side::Buy || side == Side::Sell;
    }
    LOB_FORCE_INLINE bool valid_report(Side side, Qty qty) const noexcept {
        return valid_side(side) && qty > 0 &&
               qty <= (side == Side::Buy ? open_buy_ : open_sell_);
    }

    // --- line 1: READ-ONLY limits. Never written after construction, so
    //     this line lives in every relevant L1 in Shared state forever. ---
    alignas(CACHE_LINE) const Qty  max_qty_;
    const Price                    px_lo_, px_hi_;
    const Qty                      pos_limit_;
    const std::uint64_t            window_ticks_;

    // --- line 2: HOT mutable counters (single writer: the owning thread) ---
    alignas(CACHE_LINE) Qty        open_buy_  = 0;   // resting buy lots
    Qty                            open_sell_ = 0;   // resting sell lots
    Qty                            net_pos_   = 0;   // realised fills, signed
    std::uint64_t                  n_sent_    = 0;   // accepts (rate index)
    std::uint64_t                  rejected_  = 0;
    std::size_t                    stamp_count_ = 0; // saturates at ring depth

    // --- rate window: last W accept stamps, AND-indexed ring ---
    alignas(CACHE_LINE) std::uint64_t stamps_[RateWindowMsgs];
};

} // namespace lob
