#pragma once
// ============================================================================
// order.hpp -- The Order object: one cache line, intrusively linked.
//
// DESIGN NOTES
// ------------
// * INTRUSIVE DOUBLY-LINKED LIST: `next`/`prev` live INSIDE the Order.
//   A std::list<Order*> would require a separate heap node per element
//   (one extra allocation + one extra pointer dereference + one extra cache
//   miss per hop). By embedding the links, walking the FIFO queue at a price
//   level touches exactly one cache line per order.
//
// * sizeof(Order) == 64 and alignas(64): every order occupies EXACTLY one
//   cache line inside the arena. Two consequences:
//     1. No order ever straddles two lines (a straddle = 2 misses per touch).
//     2. Neighbouring orders never false-share.
//
// * The struct is trivially destructible, so pool release is literally just
//   a free-list push -- no destructor call is emitted.
// ============================================================================

#include "common.hpp"

namespace lob {

struct alignas(CACHE_LINE) Order {
    // --- hot fields, ordered by access frequency during matching ---
    Qty      qty;        //  8B  remaining open quantity (decremented on fills)
    Order*   next;       //  8B  intrusive link: next order at this price (FIFO)
    Order*   prev;       //  8B  intrusive link: previous order at this price
    Price    price;      //  8B  limit price in ticks
    OrderId  id;         //  8B  engine id: generation (hi 32) | slot+1 (lo 32)
    Side     side;       //  1B
    // 23 bytes of tail padding brings us to exactly 64.
    //
    // NOTE: liveness is deliberately NOT stored here. Once an order is
    // released to the arena its bytes are dead storage; reading them to
    // decide "is this id still valid?" would be undefined behaviour that
    // optimisers legally exploit. The book's generation array (indexed by
    // slot, bumped on every rest AND every removal) is the single source
    // of truth for liveness -- see OrderBook::cancel().

    LOB_FORCE_INLINE void init(OrderId id_, Side s, Price p, Qty q) noexcept {
        qty = q; next = nullptr; prev = nullptr;
        price = p; id = id_; side = s;
    }
};

static_assert(sizeof(Order) == CACHE_LINE, "Order must be exactly one cache line");
static_assert(alignof(Order) == CACHE_LINE, "Order must be cache-line aligned");

} // namespace lob
