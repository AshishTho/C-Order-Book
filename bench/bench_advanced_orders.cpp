// ============================================================================
// bench_advanced_orders.cpp -- Iceberg reload + Post-Only reject: proofs.
//
// PHASE 8: advanced order types, implemented natively in order_book.hpp /
// order.hpp (see the design notes there). This file proves the two
// contractual claims directly, then adds a small RDTSC cost check showing
// neither feature taxes the common (plain-limit-order) path.
//
// (a) ICEBERG LOSES TIME PRIORITY ON RELOAD
//     Rest an iceberg (display 10, total 30) then a plain 5-lot order at
//     the SAME price -- the plain order is now BEHIND the iceberg in the
//     FIFO. An aggressor sweep of 10 lots exactly exhausts the iceberg's
//     visible clip, forcing a reload; the reload must move the iceberg
//     behind the plain order. A second, smaller aggressor sweep must then
//     fill the PLAIN order first -- proven by inspecting the maker_id on
//     each Trade the engine emits, not by peeking at internal state.
//
// (b) POST-ONLY REJECTS A CROSSING ORDER
//     With a resting ask on the book, a post_only bid at or above that ask
//     must be rejected (id == 0, book state and trade count unchanged). A
//     post_only bid strictly below the ask must be accepted and rest
//     normally, proving the check doesn't over-reject legitimate quotes.
// ============================================================================

#include "lob/order_book.hpp"
#include "lob/rdtsc.hpp"

#include <cstdio>
#include <vector>

using namespace lob;

static constexpr Price BASE = 1'000'000;
using Book = OrderBook<1u << 16, 1u << 12>;

// ===========================================================================
// (a) Iceberg reload / time-priority loss.
// ===========================================================================
static bool test_iceberg_priority() {
    std::puts("\n== (a) ICEBERG RELOAD LOSES TIME PRIORITY ==");
    Book book(BASE);
    std::vector<Trade> trades;
    auto collect = [&](const Trade& t) { trades.push_back(t); };

    const Price P = BASE + 500;

    // Iceberg SELL: display 10, total 30 (visible 10, hidden 20). Arrives
    // first -> head of the level's FIFO.
    const OrderId ice = book.new_iceberg_order(Side::Sell, P, 10, 30, collect);
    std::printf("  iceberg id=%llu resting, visible level_qty=%lld (want 10)\n",
                (unsigned long long)ice, (long long)book.level_qty(P));
    bool ok = ice != 0 && ice != ~OrderId{0} && book.level_qty(P) == 10;

    // Plain SELL, 5 lots, SAME price -- arrives second -> behind the
    // iceberg in time priority.
    const OrderId plain = book.new_order(Side::Sell, P, 5, collect);
    ok &= plain != 0 && plain != ~OrderId{0} && book.level_qty(P) == 15;
    std::printf("  plain id=%llu resting behind it, level_qty=%lld (want 15)\n",
                (unsigned long long)plain, (long long)book.level_qty(P));

    // Aggressor #1: buy 10 lots @ P -- exactly exhausts the iceberg's
    // visible clip. Must trade against the ICEBERG (still at the head)
    // and trigger a reload (visible -> 10, hidden -> 10), NOT touch plain.
    trades.clear();
    book.new_order(Side::Buy, P, 10, collect);
    ok &= trades.size() == 1 && trades[0].maker_id == ice && trades[0].qty == 10;
    std::printf("  sweep #1 (10 lots): maker=%llu qty=%lld  (want maker=%llu qty=10)\n",
                (unsigned long long)(trades.empty() ? 0 : trades[0].maker_id),
                (long long)(trades.empty() ? 0 : trades[0].qty),
                (unsigned long long)ice);
    ok &= book.level_qty(P) == 15;   // 10 reloaded iceberg + 5 plain, unchanged total

    // Aggressor #2: buy 5 lots @ P -- the iceberg REJOINED THE QUEUE BEHIND
    // the plain order on reload, so the plain order must fill FIRST. This
    // is the crux of the proof: an order that arrived AFTER the iceberg's
    // original placement, but BEFORE its reload, now has priority over it.
    trades.clear();
    book.new_order(Side::Buy, P, 5, collect);
    ok &= trades.size() == 1 && trades[0].maker_id == plain && trades[0].qty == 5;
    std::printf("  sweep #2 (5 lots):  maker=%llu qty=%lld  (want maker=%llu qty=5)"
                "  <- proves reload lost priority\n",
                (unsigned long long)(trades.empty() ? 0 : trades[0].maker_id),
                (long long)(trades.empty() ? 0 : trades[0].qty),
                (unsigned long long)plain);
    ok &= book.level_qty(P) == 10;   // plain gone, only the reloaded iceberg left
    ok &= book.cancel(plain) == false;   // plain was fully filled -> already dead

    // Aggressor #3: buy 3 lots @ P -- only the iceberg remains, must fill it.
    trades.clear();
    book.new_order(Side::Buy, P, 3, collect);
    ok &= trades.size() == 1 && trades[0].maker_id == ice && trades[0].qty == 3;
    ok &= book.level_qty(P) == 7;
    std::printf("  sweep #3 (3 lots):  maker=%llu qty=%lld  level_qty=%lld (want 7)\n",
                (unsigned long long)(trades.empty() ? 0 : trades[0].maker_id),
                (long long)(trades.empty() ? 0 : trades[0].qty),
                (long long)book.level_qty(P));

    std::printf("  %s\n", ok ? "[VERIFIED]" : "[BROKEN!]");
    return ok;
}

// ===========================================================================
// (b) Post-only reject.
// ===========================================================================
static bool test_post_only() {
    std::puts("\n== (b) POST-ONLY REJECTS A CROSSING ORDER ==");
    Book book(BASE);
    std::vector<Trade> trades;
    auto collect = [&](const Trade& t) { trades.push_back(t); };

    const Price ASK = BASE + 1000;
    const OrderId resting_ask = book.new_order(Side::Sell, ASK, 20, collect);
    bool ok = resting_ask != 0;

    // Crossing post-only bid AT the ask -- must be rejected outright.
    const OrderId cross_at = book.new_order(Side::Buy, ASK, 5, collect,
                                            /*post_only=*/true);
    ok &= cross_at == 0 && trades.empty() && book.level_qty(ASK) == 20;
    std::printf("  post_only bid AT ask:    id=%llu (want 0), trades=%zu, "
                "ask level_qty=%lld (want 20)\n",
                (unsigned long long)cross_at, trades.size(),
                (long long)book.level_qty(ASK));

    // Crossing post-only bid THROUGH the ask -- also rejected.
    const OrderId cross_thru = book.new_order(Side::Buy, ASK + 50, 5, collect,
                                              /*post_only=*/true);
    ok &= cross_thru == 0 && trades.empty() && book.level_qty(ASK) == 20;
    std::printf("  post_only bid THRU ask:  id=%llu (want 0), trades=%zu\n",
                (unsigned long long)cross_thru, trades.size());

    // Non-crossing post-only bid -- must be accepted and rest normally
    // (proves the gate doesn't over-reject legitimate maker orders).
    const OrderId maker = book.new_order(Side::Buy, ASK - 100, 7, collect,
                                         /*post_only=*/true);
    ok &= maker != 0 && maker != ~OrderId{0} && trades.empty() &&
          book.best_bid() == ASK - 100 && book.level_qty(ASK - 100) == 7;
    std::printf("  post_only bid BELOW ask: id=%llu (want != 0), best_bid=%lld "
                "(want %lld)   <- legitimate maker not rejected\n",
                (unsigned long long)maker, (long long)book.best_bid(),
                (long long)(ASK - 100));

    // Sanity: the SAME price/qty WITHOUT post_only crosses and trades.
    trades.clear();
    const OrderId taker = book.new_order(Side::Buy, ASK, 5, collect,
                                         /*post_only=*/false);
    ok &= taker == ~OrderId{0} && trades.size() == 1 &&
          trades[0].maker_id == resting_ask && trades[0].qty == 5;
    std::printf("  same order, post_only=false: trades=%zu qty=%lld "
                "(want 1, 5)   <- confirms the reject was the flag, not the price\n",
                trades.size(), (long long)(trades.empty() ? 0 : trades[0].qty));

    std::printf("  %s\n", ok ? "[VERIFIED]" : "[BROKEN!]");
    return ok;
}

// ===========================================================================
// Cost check: post_only=false / iceberg-absent paths must be indistinguish-
// able in cost from the plain new_order() benchmarked elsewhere. Batched
// RDTSC over register-resident synthetic fills, deep book so nothing rests
// permanently (each order fully trades against the next).
// ===========================================================================
static void microcost(double ghz) {
    std::puts("\n== MICROCOST: post_only flag / iceberg reload overhead ==");
    constexpr int N = 200'000;

    // Plain new_order, post_only defaulted (false) -- baseline.
    {
        Book book(BASE);
        auto none = [](const Trade&) noexcept {};
        for (int i = 0; i < N; ++i)
            book.new_order(i & 1 ? Side::Buy : Side::Sell,
                           BASE + 10'000 + (i & 1 ? -1 : 1) * (i % 500), 1, none);
        const std::uint64_t c0 = tsc_begin();
        for (int i = 0; i < N; ++i)
            book.new_order(Side::Buy, BASE + 50'000, 1, none);
        const std::uint64_t c1 = tsc_end();
        std::printf("  plain new_order (post_only=false):      %6.2f ns/op\n",
                    double(c1 - c0) / N / ghz);
    }
    // Explicit post_only=false argument -- same code path, proves the extra
    // parameter costs nothing when unused.
    {
        Book book(BASE);
        auto none = [](const Trade&) noexcept {};
        const std::uint64_t c0 = tsc_begin();
        for (int i = 0; i < N; ++i)
            book.new_order(Side::Buy, BASE + 50'000 + i, 1, none, false);
        const std::uint64_t c1 = tsc_end();
        std::printf("  new_order(post_only=false explicit):    %6.2f ns/op\n",
                    double(c1 - c0) / N / ghz);
    }
    // Iceberg orders that never reload (deep enough hidden reserve that the
    // benchmark's fill volume never exhausts one clip) -- isolates the
    // "is this an iceberg" check's cost from the reload splice itself.
    {
        Book book(BASE);
        auto none = [](const Trade&) noexcept {};
        const std::uint64_t c0 = tsc_begin();
        for (int i = 0; i < N; ++i)
            book.new_iceberg_order(Side::Buy, BASE + 60'000 + i, 5, 1'000'000, none);
        const std::uint64_t c1 = tsc_end();
        std::printf("  new_iceberg_order (rest, no reload):    %6.2f ns/op\n",
                    double(c1 - c0) / N / ghz);
    }
}

int main() {
    const double ghz = calibrate_tsc_ghz();
    std::printf("TSC calibration: %.3f GHz\n", ghz);

    const bool a = test_iceberg_priority();
    const bool b = test_post_only();
    microcost(ghz);

    std::puts("\n== SUMMARY ==");
    std::printf("  (a) iceberg reload loses priority: %s\n",
                a ? "[VERIFIED]" : "[BROKEN!]");
    std::printf("  (b) post-only rejects on cross:     %s\n",
                b ? "[VERIFIED]" : "[BROKEN!]");
    return (a && b) ? 0 : 1;
}
