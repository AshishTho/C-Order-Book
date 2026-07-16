#pragma once
// ============================================================================
// risk.hpp -- Pre-trade risk gateway (PHASE 4: hardening).
//
// WHAT / WHY
// ----------
// The mandatory (SEC 15c3-5 "market access rule") checkpoint between a
// strategy and the exchange-facing gateway. EVERY outbound order passes
// through check() before it may enter the order ring:
//
//     strategy decide -> RiskGateway::check() -> SPSC order ring -> engine
//
// A risk gateway that adds 100ns wipes out most of an 841ns tick-to-trade
// budget's edge, so the whole design is organised around ONE goal: the
// ACCEPT path (>99.99% of traffic) must cost a handful of nanoseconds.
//
// HOW IT STAYS UNDER 20ns
// -----------------------
// * SINGLE-THREADED BY DESIGN. The gateway lives ON the strategy thread,
//   in front of the ring. One owner => plain loads/stores, no atomics, no
//   contention, no coherence traffic. (Each strategy thread gets its own
//   gateway; firm-wide aggregation belongs on a slow-path supervisor that
//   samples per-thread counters -- never on the order path.)
//
// * BRANCH-FREE VERDICT ACCUMULATION. Every check computes a 0/1 condition
//   with pure integer ops and ORs a reason bit into a mask -- no chain of
//   if/return. There is exactly ONE conditional branch in check(), the
//   final "mask == 0", and it is virtually always ACCEPT: the predictor
//   pins it and rejects (the rare mispredict) are the case where an extra
//   ~15ns is completely acceptable. A rejected order also gets the FULL
//   reason mask (all failed checks, not just the first) for the drop-copy
//   log -- for free.
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
};

// ---------------------------------------------------------------------------
// RiskGateway< W > : W = rate-limit window depth (messages), power of two.
// Default 256 msgs / window => the stamp ring is 2KB (32 cache lines) and
// stays resident in L1 under any realistic quoting load.
// ---------------------------------------------------------------------------
template <std::size_t RateWindowMsgs = 256>
class RiskGateway {
    static_assert((RateWindowMsgs & (RateWindowMsgs - 1)) == 0,
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
    // or the OR of every violated check (reject, state untouched).
    // `now` = caller's RDTSC read (the strategy already has one in hand for
    // its tick-to-trade stamp -- reuse it, don't pay for another).
    // ========================================================================
    LOB_FORCE_INLINE std::uint32_t check(Side side, Price price, Qty qty,
                                         std::uint64_t now) noexcept {
        // ---- fat finger + collar: pure compares, cast to 0/1, OR'd in ----
        std::uint32_t rj = 0;
        rj |= RJ_QTY        * std::uint32_t((qty <= 0) | (qty > max_qty_));
        rj |= RJ_PRICE_BAND * std::uint32_t((price < px_lo_) | (price > px_hi_));

        // ---- worst-case exposure if this order rests and fills ----
        // is_buy as an integer steers the qty to one side with multiplies
        // instead of a data-dependent branch the predictor can't learn.
        const Qty is_buy = Qty(side == Side::Buy);
        const Qty pb = open_buy_  + qty * is_buy;         // prospective buys
        const Qty ps = open_sell_ + qty * (Qty(1) - is_buy);
        rj |= RJ_POSITION * std::uint32_t((pb + net_pos_  >  pos_limit_) |
                                          (ps - net_pos_  >  pos_limit_));

        // ---- exact sliding window: W-th previous accept still inside? ----
        rj |= RJ_RATE * std::uint32_t(now - stamps_[n_sent_ & MASK]
                                          < window_ticks_);

        // ---- the ONE branch: overwhelmingly ACCEPT, statically predicted ----
        if (LOB_LIKELY(rj == 0)) {
            open_buy_  = pb;
            open_sell_ = ps;
            stamps_[n_sent_++ & MASK] = now;
        } else {
            ++rejected_;
        }
        return rj;
    }

    // ------------------------------------------------------------------
    // HOT PATH -- exposure bookkeeping (cancel-and-replace / exec reports).
    // ------------------------------------------------------------------
    // A resting order (or remainder) was pulled: open exposure shrinks.
    LOB_FORCE_INLINE void on_cancel(Side side, Qty qty) noexcept {
        const Qty is_buy = Qty(side == Side::Buy);
        open_buy_  -= qty * is_buy;
        open_sell_ -= qty * (Qty(1) - is_buy);
    }
    // A resting order filled: open exposure converts to realised position.
    LOB_FORCE_INLINE void on_fill(Side side, Qty qty) noexcept {
        const Qty is_buy = Qty(side == Side::Buy);
        open_buy_  -= qty * is_buy;
        open_sell_ -= qty * (Qty(1) - is_buy);
        net_pos_   += qty * (2 * is_buy - 1);      // +buys, -sells
    }

    // ---- observers (slow path / supervision) ----
    Qty           net_position() const noexcept { return net_pos_; }
    Qty           open_buy()     const noexcept { return open_buy_; }
    Qty           open_sell()    const noexcept { return open_sell_; }
    std::uint64_t accepted()     const noexcept { return n_sent_; }
    std::uint64_t rejected()     const noexcept { return rejected_; }

private:
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

    // --- rate window: last W accept stamps, AND-indexed ring ---
    alignas(CACHE_LINE) std::uint64_t stamps_[RateWindowMsgs];
};

} // namespace lob
