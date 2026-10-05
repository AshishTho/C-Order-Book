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
#include "lob/risk.hpp"

#include <cstdio>
#include <cstdlib>
#include <vector>
#include <thread>
#include <algorithm>
#include <limits>
#include <random>

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

// Boundary inputs must be rejected before touching arena storage or doing
// signed arithmetic. These cases are also exercised in the sanitizer build.
static void test_book_invalid_inputs() {
    Book book(BASE);
    g_trades.clear();
    constexpr Qty QMAX = std::numeric_limits<Qty>::max();
    constexpr Price PMIN = std::numeric_limits<Price>::min();
    constexpr Price PMAX = std::numeric_limits<Price>::max();
    const auto invalid_side = static_cast<Side>(255);
    CHECK(!book.cancel(1));                  // never-allocated slot, generation 0
    CHECK(book.reduce(1, 1) == 0);
    CHECK(book.execute(1, 1) == 0);
    CHECK(!book.cancel(0));
    CHECK(!book.cancel(~OrderId{0}));
    CHECK(book.new_order(invalid_side, BASE, 1, collect) == 0);
    CHECK(book.new_iceberg_order(invalid_side, BASE, 1, 2, collect) == 0);
    CHECK(book.new_order(Side::Buy, PMIN, 1, collect) == 0);
    CHECK(book.new_order(Side::Buy, PMAX, 1, collect) == 0);
    CHECK(book.new_order(Side::Buy, BASE, 0, collect) == 0);
    CHECK(book.new_order(Side::Buy, BASE, -1, collect) == 0);
    CHECK(book.new_iceberg_order(Side::Buy, BASE, 0, 1, collect) == 0);
    CHECK(book.level_qty(PMIN) == 0 && book.level_qty(PMAX) == 0);

    const auto id = book.new_order(Side::Buy, BASE, 10, collect);
    CHECK(book.reduce(id, -1) == 0);
    CHECK(book.reduce(id, std::numeric_limits<Qty>::min()) == 0);
    CHECK(book.execute(id, 0) == 0);
    CHECK(book.level_qty(BASE) == 10);
    CHECK(book.cancel(id));
    const auto dead_generation = id + (OrderId{1} << 32);
    CHECK(!book.cancel(dead_generation));   // forged current, but dead, generation
    CHECK(book.reduce(dead_generation, 1) == 0);
    CHECK(book.execute(dead_generation, 1) == 0);

    const auto huge = book.new_order(Side::Buy, BASE, QMAX, collect);
    CHECK(huge != 0);
    CHECK(book.new_order(Side::Buy, BASE, 1, collect) == 0);
    CHECK(book.new_iceberg_order(Side::Buy, BASE, 1, 2, collect) == 0);
    CHECK(book.level_qty(BASE) == QMAX);
    CHECK(book.cancel(huge));
    const auto reserve = book.new_iceberg_order(Side::Sell, BASE, 1, QMAX, collect);
    CHECK(reserve != 0);
    CHECK(book.new_order(Side::Sell, BASE, 1, collect) == 0);
    CHECK(book.execute(reserve, QMAX - 1) == QMAX - 1);
    CHECK(book.level_qty(BASE) == 1);
    CHECK(book.execute(reserve, 1) == 1);
    CHECK(book.best_ask() == -1);

    // Negative ladder bases and a ladder near INT64_MAX remain defined.
    Book low(PMIN);
    CHECK(low.new_order(Side::Buy, PMAX, 1, collect) == 0);
    CHECK(low.new_order(Side::Buy, PMIN + 1, 1, collect) != 0);
    CHECK(low.best_bid() == PMIN + 1);
    Book high(PMAX - 1);
    CHECK(high.new_order(Side::Buy, PMIN, 1, collect) == 0);
    CHECK(high.new_order(Side::Buy, PMAX, 1, collect) != 0);
    CHECK(high.best_bid() == PMAX);
    CHECK(g_trades.empty());
    std::puts("  invalid inputs / overflow OK");
}

static void test_advanced_orders() {
    for (const Side maker_side : {Side::Buy, Side::Sell}) {
        const Side taker_side = maker_side == Side::Buy ? Side::Sell : Side::Buy;
        Book book(BASE);
        g_trades.clear();
        const auto ice_a = book.new_iceberg_order(maker_side, BASE + 10, 3, 8, collect);
        const auto plain = book.new_order(maker_side, BASE + 10, 2, collect);
        const auto ice_b = book.new_iceberg_order(maker_side, BASE + 10, 2, 5, collect);
        CHECK(book.level_qty(BASE + 10) == 7);
        CHECK(book.new_order(taker_side, BASE + 10, 1, collect, true) == 0);
        CHECK(g_trades.empty() && book.level_qty(BASE + 10) == 7);
        CHECK(book.new_order(taker_side, BASE + 10, 13, collect) == ~OrderId{0});
        const OrderId expected_ids[] = {ice_a, plain, ice_b, ice_a, ice_b, ice_a};
        const Qty expected_qty[] = {3, 2, 2, 3, 2, 1};
        CHECK(g_trades.size() == 6);
        for (std::size_t i = 0; i < g_trades.size(); ++i) {
            CHECK(g_trades[i].maker_id == expected_ids[i]);
            CHECK(g_trades[i].qty == expected_qty[i]);
        }
        CHECK(book.level_qty(BASE + 10) == 2);
        CHECK(!book.cancel(plain));
        CHECK(book.cancel(ice_a) && book.cancel(ice_b));
        CHECK(book.best_bid() == -1 && book.best_ask() == -1);

        // Cancels consume reserve first. Executions consume the displayed
        // clip first and lose queue priority upon replenishment.
        const auto ice = book.new_iceberg_order(maker_side, BASE + 20, 5, 20, collect);
        const auto behind = book.new_order(maker_side, BASE + 20, 2, collect);
        CHECK(book.reduce(ice, 3) == 3);
        CHECK(book.level_qty(BASE + 20) == 7);
        Message execution{};
        execution.id = ice; execution.qty = 8; execution.type = MsgType::Execute;
        book.apply(execution, collect);          // remaining 9, visible 2
        CHECK(book.level_qty(BASE + 20) == 4);
        g_trades.clear();
        book.new_order(taker_side, BASE + 20, 2, collect);
        CHECK(g_trades.size() == 1 && g_trades[0].maker_id == behind);
        CHECK(book.reduce(ice, 8) == 8);         // hidden 7, then visible 1
        CHECK(book.level_qty(BASE + 20) == 1);
        CHECK(book.execute(ice, 100) == 1);
        CHECK(!book.cancel(ice));
    }
    std::puts("  iceberg / post-only OK");
}

static void test_risk_boundaries() {
    RiskGateway<4> rate(10, BASE, BASE + 100, 100, 100);
    for (std::uint64_t now = 0; now < 4; ++now)
        CHECK(rate.check(Side::Buy, BASE, 1, now) == RJ_NONE);
    CHECK(rate.check(Side::Buy, BASE, 1, 4) == RJ_RATE);
    CHECK(rate.accepted() == 4 && rate.open_buy() == 4);
    CHECK(rate.check(Side::Buy, BASE, 1, 100) == RJ_NONE); // exact expiry boundary
    CHECK((rate.check(Side::Buy, BASE, 1, 99) & RJ_CLOCK) != 0);
    CHECK(rate.accepted() == 5 && rate.open_buy() == 5);

    RiskGateway<4> risk(10, BASE, BASE + 100, 10, 0);
    CHECK(risk.check(Side::Buy, BASE, 8, 0) == RJ_NONE);
    CHECK(!risk.on_fill(Side::Buy, 9));
    CHECK(!risk.on_cancel(Side::Buy, -1));
    CHECK(!risk.on_cancel(static_cast<Side>(2), 1));
    CHECK(!risk.on_fill(Side::Sell, 1));
    CHECK(risk.open_buy() == 8 && risk.net_position() == 0);
    CHECK(risk.on_fill(Side::Buy, 6));
    CHECK(risk.open_buy() == 2 && risk.net_position() == 6);
    CHECK(risk.check(Side::Buy, BASE, 3, 1) == RJ_POSITION);
    CHECK(risk.check(Side::Buy, BASE, 2, 1) == RJ_NONE);
    CHECK(risk.check(Side::Sell, BASE, 10, 1) == RJ_NONE);
    CHECK(risk.on_fill(Side::Sell, 10));
    CHECK(risk.net_position() == -4);
    CHECK(risk.on_cancel(Side::Buy, 4));
    CHECK(!risk.on_cancel(Side::Buy, 1));
    CHECK(risk.check(static_cast<Side>(255), BASE, 1, 1) == RJ_SIDE);
    CHECK((risk.check(Side::Buy, BASE - 1, -1, 1) & (RJ_QTY | RJ_PRICE_BAND)) ==
           (RJ_QTY | RJ_PRICE_BAND));

    constexpr Qty QMAX = std::numeric_limits<Qty>::max();
    RiskGateway<4> huge(QMAX, BASE, BASE, QMAX, 0);
    CHECK(huge.check(Side::Buy, BASE, QMAX, 0) == RJ_NONE);
    CHECK(huge.check(Side::Buy, BASE, 1, 0) == RJ_POSITION);
    CHECK(huge.check(Side::Buy, BASE, std::numeric_limits<Qty>::min(), 0) == RJ_QTY);
    CHECK(huge.on_fill(Side::Buy, QMAX));
    CHECK(huge.net_position() == QMAX);
    CHECK(huge.check(Side::Buy, BASE, 1, 0) == RJ_POSITION);
    CHECK(huge.check(Side::Sell, BASE, QMAX, 0) == RJ_NONE);
    CHECK(huge.on_fill(Side::Sell, QMAX));
    CHECK(huge.check(Side::Sell, BASE, QMAX, 0) == RJ_NONE);
    CHECK(huge.on_fill(Side::Sell, QMAX));
    CHECK(huge.net_position() == -QMAX);
    CHECK(huge.check(Side::Sell, BASE, 1, 0) == RJ_POSITION);
    CHECK(huge.check(Side::Buy, BASE, QMAX, 0) == RJ_NONE);
    CHECK(huge.on_fill(Side::Buy, QMAX));
    CHECK(huge.net_position() == 0);

    RiskGateway<4> invalid(0, BASE + 1, BASE, -1, 0);
    CHECK((invalid.check(Side::Buy, BASE, 1, 0) & RJ_CONFIG) != 0);
    CHECK(invalid.accepted() == 0 && invalid.open_buy() == 0);
    std::puts("  risk boundaries .... OK");
}

// An intentionally simple vector reference implementation shares none of
// the optimized book's arena, bitmap or linked-list logic. Every operation
// compares trades, every price level and BBO; fixed seeds make failures replayable.
static void test_randomized_reference() {
    struct RefOrder { OrderId id; Side side; Price price; Qty qty; };
    constexpr Price WIDTH = 64;
    for (const std::uint64_t seed : {7ull, 42ull, 20261004ull}) {
        std::mt19937_64 rng(seed);
        Book book(BASE);
        std::vector<RefOrder> orders;
        std::vector<OrderId> historical_ids;
        for (int step = 0; step < 12'000; ++step) {
            g_trades.clear();
            std::vector<Trade> expected_trades;
            const auto operation = rng() % 10;
            if (operation < 6) {
                const Side side = rng() & 1 ? Side::Buy : Side::Sell;
                const Price price = BASE + static_cast<Price>(rng() % WIDTH);
                const Qty qty = 1 + static_cast<Qty>(rng() % 40);
                const bool post_only = rng() % 7 == 0;
                auto best_match = [&]() {
                    std::size_t best = orders.size();
                    for (std::size_t i = 0; i < orders.size(); ++i) {
                        const auto& o = orders[i];
                        if (o.side == side || (side == Side::Buy ? o.price > price : o.price < price))
                            continue;
                        if (best == orders.size() || (side == Side::Buy ?
                            o.price < orders[best].price : o.price > orders[best].price)) best = i;
                    }
                    return best;
                };
                const bool rejected = post_only && best_match() != orders.size();
                Qty remaining = qty;
                while (!rejected && remaining > 0) {
                    const auto best = best_match();
                    if (best == orders.size()) break;
                    auto& maker = orders[best];
                    const Qty fill = std::min(remaining, maker.qty);
                    expected_trades.push_back(Trade{maker.id, 0, maker.price, fill});
                    maker.qty -= fill; remaining -= fill;
                    if (maker.qty == 0) orders.erase(orders.begin() + best);
                }
                const auto id = book.new_order(side, price, qty, collect, post_only);
                if (rejected) CHECK(id == 0);
                else if (remaining == 0) CHECK(id == ~OrderId{0});
                else {
                    CHECK(id != 0 && id != ~OrderId{0});
                    orders.push_back({id, side, price, remaining});
                    historical_ids.push_back(id);
                }
            } else {
                OrderId id = 0;
                if (!orders.empty() && rng() % 3 != 0)
                    id = orders[rng() % orders.size()].id;
                else if (!historical_ids.empty())
                    id = historical_ids[rng() % historical_ids.size()];
                const auto it = std::find_if(orders.begin(), orders.end(),
                                            [id](const RefOrder& o) { return o.id == id; });
                if (operation == 6) {
                    CHECK(book.cancel(id) == (it != orders.end()));
                    if (it != orders.end()) orders.erase(it);
                } else {
                    const Qty qty = static_cast<Qty>(rng() % 50) - 2;
                    const Qty removed = it == orders.end() || qty <= 0 ? 0 : std::min(qty, it->qty);
                    CHECK((operation == 7 ? book.execute(id, qty) : book.reduce(id, qty)) == removed);
                    if (it != orders.end() && removed > 0) {
                        it->qty -= removed;
                        if (it->qty == 0) orders.erase(it);
                    }
                }
            }
            CHECK(g_trades.size() == expected_trades.size());
            for (std::size_t i = 0; i < g_trades.size(); ++i) {
                CHECK(g_trades[i].maker_id == expected_trades[i].maker_id);
                CHECK(g_trades[i].price == expected_trades[i].price);
                CHECK(g_trades[i].qty == expected_trades[i].qty);
            }
            Price bid = -1, ask = -1;
            Qty depth[WIDTH]{};
            for (const auto& o : orders) {
                depth[o.price - BASE] += o.qty;
                if (o.side == Side::Buy && (bid == -1 || o.price > bid)) bid = o.price;
                if (o.side == Side::Sell && (ask == -1 || o.price < ask)) ask = o.price;
            }
            CHECK(book.best_bid() == bid && book.best_ask() == ask);
            for (Price p = 0; p < WIDTH; ++p) CHECK(book.level_qty(BASE + p) == depth[p]);
        }
    }
    std::puts("  reference model (36K operations) OK");
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
    test_book_invalid_inputs();
    test_advanced_orders();
    test_risk_boundaries();
    test_randomized_reference();
    test_itch_parser();
    test_spsc_ring();
    std::puts("ALL TESTS PASSED");
    return 0;
}
