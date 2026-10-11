// ============================================================================
// test_properties.cpp -- Deterministic property-style tests.
//
// test_correctness.cpp pins hand-written scenarios. This file complements it
// with randomised-but-REPRODUCIBLE checks: every sequence comes from a
// fixed-seed PRNG (splitmix64, so the stream is identical on every compiler,
// standard library and OS), and a failure aborts with file:line like the
// other suite. Only the public API in include/lob is exercised.
//
//   (a) price-time priority: random add / cancel / reduce / execute / post-
//       only sequences are mirrored in an independent reference model; the
//       book must agree on every trade (maker, price, qty, FIFO order), on
//       best bid/ask (always uncrossed), and on every level's total quantity.
//   (b) journal replay determinism: a seeded ITCH session is journaled, then
//       replayed twice from the journal file into fresh books; trade stream,
//       counters and a full-ladder fingerprint must be identical every time.
//   (c) ITCH round trip: every supported message type is encoded byte-by-byte
//       (independently of the library), parsed, and compared field by field,
//       plus the framing edge cases.
//   (d) SPSC ring boundaries: empty/full, wrap-around, minimum capacity, and
//       a long random interleave against a std::deque oracle.
// ============================================================================

#include "lob/arena_ring.hpp"
#include "lob/flat_hash.hpp"
#include "lob/itch.hpp"
#include "lob/journal.hpp"
#include "lob/order_book.hpp"
#include "lob/spsc_ring.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <vector>

static std::uint64_t g_checks = 0;   // number of CHECK evaluations (reported)

#define CHECK(cond)                                                          \
    do {                                                                     \
        ++g_checks;                                                          \
        if (!(cond)) {                                                       \
            std::fprintf(stderr, "FAILED: %s  (%s:%d)\n", #cond, __FILE__,   \
                         __LINE__);                                          \
            std::abort();                                                    \
        }                                                                    \
    } while (0)

using namespace lob;

// ---------------------------------------------------------------------------
// splitmix64: tiny, fully specified, identical everywhere.
// ---------------------------------------------------------------------------
struct Rng {
    std::uint64_t s;
    explicit Rng(std::uint64_t seed) : s(seed) {}
    std::uint64_t next() noexcept {
        std::uint64_t z = (s += 0x9E3779B97F4A7C15ull);
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
        return z ^ (z >> 31);
    }
    std::uint64_t below(std::uint64_t n) noexcept { return next() % n; }
    std::int64_t  range(std::int64_t lo, std::int64_t hi) noexcept {   // inclusive
        return lo + static_cast<std::int64_t>(below(std::uint64_t(hi - lo + 1)));
    }
};

// Small book so tests reach the edges quickly: 1024-tick ladder, 4096 orders.
using Book = OrderBook<1u << 10, 1u << 12>;
static constexpr std::size_t NUM_TICKS = 1u << 10;
static constexpr Price       BASE      = 100'000;
static constexpr Price       MID       = BASE + 512;

// ===========================================================================
// (a) PRICE-TIME PRIORITY vs. AN INDEPENDENT REFERENCE MODEL
// ===========================================================================
namespace {

struct Resting { OrderId id; Qty qty; };
struct Expect  { OrderId maker; Price px; Qty qty; };
struct Loc     { Side side; Price px; std::size_t pos; };

// Straightforward, obviously-correct book: ordered maps of FIFO deques.
// Knows nothing about bitmaps, arenas, or generations.
class RefBook {
public:
    using Level = std::deque<Resting>;
    std::map<Price, Level, std::greater<Price>> bids;   // best (highest) first
    std::map<Price, Level>                      asks;   // best (lowest) first
    std::map<OrderId, Loc>                      where;  // live id -> location
    std::vector<OrderId>                        ids;    // live ids, dense
    std::vector<OrderId>                        dead;   // recently removed ids

    bool would_cross(Side s, Price px) const {
        return s == Side::Buy ? (!asks.empty() && asks.begin()->first <= px)
                              : (!bids.empty() && bids.begin()->first >= px);
    }

    // Match an incoming order; returns the unfilled remainder.
    Qty match(Side s, Price px, Qty qty, std::vector<Expect>& out) {
        return s == Side::Buy ? sweep(asks, px, true, qty, out)
                              : sweep(bids, px, false, qty, out);
    }

    void rest(Side s, Price px, Qty qty, OrderId id) {
        Level& lvl = (s == Side::Buy) ? bids[px] : asks[px];
        lvl.push_back({id, qty});
        where[id] = {s, px, ids.size()};
        ids.push_back(id);
    }

    // Reduce by q (capped at the remaining qty); removes the order if it hits
    // zero. Returns the quantity actually removed.
    Qty reduce(OrderId id, Qty q) {
        const Loc loc = where.at(id);
        Resting& r = entry(loc, id);
        const Qty removed = q < r.qty ? q : r.qty;
        if (q >= r.qty) remove(id); else r.qty -= q;
        return removed;
    }

    void remove(OrderId id) {
        const Loc loc = where.at(id);
        if (loc.side == Side::Buy) erase_from(bids, loc.px, id);
        else                       erase_from(asks, loc.px, id);
        forget(id);
    }

    Qty qty_of(OrderId id) { return entry(where.at(id), id).qty; }

    // Sum of resting order quantities at one price, straight from the orders.
    Qty level_sum(Price px) const {
        Qty sum = 0;
        if (auto it = bids.find(px); it != bids.end())
            for (const Resting& r : it->second) sum += r.qty;
        if (auto it = asks.find(px); it != asks.end())
            for (const Resting& r : it->second) sum += r.qty;
        return sum;
    }

    Price best_bid() const { return bids.empty() ? -1 : bids.begin()->first; }
    Price best_ask() const { return asks.empty() ? -1 : asks.begin()->first; }

private:
    Resting& entry(const Loc& loc, OrderId id) {
        Level& lvl = (loc.side == Side::Buy) ? bids.at(loc.px) : asks.at(loc.px);
        for (Resting& r : lvl) if (r.id == id) return r;
        std::abort();   // model corruption: id mapped but not in its level
    }

    template <class M>
    void erase_from(M& m, Price px, OrderId id) {
        auto it = m.find(px);
        Level& lvl = it->second;
        for (auto e = lvl.begin(); e != lvl.end(); ++e)
            if (e->id == id) { lvl.erase(e); break; }
        if (lvl.empty()) m.erase(it);
    }

    void forget(OrderId id) {
        const std::size_t pos = where.at(id).pos;
        ids[pos] = ids.back();
        where.at(ids[pos]).pos = pos;
        ids.pop_back();
        where.erase(id);
        dead.push_back(id);
        if (dead.size() > 256) dead.erase(dead.begin(), dead.begin() + 128);
    }

    template <class M>
    Qty sweep(M& m, Price limit, bool buy, Qty qty, std::vector<Expect>& out) {
        while (qty > 0 && !m.empty()) {
            auto it = m.begin();
            if (buy ? it->first > limit : it->first < limit) break;
            Level& lvl = it->second;
            while (qty > 0 && !lvl.empty()) {
                Resting& r = lvl.front();
                const Qty fill = r.qty < qty ? r.qty : qty;
                out.push_back({r.id, it->first, fill});
                r.qty -= fill;
                qty   -= fill;
                if (r.qty == 0) {
                    const OrderId id = r.id;
                    lvl.pop_front();
                    forget(id);
                }
            }
            if (lvl.empty()) m.erase(it);
        }
        return qty;
    }
};

struct Stats {
    std::uint64_t ops = 0, adds = 0, rested = 0, crossing = 0, trades = 0,
                  lots = 0, post_only_rejects = 0, invalid_rejects = 0,
                  cancels = 0, partial_reduces = 0, full_reduces = 0,
                  stale_rejects = 0, drained_orders = 0;
    void add(const Stats& o) {
        ops += o.ops; adds += o.adds; rested += o.rested; crossing += o.crossing;
        trades += o.trades; lots += o.lots;
        post_only_rejects += o.post_only_rejects;
        invalid_rejects += o.invalid_rejects; cancels += o.cancels;
        partial_reduces += o.partial_reduces; full_reduces += o.full_reduces;
        stale_rejects += o.stale_rejects; drained_orders += o.drained_orders;
    }
};

class Harness {
public:
    explicit Harness(std::uint64_t seed) : rng_(seed), book_(BASE) {}

    Stats run(int n_ops) {
        for (int i = 0; i < n_ops; ++i) {
            step();
            ++st_.ops;
            verify_fast();
            if ((i & 127) == 127) verify_full();
        }
        verify_full();
        drain();
        return st_;
    }

private:
    void touch(Price p) { touched_.push_back(p); }

    // ---- one random operation ------------------------------------------
    void step() {
        const std::uint64_t r = rng_.below(100);
        const bool crowded = ref_.ids.size() > 2500;       // stay under PoolCap
        if (ref_.ids.empty() || (!crowded && r < 50)) {
            random_add(false);
        } else if (crowded || r < 62) {
            cancel_live(false);
        } else if (r < 70) {
            reduce_live(false, MsgType::Reduce);           // direct reduce()
        } else if (r < 78) {
            reduce_live(true, MsgType::Execute);           // via apply()
        } else if (r < 84) {
            reduce_live(true, MsgType::Reduce);
        } else if (r < 88) {
            cancel_live(true);                             // via apply()
        } else if (r < 92) {
            stale_op();
        } else if (r < 94) {
            invalid_add();
        } else {
            random_add(true);                              // big sweeping order
        }
    }

    void random_add(bool large) {
        const Side side = rng_.below(2) ? Side::Buy : Side::Sell;
        // Buys land in [MID-20, MID+4], sells in [MID-4, MID+20]: the overlap
        // makes roughly one add in ten cross the spread.
        const Price px = (side == Side::Buy) ? MID - 20 + Price(rng_.below(25))
                                             : MID + 20 - Price(rng_.below(25));
        const Qty qty = large ? Qty(rng_.range(60, 400)) : Qty(rng_.range(1, 50));
        add(side, px, qty, rng_.below(10) == 0);
    }

    // Place an order, then require the book to agree with the model on every
    // trade (maker identity, level price, size, FIFO order) and on the id.
    void add(Side side, Price px, Qty qty, bool post_only) {
        got_.clear();
        std::vector<Expect> exp;
        const bool crosses = ref_.would_cross(side, px);
        ++st_.adds;
        const OrderId id = book_.new_order(
            side, px, qty, [&](const Trade& t) { got_.push_back(t); }, post_only);

        if (post_only && crosses) {                        // maker-only reject
            CHECK(id == 0);
            CHECK(got_.empty());
            ++st_.post_only_rejects;
            return;
        }
        if (crosses) ++st_.crossing;
        const Qty rem = ref_.match(side, px, qty, exp);

        CHECK(got_.size() == exp.size());
        for (std::size_t i = 0; i < exp.size(); ++i) {
            CHECK(got_[i].maker_id == exp[i].maker);
            CHECK(got_[i].price    == exp[i].px);
            CHECK(got_[i].qty      == exp[i].qty);
            touch(exp[i].px);
            ++st_.trades;
            st_.lots += std::uint64_t(exp[i].qty);
        }
        CHECK(id != 0);                                    // accepted
        if (rem > 0) {
            CHECK(ref_.where.find(id) == ref_.where.end());   // id is fresh
            ref_.rest(side, px, rem, id);
            ++st_.rested;
        }
        touch(px);
    }

    void invalid_add() {
        got_.clear();
        auto h = [&](const Trade& t) { got_.push_back(t); };
        CHECK(book_.new_order(Side::Buy,  MID, 0,  h) == 0);              // qty 0
        CHECK(book_.new_order(Side::Sell, MID, -5, h) == 0);              // qty < 0
        CHECK(book_.new_order(Side::Buy,  BASE - 1, 10, h) == 0);         // below ladder
        CHECK(book_.new_order(Side::Sell, BASE + Price(NUM_TICKS), 10, h) == 0);  // above
        CHECK(got_.empty());
        ++st_.invalid_rejects;
    }

    OrderId pick_live() { return ref_.ids[rng_.below(ref_.ids.size())]; }

    void cancel_live(bool via_apply) {
        const OrderId id = pick_live();
        const Price px = ref_.where.at(id).px;
        if (via_apply) {
            Message m{};
            m.type = MsgType::Cancel;
            m.id   = id;
            book_.apply(m, [](const Trade&) {});
        } else {
            CHECK(book_.cancel(id));
        }
        ref_.remove(id);
        CHECK(!book_.cancel(id));                          // second cancel fails
        touch(px);
        ++st_.cancels;
    }

    void reduce_live(bool via_apply, MsgType type) {
        const OrderId id = pick_live();
        const Loc loc = ref_.where.at(id);
        const Qty have = ref_.qty_of(id);
        // 1..have+3: partial most of the time, sometimes exact, sometimes over.
        const Qty q = Qty(rng_.range(1, have + 3));
        const Qty expect = q < have ? q : have;
        if (via_apply) {
            Message m{};
            m.type = type;
            m.id   = id;
            m.qty  = q;
            book_.apply(m, [](const Trade&) {});
        } else {
            CHECK(book_.reduce(id, q) == expect);
        }
        ref_.reduce(id, q);
        if (q >= have) ++st_.full_reduces; else ++st_.partial_reduces;
        touch(loc.px);
    }

    // Dead ids (filled, cancelled, reduced to zero) must never resolve again,
    // even after their arena slot has been recycled by newer orders.
    void stale_op() {
        if (ref_.dead.empty()) return;
        const OrderId id = ref_.dead[rng_.below(ref_.dead.size())];
        switch (rng_.below(3)) {
        case 0: CHECK(!book_.cancel(id));      break;
        case 1: CHECK(book_.reduce(id, 3) == 0); break;
        default: {
            Message m{};
            m.type = MsgType::Execute;
            m.id   = id;
            m.qty  = 2;
            book_.apply(m, [](const Trade&) {});           // must be a no-op
        }
        }
        ++st_.stale_rejects;
    }

    // ---- invariants ------------------------------------------------------
    void verify_fast() {
        const Price bb = book_.best_bid(), ba = book_.best_ask();
        CHECK(bb == ref_.best_bid());
        CHECK(ba == ref_.best_ask());
        if (bb != -1 && ba != -1) CHECK(bb < ba);          // never crossed
        for (Price p : touched_) CHECK(book_.level_qty(p) == ref_.level_sum(p));
        touched_.clear();
    }

    void verify_full() {
        for (std::size_t i = 0; i < NUM_TICKS; ++i) {
            const Price p = BASE + Price(i);
            CHECK(book_.level_qty(p) == ref_.level_sum(p));
        }
        // Model self-consistency: ids/where/levels describe the same orders.
        std::size_t n = 0;
        for (const auto& [px, lvl] : ref_.bids) { CHECK(!lvl.empty()); n += lvl.size(); }
        for (const auto& [px, lvl] : ref_.asks) { CHECK(!lvl.empty()); n += lvl.size(); }
        CHECK(n == ref_.ids.size() && n == ref_.where.size());
    }

    // Sweep the whole book with two huge marketable orders. Every order still
    // resting (up to ~2500, in every level) is consumed in strict price-time
    // order and compared with the model: a full FIFO audit that also reaches
    // orders no random op happened to cross. Then cancel what remains.
    void drain() {
        const std::size_t before = ref_.ids.size();
        add(Side::Buy,  BASE + Price(NUM_TICKS) - 1, 1'000'000, false);
        add(Side::Sell, BASE,                        1'000'000, false);
        verify_full();
        st_.drained_orders += before;

        while (!ref_.ids.empty()) {
            const OrderId id = ref_.ids.back();
            CHECK(book_.cancel(id));
            ref_.remove(id);
        }
        CHECK(book_.best_bid() == -1 && book_.best_ask() == -1);
        for (std::size_t i = 0; i < NUM_TICKS; ++i)
            CHECK(book_.level_qty(BASE + Price(i)) == 0);
    }

    Rng                rng_;
    Book               book_;
    RefBook            ref_;
    Stats              st_;
    std::vector<Trade> got_;
    std::vector<Price> touched_;
};

} // namespace

static void test_price_time_properties() {
    // Distinct fixed seeds: each is its own reproducible sequence.
    const std::uint64_t seeds[] = {0x5EED0001ull, 0x5EED0002ull, 0x5EED0003ull};
    Stats total;
    for (std::uint64_t seed : seeds) {
        Harness h(seed);
        total.add(h.run(20'000));
    }
    // The sequences must actually exercise the interesting paths; if a PRNG
    // or generator change ever made them vacuous, fail loudly.
    CHECK(total.trades > 1000);
    CHECK(total.crossing > 500);
    CHECK(total.post_only_rejects > 50);
    CHECK(total.partial_reduces > 500 && total.full_reduces > 100);
    CHECK(total.cancels > 1000);
    CHECK(total.stale_rejects > 500);
    CHECK(total.invalid_rejects > 100);
    CHECK(total.drained_orders > 1000);
    std::printf("  price-time props ... OK  (%llu ops: %llu trades, %llu lots, "
                "%llu crossing adds, %llu post-only rejects, %llu drained)\n",
                (unsigned long long)total.ops, (unsigned long long)total.trades,
                (unsigned long long)total.lots, (unsigned long long)total.crossing,
                (unsigned long long)total.post_only_rejects,
                (unsigned long long)total.drained_orders);
}

// ===========================================================================
// ITCH encoding helpers (independent of the library: plain big-endian puts)
// ===========================================================================
namespace {

using Bytes = std::vector<std::uint8_t>;

void put_be(Bytes& b, std::uint64_t v, int bytes) {
    for (int i = bytes - 1; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}

struct Hdr { std::uint16_t locate; std::uint16_t tracking; std::uint64_t ts; };

// One decoded-or-to-be-encoded message; fields not used by a type are zero.
struct Rec {
    char          type = 0;
    Hdr           h{};
    std::uint64_t ref = 0;
    char          side = 0;
    std::uint32_t shares = 0;        // A: shares, E: executed, X: canceled
    char          stock[8] = {};
    std::uint32_t price = 0;
    std::uint64_t match = 0;
};

void put_hdr(Bytes& b, char type, const Hdr& h) {
    b.push_back(std::uint8_t(type));
    put_be(b, h.locate, 2);
    put_be(b, h.tracking, 2);
    put_be(b, h.ts & 0xFFFFFFFFFFFFull, 6);
}

// Encode with the 2-byte big-endian length prefix, exactly as on the wire.
void encode(Bytes& b, const Rec& r) {
    switch (r.type) {
    case 'A':
        put_be(b, sizeof(itch::AddOrder), 2);
        put_hdr(b, 'A', r.h);
        put_be(b, r.ref, 8);
        b.push_back(std::uint8_t(r.side));
        put_be(b, r.shares, 4);
        b.insert(b.end(), r.stock, r.stock + 8);
        put_be(b, r.price, 4);
        break;
    case 'E':
        put_be(b, sizeof(itch::OrderExecuted), 2);
        put_hdr(b, 'E', r.h);
        put_be(b, r.ref, 8);
        put_be(b, r.shares, 4);
        put_be(b, r.match, 8);
        break;
    case 'X':
        put_be(b, sizeof(itch::OrderCancel), 2);
        put_hdr(b, 'X', r.h);
        put_be(b, r.ref, 8);
        put_be(b, r.shares, 4);
        break;
    case 'D':
        put_be(b, sizeof(itch::OrderDelete), 2);
        put_hdr(b, 'D', r.h);
        put_be(b, r.ref, 8);
        break;
    default: std::abort();
    }
}

const std::byte* bytes(const Bytes& b, std::size_t off = 0) {
    return reinterpret_cast<const std::byte*>(b.data()) + off;
}

// Decodes everything the parser hands over and checks it against `exp`.
struct RoundTripSink {
    const std::vector<Rec>& exp;
    std::size_t i = 0;

    const Rec& take(char type, const itch::MsgHeader& h) {
        CHECK(i < exp.size());
        const Rec& r = exp[i++];
        CHECK(r.type == type);
        CHECK(h.type == type);
        CHECK(itch::bswap(h.stock_locate)    == r.h.locate);
        CHECK(itch::bswap(h.tracking_number) == r.h.tracking);
        CHECK(itch::timestamp_ns(h)          == (r.h.ts & 0xFFFFFFFFFFFFull));
        return r;
    }
    void operator()(const itch::AddOrder& a) {
        const Rec& r = take('A', a.h);
        CHECK(itch::bswap(a.order_ref) == r.ref);
        CHECK(a.side == r.side);
        CHECK(itch::bswap(a.shares) == r.shares);
        CHECK(std::memcmp(a.stock, r.stock, 8) == 0);
        CHECK(itch::bswap(a.price) == r.price);
    }
    void operator()(const itch::OrderExecuted& e) {
        const Rec& r = take('E', e.h);
        CHECK(itch::bswap(e.order_ref) == r.ref);
        CHECK(itch::bswap(e.executed_shares) == r.shares);
        CHECK(itch::bswap(e.match_number) == r.match);
    }
    void operator()(const itch::OrderCancel& x) {
        const Rec& r = take('X', x.h);
        CHECK(itch::bswap(x.order_ref) == r.ref);
        CHECK(itch::bswap(x.canceled_shares) == r.shares);
    }
    void operator()(const itch::OrderDelete& d) {
        const Rec& r = take('D', d.h);
        CHECK(itch::bswap(d.order_ref) == r.ref);
    }
};

Rec random_rec(Rng& rng, char type) {
    Rec r;
    r.type = type;
    r.h = {std::uint16_t(rng.next()), std::uint16_t(rng.next()), rng.next()};
    r.ref = rng.next();
    switch (type) {
    case 'A':
        r.side   = rng.below(2) ? 'B' : 'S';
        r.shares = std::uint32_t(rng.next());
        for (char& c : r.stock) c = ' ';
        for (std::size_t i = 0, n = 1 + rng.below(8); i < n; ++i)
            r.stock[i] = char('A' + rng.below(26));
        r.price  = std::uint32_t(rng.next());
        break;
    case 'E': r.shares = std::uint32_t(rng.next()); r.match = rng.next(); break;
    case 'X': r.shares = std::uint32_t(rng.next()); break;
    default: break;
    }
    return r;
}

} // namespace

// ===========================================================================
// (c) ITCH PARSER ROUND TRIP
// ===========================================================================
static void test_itch_wire_layout() {
    // The overlay structs must be byte-identical to the ITCH 5.0 layout.
    static_assert(offsetof(itch::MsgHeader, stock_locate)    == 1);
    static_assert(offsetof(itch::MsgHeader, tracking_number) == 3);
    static_assert(offsetof(itch::MsgHeader, timestamp)       == 5);
    static_assert(offsetof(itch::AddOrder, order_ref) == 11);
    static_assert(offsetof(itch::AddOrder, side)      == 19);
    static_assert(offsetof(itch::AddOrder, shares)    == 20);
    static_assert(offsetof(itch::AddOrder, stock)     == 24);
    static_assert(offsetof(itch::AddOrder, price)     == 32);
    static_assert(offsetof(itch::OrderExecuted, order_ref)       == 11);
    static_assert(offsetof(itch::OrderExecuted, executed_shares) == 19);
    static_assert(offsetof(itch::OrderExecuted, match_number)    == 23);
    static_assert(offsetof(itch::OrderCancel, order_ref)       == 11);
    static_assert(offsetof(itch::OrderCancel, canceled_shares) == 19);
    static_assert(offsetof(itch::OrderDelete, order_ref) == 11);

    CHECK(itch::bswap(std::uint16_t{0x1234}) == 0x3412);
    CHECK(itch::bswap(std::uint32_t{0x01020304u}) == 0x04030201u);
    CHECK(itch::bswap(std::uint64_t{0x0102030405060708ull}) == 0x0807060504030201ull);
    const std::byte pfx[3] = {std::byte{0xAB}, std::byte{0xCD}, std::byte{0xEF}};
    CHECK(itch::load_be16(pfx)     == 0xABCD);
    CHECK(itch::load_be16(pfx + 1) == 0xCDEF);              // odd address
    std::puts("  itch wire layout ... OK");
}

static void test_itch_round_trip() {
    const char types[4] = {'A', 'E', 'X', 'D'};

    // 1. Extremes for every type: all-zero and all-ones fields.
    {
        std::vector<Rec> recs;
        for (char t : types) {
            Rec zero; zero.type = t;
            for (char& c : zero.stock) c = ' ';
            if (t == 'A') zero.side = 'B';
            recs.push_back(zero);

            Rec ones; ones.type = t;
            ones.h = {0xFFFF, 0xFFFF, 0xFFFFFFFFFFFFull};
            ones.ref = ~0ull; ones.shares = ~0u; ones.price = ~0u; ones.match = ~0ull;
            for (char& c : ones.stock) c = 'Z';
            if (t == 'A') ones.side = 'S';
            recs.push_back(ones);
        }
        Bytes wire;
        for (const Rec& r : recs) encode(wire, r);
        RoundTripSink sink{recs};
        itch::ItchParser p(bytes(wire), wire.size());
        while (p.next(sink)) {}
        CHECK(sink.i == recs.size());
        CHECK(p.exhausted());
    }

    // 2. Seeded random stream, all types interleaved; parsed from an aligned
    //    and from a deliberately misaligned (+1) base address.
    for (std::uint64_t seed : {0xA11CE001ull, 0xA11CE002ull}) {
        Rng rng(seed);
        std::vector<Rec> recs;
        Bytes wire;
        for (int i = 0; i < 4000; ++i) {
            Rec r = random_rec(rng, types[rng.below(4)]);
            recs.push_back(r);
            encode(wire, r);
        }
        for (std::size_t shift : {std::size_t{0}, std::size_t{1}}) {
            Bytes shifted(shift, 0xEE);
            shifted.insert(shifted.end(), wire.begin(), wire.end());
            RoundTripSink sink{recs};
            itch::ItchParser p(bytes(shifted, shift), wire.size());
            while (p.next(sink)) {}
            CHECK(sink.i == recs.size());
            CHECK(p.exhausted());
            CHECK(p.cursor() == bytes(shifted, shift) + wire.size());
        }
    }

    // 3. Every message type decodes on its own at the exact frame size.
    for (char t : types) {
        Rng rng(0xB0B0ull + std::uint64_t(t));
        std::vector<Rec> recs{random_rec(rng, t)};
        Bytes wire;
        encode(wire, recs[0]);
        CHECK(wire.size() == 2 + (t == 'A' ? 36u : t == 'E' ? 31u : t == 'X' ? 23u : 19u));
        RoundTripSink sink{recs};
        itch::ItchParser p(bytes(wire), wire.size());
        CHECK(p.next(sink));
        CHECK(!p.next(sink));
        CHECK(sink.i == 1);
    }
    std::puts("  itch round trip .... OK");
}

namespace {
struct CountSink {
    int n = 0;
    void operator()(const itch::AddOrder&)      { ++n; }
    void operator()(const itch::OrderExecuted&) { ++n; }
    void operator()(const itch::OrderCancel&)   { ++n; }
    void operator()(const itch::OrderDelete&)   { ++n; }
};
} // namespace

static void test_itch_framing_edges() {
    Rng rng(0xFADE);
    Bytes good;
    encode(good, random_rec(rng, 'D'));
    Bytes good_add;
    encode(good_add, random_rec(rng, 'A'));

    // Empty stream, and a lone byte (incomplete length prefix).
    {
        CountSink s;
        itch::ItchParser p(bytes(good), 0);
        CHECK(p.exhausted());
        CHECK(!p.next(s));
        itch::ItchParser q(bytes(good), 1);
        CHECK(!q.next(s));
        CHECK(q.cursor() == bytes(good));
        CHECK(s.n == 0);
    }

    // Payload shorter than the prefix claims: stop without advancing.
    for (std::size_t cut = 1; cut < good.size(); ++cut) {
        CountSink s;
        itch::ItchParser p(bytes(good), good.size() - cut);
        if (good.size() - cut >= 2) CHECK(!p.next(s));
        CHECK(s.n == 0);
        CHECK(p.cursor() == bytes(good));
    }

    // Wrong frame length for each known type -> rejected, sink untouched.
    for (char t : {'A', 'E', 'X', 'D'}) {
        Rec r = random_rec(rng, t);
        Bytes w;
        encode(w, r);
        for (int delta : {-1, +1}) {
            Bytes bad = w;
            const std::uint16_t len = std::uint16_t(w.size() - 2 + delta);
            bad[0] = std::uint8_t(len >> 8);
            bad[1] = std::uint8_t(len);
            if (delta > 0) bad.push_back(0);        // keep the claimed bytes present
            CountSink s;
            itch::ItchParser p(bytes(bad), bad.size());
            CHECK(!p.next(s));
            CHECK(s.n == 0);
            CHECK(p.cursor() == bytes(bad));
        }
    }

    // Unknown type (a real ITCH type this parser doesn't support): halts the
    // stream, and later valid frames are NOT delivered.
    {
        Bytes w;
        put_be(w, 12, 2);
        w.push_back('S');                           // System Event
        for (int i = 0; i < 11; ++i) w.push_back(0);
        const std::size_t bad_end = w.size();
        w.insert(w.end(), good.begin(), good.end());
        CountSink s;
        itch::ItchParser p(bytes(w), w.size());
        CHECK(!p.next(s));
        CHECK(s.n == 0);
        CHECK(p.cursor() == bytes(w));
        CHECK(bad_end < w.size());
    }

    // A valid frame followed by garbage: first delivered, then clean stop.
    {
        Bytes w = good;
        w.push_back(0x00);                          // dangling half prefix
        CountSink s;
        itch::ItchParser p(bytes(w), w.size());
        CHECK(p.next(s));
        CHECK(s.n == 1);
        CHECK(!p.next(s));
        CHECK(s.n == 1);
        CHECK(!p.exhausted());                      // one byte left unconsumed
    }
    std::puts("  itch framing edges . OK");
}

// ===========================================================================
// (b) JOURNAL REPLAY DETERMINISM
// ===========================================================================
namespace {

// Deterministic ITCH session with sparse 64-bit order references. A fraction
// of adds deliberately cross the spread, so the stream also produces trades
// and leaves later E/X/D messages pointing at already-consumed orders (the
// engine must treat those as stale, identically on every run).
Bytes build_feed(std::uint64_t seed, int n_msgs) {
    Rng rng(seed);
    Bytes buf;
    struct Live { std::uint64_t ref; std::uint32_t shares; };
    std::vector<Live> live;
    std::uint64_t counter = 0, ts = 34'200'000'000'000ull;

    for (int i = 0; i < n_msgs; ++i) {
        const std::uint64_t r = rng.next();
        ts += 1 + (r & 1023);
        const Hdr h{1, 0, ts};
        unsigned bucket = unsigned(r % 100);
        if (live.empty()) bucket = 0;

        Rec rec;
        rec.h = h;
        if (bucket < 50) {
            rec.type   = 'A';
            rec.ref    = (++counter << 12) | rng.below(4096);   // sparse, unique
            const bool buy = (r >> 8) & 1;
            const bool cross = ((r >> 12) & 0xF) == 0;           // ~6% marketable
            const std::int64_t off = 1 + std::int64_t((r >> 16) & 15);
            rec.side   = buy ? 'B' : 'S';
            rec.shares = 4 + std::uint32_t((r >> 24) & 31);
            const char sym[8] = {'L','O','B','X',' ',' ',' ',' '};
            std::memcpy(rec.stock, sym, 8);
            const std::int64_t px = cross ? (buy ? MID + off : MID - off)
                                          : (buy ? MID - off : MID + off);
            rec.price  = std::uint32_t(px);
            live.push_back({rec.ref, rec.shares});
        } else if (bucket < 70) {
            Live& o = live[(r >> 32) % live.size()];
            rec.type   = 'E';
            rec.ref    = o.ref;
            rec.shares = std::min<std::uint32_t>(1 + std::uint32_t((r >> 16) & 7), o.shares);
            rec.match  = std::uint64_t(i) + 1;
            o.shares  -= rec.shares;
            if (o.shares == 0) { o = live.back(); live.pop_back(); }
        } else if (bucket < 82) {
            Live& o = live[(r >> 32) % live.size()];
            if (o.shares < 2) { --i; continue; }
            rec.type   = 'X';
            rec.ref    = o.ref;
            rec.shares = 1 + std::uint32_t((r >> 16) % (o.shares - 1));
            o.shares  -= rec.shares;
        } else {
            const std::size_t k = (r >> 32) % live.size();
            rec.type = 'D';
            rec.ref  = live[k].ref;
            live[k]  = live.back();
            live.pop_back();
        }
        encode(buf, rec);
    }
    return buf;
}

// Everything observable about a finished session.
struct SessionState {
    std::uint64_t msgs = 0, trades = 0, lots = 0, trade_hash = 0, ladder_hash = 0,
                  stale = 0;
    Price bb = 0, ba = 0;
    bool operator==(const SessionState&) const = default;
};

constexpr std::uint64_t FNV_OFFSET = 1469598103934665603ull;
constexpr std::uint64_t FNV_PRIME  = 1099511628211ull;

// Gateway-style sink: ITCH order reference -> engine id via the sparse hash
// map, then apply to the book. Same shape as the benchmark gateway.
struct Replayer {
    Book&                       book;
    FlatHashMap<1u << 14>&      refs;
    SessionState&               st;

    void mix(std::uint64_t v) { st.trade_hash = (st.trade_hash ^ v) * FNV_PRIME; }

    void operator()(const itch::AddOrder& a) {
        ++st.msgs;
        const Side side = a.side == 'B' ? Side::Buy : Side::Sell;
        const OrderId id = book.new_order(
            side, Price(itch::bswap(a.price)), Qty(itch::bswap(a.shares)),
            [this](const Trade& t) {
                ++st.trades;
                st.lots += std::uint64_t(t.qty);
                mix(t.maker_id); mix(std::uint64_t(t.price)); mix(std::uint64_t(t.qty));
            });
        CHECK(id != 0);
        CHECK(refs.insert(itch::bswap(a.order_ref), id));
    }
    void operator()(const itch::OrderExecuted& e) {
        ++st.msgs;
        const Qty n = book.reduce(refs.find(itch::bswap(e.order_ref)),
                                  Qty(itch::bswap(e.executed_shares)));
        if (n == 0) ++st.stale;
        mix(std::uint64_t(n));
    }
    void operator()(const itch::OrderCancel& x) {
        ++st.msgs;
        const Qty n = book.reduce(refs.find(itch::bswap(x.order_ref)),
                                  Qty(itch::bswap(x.canceled_shares)));
        if (n == 0) ++st.stale;
        mix(std::uint64_t(n));
    }
    void operator()(const itch::OrderDelete& d) {
        ++st.msgs;
        const std::uint64_t ref = itch::bswap(d.order_ref);
        if (!book.cancel(refs.find(ref))) ++st.stale;
        refs.erase(ref);
    }
};

// Play a length-framed byte stream into a FRESH book; return its state.
SessionState play(const std::byte* data, std::size_t len) {
    auto book = std::make_unique<Book>(BASE);
    auto refs = std::make_unique<FlatHashMap<1u << 14>>();
    SessionState st;
    st.trade_hash = FNV_OFFSET;
    Replayer rp{*book, *refs, st};
    itch::ItchParser p(data, len);
    while (p.next(rp)) {}
    CHECK(p.exhausted());                           // consumed every byte

    st.bb = book->best_bid();
    st.ba = book->best_ask();
    st.ladder_hash = FNV_OFFSET;                    // fingerprint of the ladder
    for (std::size_t i = 0; i < NUM_TICKS; ++i)
        st.ladder_hash = (st.ladder_hash ^ std::uint64_t(book->level_qty(BASE + Price(i)))) * FNV_PRIME;
    return st;
}

std::string journal_path(const char* tag) {
    return (std::filesystem::temp_directory_path() /
            (std::string("lob_test_properties_") + tag + ".bin")).string();
}

} // namespace

static void test_replay_determinism() {
    constexpr int N = 6000;
    const Bytes feed = build_feed(0xC0FFEE01ull, N);

    // Independent fresh books fed the same bytes must agree exactly.
    const SessionState live_a = play(bytes(feed), feed.size());
    const SessionState live_b = play(bytes(feed), feed.size());
    CHECK(live_a == live_b);
    CHECK(live_a.msgs == std::uint64_t(N));
    CHECK(live_a.trades > 20);                      // crossing adds do trade
    CHECK(live_a.stale > 20);                       // stale refs do occur

    // A different seed must give a different fingerprint, or the hash is vacuous.
    const Bytes other = build_feed(0xC0FFEE02ull, N);
    const SessionState other_st = play(bytes(other), other.size());
    CHECK(other_st.ladder_hash != live_a.ladder_hash || other_st.trade_hash != live_a.trade_hash);

    // ---- journal the live session frame by frame (what the feed thread does)
    const std::string path = journal_path("replay");
    {
        Journal jr;
        CHECK(jr.open(path.c_str(), feed.size() + 64));
        const std::byte* cur = bytes(feed);
        const std::byte* end = cur + feed.size();
        while (cur < end) {
            const std::size_t n = 2 + itch::load_be16(cur);
            jr.append(cur, n);
            cur += n;
        }
        CHECK(jr.committed() == feed.size());
        CHECK(jr.dropped() == 0);
        jr.flush();
    }

    // ---- replay the journal twice into fresh books
    JournalReplay r1, r2;
    CHECK(r1.open(path.c_str()));
    CHECK(r2.open(path.c_str()));
    CHECK(r1.size() == feed.size());
    CHECK(std::memcmp(r1.data(), feed.data(), feed.size()) == 0);   // byte-identical
    const SessionState rep1 = play(r1.data(), r1.size());
    const SessionState rep2 = play(r2.data(), r2.size());
    CHECK(rep1 == live_a);
    CHECK(rep2 == live_a);
    CHECK(rep1 == rep2);
    r1.close(); r2.close();
    std::remove(path.c_str());

    // ---- a full journal drops whole frames, never tears one
    {
        const std::string small = journal_path("small");
        const std::size_t cap = feed.size() / 2;
        Journal jr;
        CHECK(jr.open(small.c_str(), cap));
        const std::byte* cur = bytes(feed);
        const std::byte* end = cur + feed.size();
        while (cur < end) {
            const std::size_t n = 2 + itch::load_be16(cur);
            jr.append(cur, n);
            cur += n;
        }
        CHECK(jr.dropped() > 0);
        CHECK(jr.committed() <= cap);
        CHECK(jr.committed() + jr.dropped() == feed.size());
        jr.flush();
        jr.close();

        JournalReplay rp;
        CHECK(rp.open(small.c_str()));
        CHECK(rp.size() > 0 && rp.size() <= cap);
        CountSink cs;
        itch::ItchParser p(rp.data(), rp.size());
        while (p.next(cs)) {}
        CHECK(p.exhausted());                       // ends exactly on a frame boundary
        CHECK(cs.n > 0);
        rp.close();
        std::remove(small.c_str());
    }

    // A path that is not a journal must be refused.
    {
        const std::string junk = journal_path("junk");
        std::FILE* f = std::fopen(junk.c_str(), "wb");
        CHECK(f != nullptr);
        const Bytes zeros(8192, 0);
        std::fwrite(zeros.data(), 1, zeros.size(), f);
        std::fclose(f);
        JournalReplay rp;
        CHECK(!rp.open(junk.c_str()));
        std::remove(junk.c_str());
    }
    std::printf("  replay determinism . OK  (%d msgs, %llu trades, %llu stale, "
                "ladder fp %016llx)\n", N, (unsigned long long)live_a.trades,
                (unsigned long long)live_a.stale,
                (unsigned long long)live_a.ladder_hash);
}

// ===========================================================================
// (d) SPSC RING BOUNDARIES
// ===========================================================================
static void test_spsc_ring_boundaries() {
    // Minimum legal capacity (2): every push/pop is a boundary.
    {
        SpscRing<std::uint64_t, 2> ring{};   // {} zero-fills slots (quiets GCC maybe-uninitialized)
        std::uint64_t v = 0;
        CHECK(!ring.try_pop(v));
        CHECK(ring.try_push(10));
        CHECK(ring.try_push(11));
        CHECK(!ring.try_push(12));                  // full
        CHECK(ring.size_approx() == 2);
        CHECK(ring.try_pop(v) && v == 10);
        CHECK(ring.try_push(12));                   // wraps into slot 0
        CHECK(!ring.try_push(13));
        CHECK(ring.try_pop(v) && v == 11);
        CHECK(ring.try_pop(v) && v == 12);
        CHECK(!ring.try_pop(v));
        CHECK(ring.size_approx() == 0);
    }

    // Capacity 8: fill, overfill, drain, underflow, many full wrap cycles.
    {
        SpscRing<std::uint64_t, 8> ring{};
        std::uint64_t v = ~0ull, next_in = 0, next_out = 0;
        CHECK(!ring.try_pop(v) && v == ~0ull);      // failed pop leaves `out` alone
        for (int cycle = 0; cycle < 50; ++cycle) {  // 400 items through 8 slots
            for (int i = 0; i < 8; ++i) CHECK(ring.try_push(next_in++));
            CHECK(ring.size_approx() == 8);
            for (int i = 0; i < 3; ++i) CHECK(!ring.try_push(999));   // stays full
            CHECK(ring.size_approx() == 8);
            for (int i = 0; i < 8; ++i) {
                CHECK(ring.try_pop(v));
                CHECK(v == next_out++);
            }
            CHECK(!ring.try_pop(v));
            CHECK(ring.size_approx() == 0);
        }
        // Offset start: partial fill then slide across the wrap point.
        for (int i = 0; i < 5; ++i) CHECK(ring.try_push(next_in++));
        for (int round = 0; round < 100; ++round) {
            CHECK(ring.try_push(next_in++));
            CHECK(ring.try_pop(v)); CHECK(v == next_out++);
        }
        while (ring.try_pop(v)) CHECK(v == next_out++);
        CHECK(next_out == next_in);
    }

    // Random interleave against a std::deque oracle, with phases biased toward
    // "mostly full" and "mostly empty" so both stale-cache refresh paths run.
    {
        SpscRing<Message, 16> ring{};
        std::deque<std::uint64_t> oracle;
        Rng rng(0x5B5C0001ull);
        std::uint64_t seq = 0, full_hits = 0, empty_hits = 0;
        for (int i = 0; i < 300'000; ++i) {
            const unsigned push_bias = (i / 5000) % 3 == 0 ? 75 : (i / 5000) % 3 == 1 ? 25 : 50;
            if (rng.below(100) < push_bias) {
                Message m{};
                m.id = seq; m.qty = Qty(seq * 3); m.price = Price(seq ^ 0x55);
                const bool ok = ring.try_push(m);
                CHECK(ok == (oracle.size() < 16));
                if (ok) { oracle.push_back(seq); ++seq; } else ++full_hits;
            } else {
                Message m{};
                const bool ok = ring.try_pop(m);
                CHECK(ok == !oracle.empty());
                if (ok) {
                    CHECK(m.id == oracle.front());
                    CHECK(m.qty == Qty(oracle.front() * 3));
                    CHECK(m.price == Price(oracle.front() ^ 0x55));
                    oracle.pop_front();
                } else ++empty_hits;
            }
            CHECK(ring.size_approx() == oracle.size());
        }
        CHECK(full_hits > 1000 && empty_hits > 1000);
        CHECK(seq > 50'000);
    }
    std::puts("  spsc boundaries .... OK");
}

static void test_arena_ring_boundaries() {
    SpscArenaRing<64, 4> ring;
    CHECK(ring.slot_bytes() == 64);
    CHECK(ring.try_front() == nullptr);             // empty
    CHECK(ring.size_approx() == 0);

    auto put = [&](std::uint64_t v) {
        std::byte* s = ring.try_acquire();
        if (!s) return false;
        std::memcpy(s, &v, sizeof v);
        ring.publish();
        return true;
    };
    auto get = [&](std::uint64_t& v) {
        const std::byte* s = ring.try_front();
        if (!s) return false;
        std::memcpy(&v, s, sizeof v);
        ring.release();
        return true;
    };

    // Acquire without publish does not consume capacity or become visible.
    std::byte* first = ring.try_acquire();
    CHECK(first != nullptr);
    CHECK(ring.try_acquire() == first);             // same slot until published
    CHECK(ring.try_front() == nullptr);

    for (std::uint64_t i = 0; i < 4; ++i) CHECK(put(100 + i));
    CHECK(ring.size_approx() == 4);
    CHECK(ring.try_acquire() == nullptr);           // full
    CHECK(!put(999));

    std::uint64_t v = 0;
    CHECK(get(v) && v == 100);
    std::byte* wrapped = ring.try_acquire();        // freed slot 0 comes back
    CHECK(wrapped == first);
    CHECK(put(104));                                // wrap-around write
    CHECK(ring.try_acquire() == nullptr);
    for (std::uint64_t want = 101; want <= 104; ++want) CHECK(get(v) && v == want);
    CHECK(!get(v));

    // Long run across many wraps, with a deque oracle and random bias.
    std::deque<std::uint64_t> oracle;
    Rng rng(0xA4E0A001ull);
    std::uint64_t seq = 1000;
    for (int i = 0; i < 100'000; ++i) {
        if (rng.below(2)) {
            const bool ok = put(seq);
            CHECK(ok == (oracle.size() < 4));
            if (ok) oracle.push_back(seq++);
        } else {
            const bool ok = get(v);
            CHECK(ok == !oracle.empty());
            if (ok) { CHECK(v == oracle.front()); oracle.pop_front(); }
        }
        CHECK(ring.size_approx() == oracle.size());
    }
    std::puts("  arena ring bounds .. OK");
}

// ---------------------------------------------------------------------------
int main() {
    std::puts("property suite:");
    test_price_time_properties();
    test_itch_wire_layout();
    test_itch_round_trip();
    test_itch_framing_edges();
    test_replay_determinism();
    test_spsc_ring_boundaries();
    test_arena_ring_boundaries();
    std::printf("%llu checks evaluated\n", (unsigned long long)g_checks);
    std::puts("ALL PROPERTY TESTS PASSED");
    return 0;
}
