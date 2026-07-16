#pragma once
// ============================================================================
// signals.hpp -- Quant signal generation at the source: Order Flow
//                Imbalance (OFI) + volume-weighted micro-price.
//                PHASE 7: market microstructure.
//
// WHY COMPUTE SIGNALS ON THE ENGINE CORE
// --------------------------------------
// The engine already loads best bid/ask px+qty into registers to diff the
// top of book (bbo.hpp). Deriving OFI and the micro-price RIGHT THERE
// costs a handful of ALU ops on values that are already in registers --
// doing it downstream on the strategy core would burn a ring slot AND
// re-derive state the engine just had for free. The strategy receives
// decision-ready numbers, not raw material.
//
// THE SIGNALS (both exact integer arithmetic -- no float/double anywhere)
// -----------------------------------------------------------------------
// * OFI (Cont/Kukanov/Stoikov 2014): per top-of-book transition,
//       e  =  I{Pb >= Pb'} qb  -  I{Pb <= Pb'} qb'
//          -  I{Pa <= Pa'} qa  +  I{Pa >= Pa'} qa'
//   (primes = previous update). Intuition: bid price up or bid size
//   growing = buy pressure; ask price down or ask size growing = sell
//   pressure. The published value is the RUNNING SUM in lots; the
//   strategy differences it over its own horizon (sum over any window =
//   difference of two cumulative reads -- O(1), windowing policy stays
//   out of the hot path). Empirically the strongest short-horizon price
//   predictor in the microstructure literature.
//
// * MICRO-PRICE: (Pb*qa + Pa*qb) / (qb + qa), the size-weighted mid that
//   leans toward the THINNER side (where the next trade will likely
//   move). Published in Q56.8 fixed point (ticks << 8): sub-tick
//   resolution with exact integer math. One IDIV per top change -- ~25
//   cycles, paid only on the ~3% of messages that move the top.
//
// BRANCHLESSNESS
// --------------
// The four OFI indicator terms are bool-to-int multiplies (SETcc + IMUL,
// no jumps -- price-move direction is market-random, ~50% mispredict if
// branched). The micro-price divide-by-zero guard adds (denom == 0) to
// the denominator instead of testing it. The ONLY branches in publish
// are the two the BBO publisher already had: "top unchanged" (common,
// predictable) and "ring full" (never taken).
//
// WIRE FORMAT: exactly one cache line. px/qty compress to i32/u32 --
// ITCH itself carries 32-bit prices and shares, so nothing is lost.
// ============================================================================

#include "common.hpp"

namespace lob {

// ---------------------------------------------------------------------------
// SignalUpdate -- decision-ready top-of-book + signals, one cache line.
// ---------------------------------------------------------------------------
struct alignas(CACHE_LINE) SignalUpdate {
    std::int32_t  bid_px;       // ticks (-1 => side empty). ITCH prices are
    std::int32_t  ask_px;       //   u32 on the wire; i32 loses nothing.
    std::int32_t  bid_qty;      // lots at best bid
    std::int32_t  ask_qty;      // lots at best ask
    std::int64_t  ofi;          // cumulative Order Flow Imbalance, lots
    std::int64_t  micro_px;     // micro-price, Q56.8 fixed point (ticks<<8)
    std::uint64_t seq;          // gap detection under conflation
    std::uint64_t origin_tsc;   // causing packet's pipeline-entry RDTSC
};
static_assert(sizeof(SignalUpdate) == CACHE_LINE);

// ---------------------------------------------------------------------------
// SignalPublisher -- drop-in upgrade of BboPublisher (same diff-and-publish
// contract, same conflation policy) that also emits OFI + micro-price.
// Lives on the engine thread; `Ring` is any SpscRing<SignalUpdate, N>.
// ---------------------------------------------------------------------------
template <typename Book, typename Ring>
class SignalPublisher {
public:
    SignalPublisher(const Book& book, Ring& ring) noexcept
        : book_(book), ring_(ring) {}

    LOB_FORCE_INLINE void on_message_applied(std::uint64_t origin_tsc) noexcept {
        const Price bp = book_.best_bid();
        const Price ap = book_.best_ask();
        const Qty   bq = bp >= 0 ? book_.level_qty(bp) : 0;
        const Qty   aq = ap >= 0 ? book_.level_qty(ap) : 0;

        // Hot common case: top untouched -- four compares, fall through.
        if (bp == last_bp_ && ap == last_ap_ && bq == last_bq_ && aq == last_aq_)
            return;

        // ---- OFI increment: four indicator terms, zero jumps ----
        // (On the very first update the priors are {-1,-1,0,0}: the bid
        // term contributes the full opening size -- by construction, the
        // arrival of standing liquidity IS the flow.)
        ofi_ += Qty(bp >= last_bp_) * bq  - Qty(bp <= last_bp_) * last_bq_
              - Qty(ap <= last_ap_) * aq  + Qty(ap >= last_ap_) * last_aq_;

        // ---- micro-price, Q56.8: lean toward the thin side ----
        // Branchless empty-book guard: denominator 0 becomes 1 (numerator
        // is 0 in that case, so micro publishes 0 -- strategy already
        // treats px < 0 sides as "no market").
        const Qty denom = bq + aq;
        const std::int64_t micro =
            ((bp * aq + ap * bq) << 8) / (denom + Qty(denom == 0));

        last_bp_ = bp; last_ap_ = ap; last_bq_ = bq; last_aq_ = aq;

        const SignalUpdate u {
            std::int32_t(bp), std::int32_t(ap),
            std::int32_t(bq), std::int32_t(aq),
            ofi_, micro, ++seq_, origin_tsc
        };
        if (LOB_UNLIKELY(!ring_.try_push(u))) ++dropped_;   // conflate
        else                                  ++published_;
    }

    std::int64_t  ofi()       const noexcept { return ofi_; }
    std::uint64_t published() const noexcept { return published_; }
    std::uint64_t dropped()   const noexcept { return dropped_; }

private:
    const Book&   book_;
    Ring&         ring_;
    Price         last_bp_ = -1, last_ap_ = -1;
    Qty           last_bq_ = 0,  last_aq_ = 0;
    std::int64_t  ofi_ = 0;
    std::uint64_t seq_ = 0, published_ = 0, dropped_ = 0;
};

} // namespace lob
