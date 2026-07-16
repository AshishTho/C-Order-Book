#pragma once
// ============================================================================
// bbo.hpp -- Outbound market data: Best Bid & Offer publication.
//
// PHASE 3, COMPONENT 1: the outbound leg of the trading loop.
//
//     feed -> [engine core: match]  --BboUpdate-->  SPSC ring  -->  strategy
//
// DESIGN
// ------
// * DIFF-AND-PUBLISH, NOT EVENT-DRIVEN. The engine is single-threaded and
//   owns the book exclusively, so the cheapest correct way to detect a
//   top-of-book change is to snapshot (bid px/qty, ask px/qty) AFTER each
//   applied inbound message and compare against the last published state.
//   All four reads are O(1) (cached best indices + one flat-array load);
//   the common case -- top unchanged -- costs four loads and four compares,
//   then falls through. No instrumentation hooks inside the matching code,
//   no extra branches on the sweep/rest/cancel paths themselves.
//
// * ONE UPDATE PER INBOUND MESSAGE, MAXIMUM. A single order can move the
//   book through several intermediate states (sweep 3 levels, then rest);
//   subscribers only ever care about the FINAL state. Diffing after apply()
//   gives natural conflation of intra-message churn for free.
//
// * DROP-ON-FULL (CONFLATION) POLICY. Market data is state, not commands:
//   if the consumer stalls, shipping it a backlog of stale BBOs is worse
//   than useless -- it delays the CURRENT truth. If the ring is full we
//   drop the update and count it; the next successful publish carries the
//   latest state anyway (each update is a full snapshot, not a delta).
//   The seq field lets the consumer DETECT the gap. Crucially this means
//   the ENGINE NEVER BLOCKS ON A SLOW SUBSCRIBER.
//
// * WIRE FORMAT: one cache line exactly. alignas(64) => every update owns
//   its line inside the ring's slot array, so the producer writing update
//   N+1 never invalidates the line the consumer is reading update N from.
//
// * ORIGIN TIMESTAMP: the RDTSC stamp of the inbound packet that CAUSED
//   this update travels inside it. That is what makes true tick-to-trade
//   measurement possible downstream: (strategy's order applied) - origin.
//
// ZERO HEAP: everything here is POD + references. Nothing allocates.
// ============================================================================

#include "common.hpp"

namespace lob {

// ---------------------------------------------------------------------------
// BboUpdate -- full top-of-book snapshot, exactly one cache line.
// px == -1 encodes "side empty" (matches OrderBook::best_bid/ask sentinels).
// ---------------------------------------------------------------------------
struct alignas(CACHE_LINE) BboUpdate {
    Price         bid_px;       // best bid, ticks (-1 => no bids)
    Price         ask_px;       // best ask, ticks (-1 => no asks)
    Qty           bid_qty;      // aggregate resting lots at best bid
    Qty           ask_qty;      // aggregate resting lots at best ask
    std::uint64_t seq;          // publisher sequence -- gaps => conflation
    std::uint64_t origin_tsc;   // RDTSC at inbound-packet pipeline entry
};
static_assert(sizeof(BboUpdate) == CACHE_LINE);

// ---------------------------------------------------------------------------
// BboPublisher -- owns the last-published snapshot, diffs, and pushes.
// Lives on the ENGINE thread; `Ring` is any SpscRing<BboUpdate, N>.
// ---------------------------------------------------------------------------
template <typename Book, typename Ring>
class BboPublisher {
public:
    BboPublisher(const Book& book, Ring& ring) noexcept
        : book_(book), ring_(ring) {}

    // Call once after EVERY applied inbound message. Publishes iff the
    // top of book actually changed. `origin_tsc` = RDTSC stamped when the
    // causing packet entered the pipeline (flows through to the strategy).
    LOB_FORCE_INLINE void on_message_applied(std::uint64_t origin_tsc) noexcept {
        const Price bp = book_.best_bid();
        const Price ap = book_.best_ask();
        const Qty   bq = bp >= 0 ? book_.level_qty(bp) : 0;
        const Qty   aq = ap >= 0 ? book_.level_qty(ap) : 0;

        // HOT COMMON CASE: top untouched (deep-book add/cancel). Four
        // compares, no stores, fall through.
        if (bp == last_.bid_px && ap == last_.ask_px &&
            bq == last_.bid_qty && aq == last_.ask_qty)
            return;

        last_.bid_px  = bp;  last_.ask_px  = ap;
        last_.bid_qty = bq;  last_.ask_qty = aq;
        last_.seq     = ++seq_;
        last_.origin_tsc = origin_tsc;

        // Drop-on-full = conflate. Never spin: the engine's latency budget
        // outranks a lagging subscriber's completeness.
        if (LOB_UNLIKELY(!ring_.try_push(last_))) ++dropped_;
        else                                      ++published_;
    }

    std::uint64_t published() const noexcept { return published_; }
    std::uint64_t dropped()   const noexcept { return dropped_;   }

private:
    const Book&   book_;
    Ring&         ring_;
    BboUpdate     last_{ -1, -1, 0, 0, 0, 0 };   // nothing published yet
    std::uint64_t seq_ = 0, published_ = 0, dropped_ = 0;
};

} // namespace lob
