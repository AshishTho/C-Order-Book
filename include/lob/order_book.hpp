#pragma once
// ============================================================================
// order_book.hpp -- Price-time priority matching engine.
//
// DATA-STRUCTURE CHOICES (vs. the "academic" std::map book)
// ----------------------------------------------------------
// * PRICE LEVELS: a CONTIGUOUS FLAT ARRAY indexed by tick offset.
//     - price -> level is ONE add + ONE indexed load. O(1), no comparisons.
//     - std::map is a red-black tree: every lookup is ~log2(N) POINTER
//       CHASES to nodes scattered across the heap. Each hop is a likely
//       cache miss (~100ns from DRAM). One miss already blows the entire
//       50ns budget; a tree walk takes several.
//     - Adjacent levels are adjacent in memory, so a sweep through 2-3
//       levels stays inside lines the prefetcher has already pulled.
//
// * ORDERS AT A LEVEL: an INTRUSIVE doubly-linked FIFO. head = oldest
//   (time priority). Fills pop the head; cancels unlink in O(1) from the
//   middle with zero searching, because the Order carries its own links.
//
// * BEST-PRICE MAINTENANCE: cached best bid/ask indices + a hierarchical
//   bitmap (level_bitmap.hpp) so that "level emptied, find next best" is
//   constant-time even across a huge price gap. This is what keeps P99.9
//   bounded: no operation in this engine is O(book depth).
//
// * ID -> ORDER RESOLUTION for cancels: the order id ENCODES the arena slot
//   (low 32 bits) plus a generation counter (high 32 bits). The generation
//   array is bumped when a slot is RESTED and again when it is REMOVED, so
//   "is this id live?" is one array compare -- O(1), no hash table, and it
//   never dereferences possibly-recycled order memory (which would be UB).
//
// * The engine is single-threaded by design (LMAX philosophy): ALL mutation
//   happens on one core that owns the book exclusively, fed by the SPSC
//   ring. No locks are needed because there is no sharing.
// ============================================================================

#include "common.hpp"
#include "order.hpp"
#include "memory_pool.hpp"
#include "level_bitmap.hpp"

namespace lob {

// ---------------------------------------------------------------------------
// One price level: FIFO queue head/tail + aggregate size.
// 32 bytes => two levels per cache line. Neighbouring levels are usually
// accessed together while sweeping, so sharing a line here HELPS (this is
// true sharing on one thread, not false sharing across threads).
// ---------------------------------------------------------------------------
struct PriceLevel {
    Order* head;        // oldest order (first to fill -- time priority)
    Order* tail;        // newest order (arrivals append here)
    Qty    total_qty;   // aggregate resting size (top-of-book feeds)
    Qty    _pad;
};
static_assert(sizeof(PriceLevel) == 32);

// ---------------------------------------------------------------------------
// Inbound message (what travels through the SPSC ring). 32 bytes, POD.
// ---------------------------------------------------------------------------
// NewOrder : place a limit order (may cross).
// Cancel   : remove a resting order entirely   (ITCH 'D' Order Delete).
// Execute  : fill `qty` of a resting order      (ITCH 'E' Order Executed).
// Reduce   : cancel `qty` of a resting order    (ITCH 'X' partial cancel).
// Execute and Reduce mutate the book identically (both call reduce()); they
// are distinct types so the consumer can count trades vs. cancels correctly.
enum class MsgType : std::uint8_t {
    NewOrder = 0, Cancel = 1, Execute = 2, Reduce = 3
};

struct Message {
    Price    price;     // ticks (NewOrder only)
    Qty      qty;       // lots  (NewOrder / Execute / Reduce)
    OrderId  id;        // engine id -- or gateway order-reference (see itch)
    MsgType  type;
    Side     side;      // NewOrder only
    std::uint8_t _pad[6];
};
static_assert(sizeof(Message) == 32);

// ---------------------------------------------------------------------------
// Execution report, delivered synchronously to a user-supplied handler.
// The default handler in the benchmark just accumulates -- what matters is
// that reporting is a template call, inlined, never a virtual dispatch.
// ---------------------------------------------------------------------------
struct Trade {
    OrderId maker_id;
    OrderId taker_id;
    Price   price;
    Qty     qty;
};

// ---------------------------------------------------------------------------
// The book.
//   NumTicks : size of the price ladder (flat array length).
//   PoolCap  : max simultaneously-resting orders (arena size).
// ---------------------------------------------------------------------------
template <std::size_t NumTicks = 1u << 16, std::size_t PoolCap = 1u << 20>
class OrderBook {
public:
    static constexpr std::size_t NPOS = LevelBitmap<NumTicks>::NPOS;

    // COLD PATH -- all heap allocation for the process happens here.
    explicit OrderBook(Price base_tick)
        : pool_(PoolCap), base_(base_tick)
    {
        levels_ = static_cast<PriceLevel*>(
#if defined(_WIN32)
            _aligned_malloc(NumTicks * sizeof(PriceLevel), CACHE_LINE)
#else
            std::aligned_alloc(CACHE_LINE, NumTicks * sizeof(PriceLevel))
#endif
        );
        if (!levels_) std::abort();
        // Pre-touch: zero every level so no soft page faults occur mid-session.
        for (std::size_t i = 0; i < NumTicks; ++i)
            levels_[i] = PriceLevel{nullptr, nullptr, 0, 0};
        generations_ = new std::uint32_t[PoolCap]();   // cold-path alloc, ok
    }

    ~OrderBook() {
        delete[] generations_;
#if defined(_WIN32)
        _aligned_free(levels_);
#else
        std::free(levels_);
#endif
    }

    OrderBook(const OrderBook&)            = delete;
    OrderBook& operator=(const OrderBook&) = delete;

    // ========================================================================
    // HOT PATH -- new limit order. Returns the engine id (0 => rejected).
    // Price-time priority: cross against the far side first, rest remainder.
    // `on_trade` is any callable `void(const Trade&)`; it inlines away.
    // ========================================================================
    template <typename TradeHandler>
    LOB_FORCE_INLINE OrderId new_order(Side side, Price price, Qty qty,
                                       TradeHandler&& on_trade) noexcept {
        const std::size_t idx = static_cast<std::size_t>(price - base_);
        if (LOB_UNLIKELY(idx >= NumTicks || qty <= 0)) return 0;

        if (side == Side::Buy) {
            // ---- match against asks priced <= our limit ----
            while (LOB_LIKELY(qty > 0) && best_ask_ <= idx) {
                qty = sweep_level<Side::Sell>(best_ask_, qty, on_trade);
            }
            if (qty == 0) return TAKER_FILLED_ID;
            // ---- rest the remainder on the bid side ----
            return rest<Side::Buy>(idx, price, qty);
        } else {
            // ---- match against bids priced >= our limit ----
            while (LOB_LIKELY(qty > 0) &&
                   best_bid_ != NPOS && best_bid_ >= idx) {
                qty = sweep_level<Side::Buy>(best_bid_, qty, on_trade);
            }
            if (qty == 0) return TAKER_FILLED_ID;
            return rest<Side::Sell>(idx, price, qty);
        }
    }

    // ========================================================================
    // HOT PATH -- cancel by id. O(1): decode slot, verify generation, unlink.
    // ========================================================================
    LOB_FORCE_INLINE bool cancel(OrderId id) noexcept {
        const std::size_t slot = static_cast<std::size_t>(id & 0xFFFFFFFFu) - 1;
        if (LOB_UNLIKELY(slot >= PoolCap)) return false;
        // Liveness check against the generation array ONLY. Generations bump
        // on rest and on removal, so a stale/duplicate/forged id mismatches
        // here and we never touch (possibly recycled) order storage.
        if (LOB_UNLIKELY(generations_[slot] !=
                         static_cast<std::uint32_t>(id >> 32))) return false;
        Order* o = pool_.at(slot);
        unlink_and_release(o, slot);
        return true;
    }

    // ========================================================================
    // HOT PATH -- reduce a resting order by `q` lots (ITCH 'E' Order Executed
    // and 'X' partial cancel both map here). O(1): the common case (partial)
    // is two subtractions; full exhaustion falls through to the unlink path.
    // Returns the quantity actually removed (0 => stale/unknown id).
    // ========================================================================
    LOB_FORCE_INLINE Qty reduce(OrderId id, Qty q) noexcept {
        const std::size_t slot = static_cast<std::size_t>(id & 0xFFFFFFFFu) - 1;
        if (LOB_UNLIKELY(slot >= PoolCap)) return 0;
        if (LOB_UNLIKELY(generations_[slot] !=
                         static_cast<std::uint32_t>(id >> 32))) return 0;
        Order* o = pool_.at(slot);

        if (LOB_LIKELY(q < o->qty)) {           // partial: 2 stores, done
            o->qty -= q;
            levels_[static_cast<std::size_t>(o->price - base_)].total_qty -= q;
            return q;
        }
        const Qty removed = o->qty;             // full: remove from book
        unlink_and_release(o, slot);
        return removed;
    }

    // Message dispatcher -- what the engine thread runs per ring item.
    // (Gateways that translate exchange order-references to engine ids do
    // their own dispatch; this is the direct-id path.)
    template <typename TradeHandler>
    LOB_FORCE_INLINE void apply(const Message& m, TradeHandler&& on_trade) noexcept {
        switch (m.type) {
        case MsgType::NewOrder:
            new_order(m.side, m.price, m.qty, on_trade);  break;
        case MsgType::Cancel:
            cancel(m.id);                                 break;
        case MsgType::Execute:
        case MsgType::Reduce:
            reduce(m.id, m.qty);                          break;
        }
    }

    // ---- observers (used by tests; also O(1)) ----
    Price best_bid() const noexcept {
        return best_bid_ == NPOS ? -1 : base_ + static_cast<Price>(best_bid_);
    }
    Price best_ask() const noexcept {
        return best_ask_ >= NumTicks ? -1 : base_ + static_cast<Price>(best_ask_);
    }
    Qty level_qty(Price p) const noexcept {
        const std::size_t idx = static_cast<std::size_t>(p - base_);
        return idx < NumTicks ? levels_[idx].total_qty : 0;
    }

private:
    // Sentinel id returned when a taker fully fills without resting.
    static constexpr OrderId TAKER_FILLED_ID = ~OrderId{0};

    // ------------------------------------------------------------------
    // Remove a live order from its level, invalidate its id, and recycle
    // its arena slot. Shared by cancel() and reduce()-to-zero.
    // ------------------------------------------------------------------
    LOB_FORCE_INLINE void unlink_and_release(Order* o, std::size_t slot) noexcept {
        const std::size_t idx = static_cast<std::size_t>(o->price - base_);
        PriceLevel& lvl = levels_[idx];

        // O(1) intrusive unlink -- this is why the links live IN the order.
        if (o->prev) o->prev->next = o->next; else lvl.head = o->next;
        if (o->next) o->next->prev = o->prev; else lvl.tail = o->prev;
        lvl.total_qty -= o->qty;

        if (lvl.head == nullptr) {          // level emptied
            if (o->side == Side::Buy) {
                bids_.reset(idx);
                if (idx == best_bid_) best_bid_ = bids_.prev_set(idx);
            } else {
                asks_.reset(idx);
                if (idx == best_ask_) {
                    const std::size_t nxt = asks_.next_set(idx);
                    best_ask_ = (nxt == NPOS) ? NumTicks : nxt;
                }
            }
        }
        ++generations_[slot];       // invalidate the id before slot reuse
        pool_.release(o);
    }

    // ------------------------------------------------------------------
    // Fill `qty` against resting orders at level `idx` (side = MakerSide).
    // Returns the taker quantity still open. Pops fully-filled makers off
    // the FIFO head and recycles them into the arena.
    // ------------------------------------------------------------------
    template <Side MakerSide, typename TradeHandler>
    LOB_FORCE_INLINE Qty sweep_level(std::size_t idx, Qty qty,
                                     TradeHandler&& on_trade) noexcept {
        PriceLevel& lvl = levels_[idx];
        Order* o = lvl.head;
        const Price px = base_ + static_cast<Price>(idx);

        while (o != nullptr && qty > 0) {
            const Qty fill = o->qty < qty ? o->qty : qty;   // branchless CMOV
            qty          -= fill;
            o->qty       -= fill;
            lvl.total_qty -= fill;
            on_trade(Trade{o->id, 0, px, fill});

            if (o->qty == 0) {                 // maker exhausted: pop head
                Order* nxt = o->next;
                ++generations_[pool_.index_of(o)];   // invalidate maker's id
                pool_.release(o);
                o = nxt;
            }
            // else: maker partially filled => taker must be done (qty==0)
        }

        lvl.head = o;
        if (o != nullptr) {
            o->prev = nullptr;
        } else {
            // Level swept clean: clear bitmap, hop to next best in O(1).
            lvl.tail = nullptr;
            if constexpr (MakerSide == Side::Sell) {
                asks_.reset(idx);
                const std::size_t nxt = asks_.next_set(idx);
                best_ask_ = (nxt == NPOS) ? NumTicks : nxt;
            } else {
                bids_.reset(idx);
                best_bid_ = bids_.prev_set(idx);
            }
        }
        return qty;
    }

    // ------------------------------------------------------------------
    // Rest a passive order at level `idx`. Placement-new from the arena,
    // append to the intrusive FIFO tail, maintain bitmap + cached best.
    // ------------------------------------------------------------------
    template <Side S>
    LOB_FORCE_INLINE OrderId rest(std::size_t idx, Price price, Qty qty) noexcept {
        Order* o = pool_.acquire();            // placement-new inside
        if (LOB_UNLIKELY(o == nullptr)) return 0;   // arena exhausted: reject

        const std::size_t slot = pool_.index_of(o);
        // id = generation (high 32) | slot+1 (low 32). Bumping the generation
        // on every reuse makes stale ids detectable in cancel().
        const OrderId id =
            (static_cast<OrderId>(++generations_[slot]) << 32) |
            static_cast<OrderId>(slot + 1);
        o->init(id, S, price, qty);

        PriceLevel& lvl = levels_[idx];
        o->prev = lvl.tail;                    // append at tail (time priority)
        if (lvl.tail) lvl.tail->next = o; else lvl.head = o;
        lvl.tail = o;
        lvl.total_qty += qty;

        if constexpr (S == Side::Buy) {
            bids_.set(idx);
            if (best_bid_ == NPOS || idx > best_bid_) best_bid_ = idx;
        } else {
            asks_.set(idx);
            if (idx < best_ask_) best_ask_ = idx;
        }
        return id;
    }

    // ---- engine state (single-threaded owner => no atomics needed here) ----
    MemoryPool<Order>     pool_;                  // pre-allocated order arena
    PriceLevel*           levels_;                // flat ladder, 1 slot per tick
    LevelBitmap<NumTicks> bids_;                  // occupancy index, bid side
    LevelBitmap<NumTicks> asks_;                  // occupancy index, ask side
    std::uint32_t*        generations_;           // slot reuse counters
    Price                 base_;                  // tick of ladder index 0
    std::size_t           best_bid_ = NPOS;       // cached best-bid index
    std::size_t           best_ask_ = NumTicks;   // cached best-ask index
};

} // namespace lob
