// ============================================================================
// test_correctness.cpp -- Functional verification of every component.
// The benchmark numbers mean nothing if the book is wrong, so this runs first.
// Plain asserts, zero dependencies: a failing test aborts with the location.
// ============================================================================

#include "lob/order_book.hpp"
#include "lob/spsc_ring.hpp"
#include "lob/level_bitmap.hpp"
#include "lob/memory_pool.hpp"
#include "lob/itch.hpp"

#include <cstdio>
#include <cstdlib>
#include <vector>
#include <thread>

#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::fprintf(stderr, "FAILED: %s  (%s:%d)\n", #cond, __FILE__,   \
                         __LINE__);                                          \
            std::abort();                                                    \
        }                                                                    \
    } while (0)

using namespace lob;

// Small book so tests exercise edges quickly.
using Book = OrderBook<1u << 10, 1u << 12>;
static constexpr Price BASE = 100'000;  // ladder covers [100000, 101023] ticks

static std::vector<Trade> g_trades;
static void collect(const Trade& t) { g_trades.push_back(t); }

// ---------------------------------------------------------------------------
static void test_memory_pool() {
    MemoryPool<Order> pool(8);
    Order* a = pool.acquire();
    Order* b = pool.acquire();
    CHECK(a && b && a != b);
    CHECK(pool.index_of(a) != pool.index_of(b));
    CHECK(pool.at(pool.index_of(a)) == a);

    // LIFO reuse: release then acquire returns the same (cache-hot) slot.
    pool.release(b);
    Order* c = pool.acquire();
    CHECK(c == b);

    // Exhaustion returns nullptr instead of allocating.
    std::vector<Order*> all;
    while (Order* o = pool.acquire()) all.push_back(o);
    CHECK(all.size() == 8 - 2);            // a and c still held
    for (Order* o : all) pool.release(o);
    std::puts("  memory_pool ........ OK");
}

// ---------------------------------------------------------------------------
static void test_bitmap() {
    LevelBitmap<1u << 12> bm;
    constexpr auto NPOS = LevelBitmap<1u << 12>::NPOS;

    CHECK(bm.next_set(0) == NPOS);
    bm.set(5); bm.set(700); bm.set(4000);
    CHECK(bm.test(5) && bm.test(700) && bm.test(4000));
    CHECK(bm.next_set(0) == 5);
    CHECK(bm.next_set(6) == 700);       // crosses an L0 word boundary
    CHECK(bm.next_set(701) == 4000);    // crosses an L1 word boundary
    CHECK(bm.next_set(4001) == NPOS);
    CHECK(bm.prev_set(4095) == 4000);
    CHECK(bm.prev_set(3999) == 700);
    CHECK(bm.prev_set(699) == 5);
    CHECK(bm.prev_set(4) == NPOS);

    bm.reset(700);
    CHECK(!bm.test(700));
    CHECK(bm.next_set(6) == 4000);

    // exhaustive round-trip on a stride pattern
    LevelBitmap<1u << 12> bm2;
    for (std::size_t i = 0; i < (1u << 12); i += 37) bm2.set(i);
    for (std::size_t i = 0; i < (1u << 12); ++i) {
        const std::size_t expect_next = ((i + 36) / 37) * 37;
        const std::size_t got = bm2.next_set(i);
        if (expect_next < (1u << 12)) CHECK(got == expect_next);
        else                          CHECK(got == NPOS);
    }
    std::puts("  level_bitmap ....... OK");
}

// ---------------------------------------------------------------------------
static void test_book_basic_match() {
    Book book(BASE);
    g_trades.clear();

    // Rest a sell 100 @ 100500, then cross it with a buy 60 @ 100500.
    OrderId ask = book.new_order(Side::Sell, 100'500, 100, collect);
    CHECK(ask != 0);
    CHECK(book.best_ask() == 100'500);
    CHECK(book.best_bid() == -1);
    CHECK(g_trades.empty());

    book.new_order(Side::Buy, 100'500, 60, collect);
    CHECK(g_trades.size() == 1);
    CHECK(g_trades[0].maker_id == ask);
    CHECK(g_trades[0].price == 100'500);
    CHECK(g_trades[0].qty == 60);
    CHECK(book.level_qty(100'500) == 40);     // maker partially filled
    CHECK(book.best_ask() == 100'500);

    // Take the rest and one tick more than available -> remainder rests as bid.
    book.new_order(Side::Buy, 100'500, 50, collect);
    CHECK(g_trades.size() == 2);
    CHECK(g_trades[1].qty == 40);
    CHECK(book.best_ask() == -1);             // ask side empty
    CHECK(book.best_bid() == 100'500);        // 10 lots rested
    CHECK(book.level_qty(100'500) == 10);
    std::puts("  basic match ........ OK");
}

// ---------------------------------------------------------------------------
static void test_price_time_priority() {
    Book book(BASE);
    g_trades.clear();

    OrderId a1 = book.new_order(Side::Sell, 100'200, 10, collect);  // first in
    OrderId a2 = book.new_order(Side::Sell, 100'200, 10, collect);  // second
    OrderId a3 = book.new_order(Side::Sell, 100'100, 10, collect);  // better px

    book.new_order(Side::Buy, 100'300, 25, collect);
    // Expect: a3 (best price) then a1 (time priority) then a2 partial.
    CHECK(g_trades.size() == 3);
    CHECK(g_trades[0].maker_id == a3 && g_trades[0].qty == 10 &&
          g_trades[0].price == 100'100);
    CHECK(g_trades[1].maker_id == a1 && g_trades[1].qty == 10 &&
          g_trades[1].price == 100'200);
    CHECK(g_trades[2].maker_id == a2 && g_trades[2].qty == 5);
    CHECK(book.level_qty(100'200) == 5);
    CHECK(book.best_ask() == 100'200);
    std::puts("  price-time ......... OK");
}

// ---------------------------------------------------------------------------
static void test_cancel() {
    Book book(BASE);
    g_trades.clear();

    OrderId b1 = book.new_order(Side::Buy, 100'400, 10, collect);
    OrderId b2 = book.new_order(Side::Buy, 100'400, 20, collect);  // middle
    OrderId b3 = book.new_order(Side::Buy, 100'400, 30, collect);

    CHECK(book.level_qty(100'400) == 60);
    CHECK(book.cancel(b2));                    // unlink from the middle
    CHECK(book.level_qty(100'400) == 40);
    CHECK(!book.cancel(b2));                   // double-cancel rejected

    // FIFO must now be b1 -> b3.
    book.new_order(Side::Sell, 100'400, 40, collect);
    CHECK(g_trades.size() == 2);
    CHECK(g_trades[0].maker_id == b1 && g_trades[0].qty == 10);
    CHECK(g_trades[1].maker_id == b3 && g_trades[1].qty == 30);
    CHECK(book.best_bid() == -1);

    // Cancelling the only order at the best level must reveal the next best.
    OrderId c1 = book.new_order(Side::Buy, 100'450, 5, collect);
    book.new_order(Side::Buy, 100'440, 5, collect);
    CHECK(book.best_bid() == 100'450);
    CHECK(book.cancel(c1));
    CHECK(book.best_bid() == 100'440);

    // Generation check: a stale id whose slot was recycled must be rejected.
    OrderId d1 = book.new_order(Side::Buy, 100'300, 7, collect);
    CHECK(book.cancel(d1));
    OrderId d2 = book.new_order(Side::Buy, 100'300, 7, collect); // reuses slot
    CHECK(d1 != d2);
    CHECK(!book.cancel(d1));                   // stale
    CHECK(book.cancel(d2));
    std::puts("  cancel ............. OK");
}

// ---------------------------------------------------------------------------
static void test_sweep_across_gap() {
    Book book(BASE);
    g_trades.clear();

    book.new_order(Side::Sell, 100'010, 5, collect);
    book.new_order(Side::Sell, 100'900, 5, collect);   // 890-tick gap
    book.new_order(Side::Buy, 100'950, 12, collect);   // sweeps both, rests 2
    CHECK(g_trades.size() == 2);
    CHECK(g_trades[0].price == 100'010);
    CHECK(g_trades[1].price == 100'900);
    CHECK(book.best_ask() == -1);
    CHECK(book.best_bid() == 100'950);
    CHECK(book.level_qty(100'950) == 2);
    std::puts("  gap sweep .......... OK");
}

// ---------------------------------------------------------------------------
static void test_reduce() {
    Book book(BASE);
    g_trades.clear();

    OrderId a = book.new_order(Side::Sell, 100'600, 10, collect);
    OrderId b = book.new_order(Side::Sell, 100'700, 5, collect);

    // Partial reduce: shrinks in place, keeps priority and level totals.
    CHECK(book.reduce(a, 4) == 4);
    CHECK(book.level_qty(100'600) == 6);
    CHECK(book.best_ask() == 100'600);

    // Reduce to zero: removes the order and reveals the next level.
    CHECK(book.reduce(a, 6) == 6);
    CHECK(book.level_qty(100'600) == 0);
    CHECK(book.best_ask() == 100'700);

    // Stale id after removal must be rejected.
    CHECK(book.reduce(a, 1) == 0);
    // Over-reduce caps at remaining quantity (ITCH never sends this, but
    // the engine must stay consistent regardless).
    CHECK(book.reduce(b, 99) == 5);
    CHECK(book.best_ask() == -1);
    std::puts("  reduce ............. OK");
}

// ---------------------------------------------------------------------------
// ITCH parser: hand-craft a big-endian wire buffer, run the zero-copy
// parser over it, and verify every decoded field.
// ---------------------------------------------------------------------------
static void put_be(std::vector<std::uint8_t>& b, std::uint64_t v, int bytes) {
    for (int i = bytes - 1; i >= 0; --i)
        b.push_back(std::uint8_t(v >> (8 * i)));
}

static void test_itch_parser() {
    std::vector<std::uint8_t> wire;
    auto header = [&](char t) {
        wire.push_back(std::uint8_t(t));
        put_be(wire, 7, 2);                    // stock locate
        put_be(wire, 3, 2);                    // tracking number
        put_be(wire, 123'456'789'012ull, 6);   // timestamp
    };

    // 'A' add: ref=42, sell, 500 shares, price $123.4567
    put_be(wire, sizeof(itch::AddOrder), 2);
    header('A');
    put_be(wire, 42, 8);
    wire.push_back('S');
    put_be(wire, 500, 4);
    const char sym[8] = {'L','O','B','X',' ',' ',' ',' '};
    wire.insert(wire.end(), sym, sym + 8);
    put_be(wire, 1'234'567, 4);

    // 'E' executed: ref=42, 200 shares, match 9001
    put_be(wire, sizeof(itch::OrderExecuted), 2);
    header('E');
    put_be(wire, 42, 8);
    put_be(wire, 200, 4);
    put_be(wire, 9001, 8);

    // 'X' partial cancel: ref=42, 100 shares
    put_be(wire, sizeof(itch::OrderCancel), 2);
    header('X');
    put_be(wire, 42, 8);
    put_be(wire, 100, 4);

    // 'D' delete: ref=42
    put_be(wire, sizeof(itch::OrderDelete), 2);
    header('D');
    put_be(wire, 42, 8);

    struct Sink {
        int seen = 0;
        void operator()(const itch::AddOrder& a) {
            CHECK(itch::bswap(a.h.stock_locate) == 7);
            CHECK(itch::bswap(a.h.tracking_number) == 3);
            CHECK(itch::timestamp_ns(a.h) == 123'456'789'012ull);
            CHECK(itch::bswap(a.order_ref) == 42);
            CHECK(a.side == 'S');
            CHECK(itch::bswap(a.shares) == 500);
            CHECK(std::memcmp(a.stock, "LOBX    ", 8) == 0);
            CHECK(itch::bswap(a.price) == 1'234'567);
            ++seen;
        }
        void operator()(const itch::OrderExecuted& e) {
            CHECK(itch::bswap(e.order_ref) == 42);
            CHECK(itch::bswap(e.executed_shares) == 200);
            CHECK(itch::bswap(e.match_number) == 9001);
            ++seen;
        }
        void operator()(const itch::OrderCancel& x) {
            CHECK(itch::bswap(x.order_ref) == 42);
            CHECK(itch::bswap(x.canceled_shares) == 100);
            ++seen;
        }
        void operator()(const itch::OrderDelete& d) {
            CHECK(itch::bswap(d.order_ref) == 42);
            ++seen;
        }
    } sink;

    itch::ItchParser p(reinterpret_cast<const std::byte*>(wire.data()),
                       wire.size());
    while (p.next(sink)) {}
    CHECK(sink.seen == 4);
    CHECK(p.exhausted());

    // Truncated frame must stop cleanly, never read past the end.
    itch::ItchParser trunc(reinterpret_cast<const std::byte*>(wire.data()),
                           wire.size() - 5);
    Sink s2;
    while (trunc.next(s2)) {}
    CHECK(s2.seen == 3);
    std::puts("  itch_parser ........ OK");
}

// ---------------------------------------------------------------------------
// SPSC ring: move 4M sequenced items across two real threads and verify
// order, completeness, and that neither side ever blocks indefinitely.
static void test_spsc_ring() {
    static SpscRing<std::uint64_t, 1024> ring;
    constexpr std::uint64_t N = 4'000'000;

    std::thread producer([] {
        for (std::uint64_t i = 0; i < N; ++i)
            while (!ring.try_push(i)) { /* spin: consumer will drain */ }
    });

    std::uint64_t expected = 0, v = 0;
    while (expected < N) {
        if (ring.try_pop(v)) {
            CHECK(v == expected);   // strict FIFO, no loss, no tearing
            ++expected;
        }
    }
    producer.join();
    CHECK(ring.size_approx() == 0);
    std::puts("  spsc_ring (4M x-thread) OK");
}

// ---------------------------------------------------------------------------
int main() {
    std::puts("correctness suite:");
    test_memory_pool();
    test_bitmap();
    test_book_basic_match();
    test_price_time_priority();
    test_cancel();
    test_sweep_across_gap();
    test_reduce();
    test_itch_parser();
    test_spsc_ring();
    std::puts("ALL TESTS PASSED");
    return 0;
}
