// Mixed reserve-order lifecycle oracle. All inputs are deterministic/synthetic.
#include "lob/order_book.hpp"
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <random>
#include <vector>

using namespace lob;
static unsigned current_seed;
static int current_step;
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "seed=%u step=%d: %s (%d)\n", current_seed, current_step, #x, __LINE__); std::abort(); } } while (0)

struct Resting { OrderId id; Side side; Price price; Qty visible, hidden, peak; };
struct Reference {
    std::vector<Resting> orders;
    std::vector<Trade> trades;
    static constexpr std::size_t capacity = 32;

    std::size_t maker(Side taker, Price limit) const {
        std::size_t found = orders.size();
        for (std::size_t i = 0; i < orders.size(); ++i) {
            const auto& o = orders[i];
            if (o.side == taker || (taker == Side::Buy ? o.price > limit : o.price < limit)) continue;
            if (found == orders.size() || (taker == Side::Buy ? o.price < orders[found].price : o.price > orders[found].price)) found = i;
        }
        return found;
    }
    bool add(OrderId actual, Side side, Price price, Qty total, Qty peak, bool post) {
        trades.clear();
        if (post && maker(side, price) != orders.size()) return false;
        while (total > 0) {
            const auto i = maker(side, price);
            if (i == orders.size()) break;
            auto& o = orders[i];
            const Qty fill = std::min(total, o.visible);
            trades.push_back({o.id, 0, o.price, fill});
            total -= fill;
            o.visible -= fill;
            if (o.visible == 0) {
                auto next = o;
                orders.erase(orders.begin() + static_cast<std::ptrdiff_t>(i));
                if (next.hidden) {
                    next.visible = std::min(next.peak, next.hidden);
                    next.hidden -= next.visible;
                    orders.push_back(next);
                }
            }
        }
        if (!total) { CHECK(actual == ~OrderId{0}); return true; }
        if (orders.size() == capacity) return false;
        const Qty visible = peak ? std::min(peak, total) : total;
        orders.push_back({actual, side, price, visible, total - visible, peak});
        return true;
    }
    std::size_t find(OrderId id) const {
        for (std::size_t i = 0; i < orders.size(); ++i) if (orders[i].id == id) return i;
        return orders.size();
    }
    bool cancel(OrderId id) {
        const auto i = find(id);
        if (i == orders.size()) return false;
        orders.erase(orders.begin() + static_cast<std::ptrdiff_t>(i));
        return true;
    }
    Qty remove(OrderId id, Qty quantity, bool execution) {
        auto i = find(id);
        if (i == orders.size() || quantity <= 0) return 0;
        const Qty removed = std::min(quantity, orders[i].visible + orders[i].hidden);
        if (!execution) {
            const Qty hidden = std::min(removed, orders[i].hidden);
            orders[i].hidden -= hidden;
            orders[i].visible -= removed - hidden;
            if (!orders[i].visible) cancel(id);
            return removed;
        }
        Qty remaining = removed;
        while (remaining) {
            i = find(id);
            auto& o = orders[i];
            const Qty fill = std::min(remaining, o.visible);
            o.visible -= fill;
            remaining -= fill;
            if (!o.visible) {
                auto next = o;
                cancel(id);
                if (next.hidden) {
                    next.visible = std::min(next.peak, next.hidden);
                    next.hidden -= next.visible;
                    orders.push_back(next);
                }
            }
        }
        return removed;
    }
};

static void mixed_lifecycle() {
    constexpr unsigned seeds[] = {1, 7, 42, 731, 2026, 65537, 99017, 20261029};
    for (unsigned seed : seeds) {
        current_seed = seed;
        std::mt19937 rng(seed);
        OrderBook<64, Reference::capacity> book(100);
        Reference ref;
        std::vector<OrderId> history;
        std::vector<Trade> observed;
        auto collect = [&](const Trade& t) { observed.push_back(t); };
        for (int step = 0; step < 5000; ++step) {
            current_step = step;
            const auto action = rng() % 8;
            if (action < 4 || history.empty()) {
                const Side side = rng() % 2 ? Side::Buy : Side::Sell;
                const Price price = 100 + rng() % 64;
                const Qty qty = 1 + rng() % 40;
                const Qty peak = action < 2 ? 1 + rng() % 12 : 0;
                const bool post = action == 3;
                observed.clear();
                const auto id = peak ? book.new_iceberg_order(side, price, peak, qty, collect)
                                     : book.new_order(side, price, qty, collect, post);
                CHECK((id != 0) == ref.add(id, side, price, qty, peak, post));
                CHECK(observed.size() == ref.trades.size());
                for (std::size_t i = 0; i < observed.size(); ++i) {
                    CHECK(observed[i].maker_id == ref.trades[i].maker_id);
                    CHECK(observed[i].taker_id == 0);
                    CHECK(observed[i].price == ref.trades[i].price);
                    CHECK(observed[i].qty == ref.trades[i].qty);
                }
                history.push_back(id);
            } else {
                // Exercise both live and stale IDs, including slot reuse.
                const bool live = !ref.orders.empty() && rng() % 4 != 0;
                const auto id = live ? ref.orders[rng() % ref.orders.size()].id : history[rng() % history.size()];
                const Qty qty = static_cast<Qty>(rng() % 60) - 2;
                if (action == 4) CHECK(book.cancel(id) == ref.cancel(id));
                else if (action == 5) CHECK(book.reduce(id, qty) == ref.remove(id, qty, false));
                else CHECK(book.execute(id, qty) == ref.remove(id, qty, true));
            }
            Price bid = -1, ask = -1;
            for (Price p = 100; p < 164; ++p) {
                Qty total = 0;
                for (const auto& o : ref.orders) {
                    if (o.price == p) total += o.visible;
                    if (o.side == Side::Buy && o.price > bid) bid = o.price;
                    if (o.side == Side::Sell && (ask == -1 || o.price < ask)) ask = o.price;
                }
                CHECK(book.level_qty(p) == total);
            }
            CHECK(book.best_bid() == bid);
            CHECK(book.best_ask() == ask);
            CHECK(bid == -1 || ask == -1 || bid < ask);
        }
    }
    std::puts("mixed iceberg lifecycle: 40000 oracle operations passed");
}

static void integer_boundaries() {
    const auto noop = [](const Trade&) {};
    constexpr Price pmin = std::numeric_limits<Price>::min();
    constexpr Price pmax = std::numeric_limits<Price>::max();
    constexpr Qty qmax = std::numeric_limits<Qty>::max();
    for (Price base : {pmin, pmax - 63}) {
        OrderBook<64, 2> book(base);
        const auto a = book.new_iceberg_order(Side::Sell, base + 63, 3, qmax, noop);
        CHECK(a != 0);
        CHECK(book.new_order(Side::Sell, base + 63, 1, noop) == 0);
        CHECK(book.best_ask() == base + 63);
        CHECK(book.execute(a, qmax - 2) == qmax - 2);
        // qmax % 3 == 1: the final two lots straddle two reserve clips.
        CHECK(book.level_qty(base + 63) == 1);
        CHECK(book.reduce(a, 1) == 1);
        CHECK(book.level_qty(base + 63) == 1);
        std::vector<Trade> trades;
        CHECK(book.new_order(Side::Buy, base + 63, 1, [&](const Trade& t) { trades.push_back(t); }) == ~OrderId{0});
        CHECK(trades.size() == 1 && trades[0].price == base + 63 && trades[0].qty == 1);
        CHECK(!book.cancel(a));
        const auto b = book.new_order(Side::Buy, base, qmax, noop);
        CHECK(b != 0 && b != a);
        CHECK(book.best_bid() == base);
        CHECK(book.execute(b, qmax) == qmax);
        CHECK(book.level_qty(base) == 0);
    }
    std::puts("extreme-price / maximum-reserve lifecycle: passed");
}

static void pool_pressure() {
    OrderBook<64, 2> book(100);
    std::vector<Trade> trades;
    auto collect = [&](const Trade& t) { trades.push_back(t); };
    const auto first = book.new_iceberg_order(Side::Sell, 110, 2, 7, collect);
    const auto second = book.new_order(Side::Sell, 111, 3, collect);
    CHECK(first != 0 && second != 0);
    CHECK(book.new_order(Side::Buy, 109, 4, collect) == 0);
    CHECK(book.new_iceberg_order(Side::Buy, 109, 1, 4, collect) == 0);
    CHECK(book.level_qty(110) == 2 && book.level_qty(111) == 3);
    CHECK(book.best_bid() == -1 && book.best_ask() == 110);
    // A full pool still accepts a taker and reuses a slot freed by matching.
    const auto remainder = book.new_order(Side::Buy, 111, 12, collect);
    CHECK(remainder != 0 && remainder != ~OrderId{0});
    CHECK(trades.size() == 5);
    for (std::size_t i = 0; i < 4; ++i) CHECK(trades[i].maker_id == first);
    CHECK(trades[4].maker_id == second && trades[4].qty == 3);
    CHECK(book.best_ask() == -1 && book.best_bid() == 111);
    CHECK(book.level_qty(111) == 2);
    CHECK(!book.cancel(first) && !book.cancel(second));
    CHECK(book.reduce(first, 1) == 0 && book.execute(second, 1) == 0);
    CHECK(book.cancel(remainder));
    CHECK(book.level_qty(111) == 0 && book.best_bid() == -1);
    std::puts("pool exhaustion / matching slot reuse: passed");
}

int main() {
    mixed_lifecycle();
    integer_boundaries();
    pool_pressure();
}
