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
//
// * ICEBERG (RESERVE) ORDERS: `hidden_qty` / `display_qty` extend the same
//   64-byte object rather than adding a second order type. `qty` remains
//   the VISIBLE quantity -- the one the matching engine sweeps -- so the
//   hot fill loop in order_book.hpp needs no change for the common (non-
//   iceberg) case. `hidden_qty == 0` IS "not an iceberg order": no extra
//   flag bit is needed, one branch on an already-loaded field. When the
//   visible clip exhausts and hidden_qty > 0, the engine reloads `qty`
//   from the reserve (capped at `display_qty`, the peak/clip size) and
//   moves the order to the back of its price level's intrusive FIFO --
//   exchange-standard iceberg semantics: a reload forfeits time priority
//   to every order currently resting at that level, including ones not
//   yet touched by the sweep in progress (order_book.hpp: sweep_level()).
// ============================================================================

#include "common.hpp"

namespace lob {

struct alignas(CACHE_LINE) Order {
    // --- hot fields, ordered by access frequency during matching ---
    Qty      qty;          //  8B  VISIBLE remaining quantity (swept in matching)
    Order*   next;         //  8B  intrusive link: next order at this price (FIFO)
    Order*   prev;         //  8B  intrusive link: previous order at this price
    Price    price;        //  8B  limit price in ticks
    OrderId  id;           //  8B  engine id: generation (hi 32) | slot+1 (lo 32)
    Side     side;         //  1B
    Qty      hidden_qty;   //  8B  ICEBERG reserve behind the visible clip (0 = plain order)
    Qty      display_qty;  //  8B  ICEBERG peak/clip size restored from hidden_qty on reload
    // compiler pads the remainder to fill the 64B line -- alignas(64)
    // mandates sizeof(Order) be a multiple of the alignment.
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
        hidden_qty = 0; display_qty = 0;
    }
};

static_assert(sizeof(Order) == CACHE_LINE, "Order must be exactly one cache line");
static_assert(alignof(Order) == CACHE_LINE, "Order must be cache-line aligned");

} // namespace lob
