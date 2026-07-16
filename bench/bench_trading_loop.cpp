// ============================================================================
// bench_trading_loop.cpp -- PHASE 3: the closed trading loop, end to end.
//
//     ITCH capture (mock network)                        [feed core]
//        --raw frames--> zero-copy arena
//        --> decode + match + BBO diff                   [engine core]
//        --BboUpdate--> SPSC market-data ring
//        --> mock market-making strategy                 [strategy core]
//        --NewOrderSingle (Message)--> SPSC order ring
//        --> engine applies the strategy's quote         [engine core]
//
// TICK-TO-TRADE: the RDTSC stamp taken when the CAUSING packet entered the
// pipeline rides inside the BboUpdate (origin_tsc), the strategy copies it
// into its order message, and the engine stamps again after the quote is
// live in the book. Delta = packet-in -> book-updated -> BBO-published ->
// strategy-decided -> order-returned -> quote-resting. The full loop.
//
// WHY THE STRATEGY CANNOT BREAK FEED VERIFICATION
// -----------------------------------------------
// The generated feed is internally consistent (every E/X/D references a
// live feed order), and its adds never cross: feed bids sit strictly below
// MID, feed asks strictly above. The mock market maker quotes ONE TICK
// BEHIND the touch (bid_px-1 / ask_px+1), so its orders (a) are never
// crossed by feed flow, (b) never cross feed flow themselves, and (c) are
// never referenced by feed cancels/executes. Live strategy trading
// therefore leaves the feed's expected fill counts EXACTLY intact --
// [VERIFIED] still must hold, which proves the loop did not corrupt the
// book. (Quoting behind the touch -- rather than joining it -- also keeps
// the loop LIVE: joining and capping at MID pins the BBO at the strategy's
// own quote and the market freezes after a handful of updates.)
//
// HOT-PATH GUARANTEES (per the phase constraints)
// -----------------------------------------------
// * zero heap: all rings/arenas/sample buffers are static or pre-allocated
//   before timing; the strategy and engine loops perform no allocation.
// * lock-free: three SPSC rings (arena in, BBO out, orders in) -- acquire/
//   release only, no mutexes anywhere.
// * alignas(64): arena slots, ring indices, and BboUpdate each own their
//   cache line (see arena_ring.hpp / spsc_ring.hpp / bbo.hpp).
// ============================================================================

#include "pipeline_common.hpp"
#include "lob/arena_ring.hpp"
#include "lob/bbo.hpp"
#include <atomic>
#include <thread>

using namespace lob;
using namespace lob::bench;

using Arena    = SpscArenaRing<64, 1u << 14>;   // mock wire -> engine
using BboRing  = SpscRing<BboUpdate, 1u << 15>; // engine -> strategy (2 MB)
using OrdRing  = SpscRing<Message,   1u << 12>; // strategy -> engine

static Arena   g_arena;    // all static: .bss, zero heap, page-touched below
static BboRing g_bbo;
static OrdRing g_orders;

// ---------------------------------------------------------------------------
// Engine-side strategy-order desk: at most one resting quote per side,
// cancel-and-replace. The engine owns the book, so IT holds the OrderIds --
// the strategy never needs an execution-report round trip to manage quotes.
// ---------------------------------------------------------------------------
struct QuoteDesk {
    Book&   book;
    OrderId bid_id = 0, ask_id = 0;
    std::uint64_t quotes = 0;

    LOB_FORCE_INLINE void apply(const Message& m) noexcept {
        OrderId& live = (m.side == Side::Buy) ? bid_id : ask_id;
        if (live) book.cancel(live);              // cancel-and-replace
        live = book.new_order(m.side, m.price, m.qty,
                              [](const Trade&) noexcept {});
        ++quotes;
    }
};

// ===========================================================================
int main() {
    pin_thread(2);
    const double ghz = calibrate_tsc_ghz();
    std::printf("TSC calibration: %.3f GHz\n", ghz);

    std::puts("\n== SETUP (cold path) ==");
    const FeedStats st = generate_feed(FEED_FILE, N_MSGS, 0x17C4);

    std::size_t feed_len = 0;
    const std::byte* feed = map_file(FEED_FILE, feed_len);
    if (!feed) { std::puts("mmap failed"); return 1; }
    std::uint64_t sink_sum = 0;                       // pre-touch mapping
    for (std::size_t i = 0; i < feed_len; i += 4096)
        sink_sum += std::uint8_t(feed[i]);
    std::printf("  mapped %zu bytes (checksum %llu)\n",
                feed_len, (unsigned long long)sink_sum);

    // Pre-touch the arena so the timed run never soft-faults.
    for (std::size_t i = 0; i < (1u << 14); ++i) {
        std::byte* s = g_arena.try_acquire();
        if (!s) break;
        s[0] = std::byte{0};
        g_arena.publish();
    }
    while (g_arena.try_front()) g_arena.release();

    // Pre-allocated (cold) buffers: producer stamps + tick-to-trade samples.
    static std::vector<std::uint64_t> t0(N_MSGS);
    static std::vector<std::uint32_t> t2t;
    t2t.resize(2 * N_MSGS);                    // worst case: 2 quotes/update
    std::memset(t2t.data(), 0, t2t.size() * sizeof(std::uint32_t));

    // Pace the mock wire at a rate this host can drain without queue build-
    // up: latency then measures the CODE, not queue depth. (Real ITCH peaks
    // are ~1-2M msgs/sec; burst absorption was proven in bench_zero_copy.)
    const std::uint64_t gap = std::uint64_t(400.0 * ghz);   // 400ns => 2.5M/s
    std::printf("  pacing: %.0f ns inter-arrival (%.2f M msgs/sec)\n",
                gap / ghz, 1e3 / (gap / ghz));

    std::atomic<bool> stop{false};
    std::atomic<bool> strat_ready{false};

    // Engine state MUST be fully constructed (and its ~70MB of arena/table
    // memory pre-touched) BEFORE any thread starts, or the feed runs into a
    // stalled engine and every early sample measures allocator startup.
    Gateway gw(st.max_ref);
    QuoteDesk desk{*gw.book};
    BboPublisher<Book, BboRing> bbo(*gw.book, g_bbo);

    // =======================================================================
    // FEED THREAD (core 0) -- the mock network layer: lands raw ITCH frames
    // in stationary arena slots, stamping pipeline entry into t0[].
    // Waits for the strategy thread to be scheduled and pinned before the
    // first packet, so thread-startup time never pollutes the samples.
    // =======================================================================
    std::thread feeder([&] {
        pin_thread(0);
        while (!strat_ready.load(std::memory_order_acquire)) { }
        const std::byte* cur = feed;
        std::uint64_t next = __rdtsc() + gap;
        for (std::size_t i = 0; i < N_MSGS; ++i) {
            while (__rdtsc() < next) { }               // pace the wire
            next += gap;
            t0[i] = __rdtsc();                         // pipeline entry
            const std::size_t n = 2 + itch::load_be16(cur);
            std::byte* slot;
            while (!(slot = g_arena.try_acquire())) { }
            std::memcpy(slot, cur, n);                 // "NIC DMA"
            g_arena.publish();
            cur += n;
        }
    });

    // =======================================================================
    // STRATEGY THREAD (core 4) -- mock market maker. Consumes BBO, keeps a
    // one-lot quote joined to each side of the top, clamped to [<=MID] /
    // [>=MID+1] (see header comment). Sends cancel-and-replace instructions
    // through the order ring; origin_tsc rides in Message.id so the engine
    // can stamp tick-to-trade on arrival.
    // =======================================================================
    std::thread strategy([&] {
        pin_thread(4);
        Price my_bid = -1, my_ask = -1;                // last quoted prices
        BboUpdate u;
        std::uint64_t gaps = 0, last_seq = 0;
        strat_ready.store(true, std::memory_order_release);
        while (true) {
            if (!g_bbo.try_pop(u)) {
                if (stop.load(std::memory_order_acquire)) break;
                continue;
            }
            gaps += (last_seq && u.seq != last_seq + 1); // conflation events
            last_seq = u.seq;

            // Quote engine: queue-imbalance market making. Under buy
            // pressure (more size resting at the bid) join the bid touch
            // and step the ask back; under sell pressure, mirror. Quotes
            // never exceed the touch, and the feed's touch never crosses
            // MID, so strategy flow can never cross feed flow. Imbalance
            // flips constantly => a realistic stream of cancel-replaces.
            const bool buy_pressure = u.bid_qty >= u.ask_qty;
            if (u.bid_px >= 0) {
                const Price want = buy_pressure ? u.bid_px : u.bid_px - 1;
                if (want != my_bid) {
                    my_bid = want;
                    Message m{};
                    m.type = MsgType::NewOrder; m.side = Side::Buy;
                    m.price = want; m.qty = 1;
                    m.id = u.origin_tsc;               // tick-to-trade origin
                    while (!g_orders.try_push(m)) { }
                }
            }
            if (u.ask_px >= 0) {
                const Price want = buy_pressure ? u.ask_px + 1 : u.ask_px;
                if (want != my_ask) {
                    my_ask = want;
                    Message m{};
                    m.type = MsgType::NewOrder; m.side = Side::Sell;
                    m.price = want; m.qty = 1;
                    m.id = u.origin_tsc;
                    while (!g_orders.try_push(m)) { }
                }
            }
        }
        std::printf("  strategy: %llu conflation gaps seen\n",
                    (unsigned long long)gaps);
    });

    // =======================================================================
    // ENGINE THREAD (this core) -- drains BOTH inbound rings. Strategy
    // orders are checked first: the firm's own flow is the latency-critical
    // path. After every feed message, the BBO publisher diffs the top.
    // =======================================================================
    std::size_t done = 0, n_t2t = 0;
    const std::uint64_t run0 = tsc_begin();
    while (done < N_MSGS) {
        // ---- priority 1: our own orders (tick-to-trade completes here) ----
        Message q;
        if (g_orders.try_pop(q)) {
            desk.apply(q);
            t2t[n_t2t++] = std::uint32_t(__rdtsc() - q.id);
        }
        // ---- priority 2: market data off the wire ----
        const std::byte* p = g_arena.try_front();
        if (!p) continue;
        itch::ItchParser one(p, std::size_t(2) + itch::load_be16(p));
        one.next(GatewaySink{gw});
        g_arena.release();
        bbo.on_message_applied(t0[done]);   // diff top-of-book, maybe publish
        ++done;
    }
    const std::uint64_t run1 = tsc_end();
    feeder.join();
    stop.store(true, std::memory_order_release);
    strategy.join();
    // Late quotes still in flight after the feed ended: apply, don't time.
    { Message q; while (g_orders.try_pop(q)) desk.apply(q); }

    // =======================================================================
    // REPORT
    // =======================================================================
    const double run_ns = (run1 - run0) / ghz;
    std::printf("\n== CLOSED LOOP: %zu feed msgs in %.1f ms (%.2f M msgs/s) ==\n",
                N_MSGS, run_ns / 1e6, N_MSGS / (run_ns / 1e9) / 1e6);
    std::printf("  feed fills   : %llu (expected %llu)  %s\n",
                (unsigned long long)gw.trades, (unsigned long long)st.execs,
                gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]");
    std::printf("  BBO updates  : %llu published, %llu conflated-away\n",
                (unsigned long long)bbo.published(),
                (unsigned long long)bbo.dropped());
    std::printf("  strategy     : %llu quote instructions applied\n",
                (unsigned long long)desk.quotes);

    if (n_t2t > 0) {
        std::vector<std::uint32_t> samples(t2t.begin(), t2t.begin() + n_t2t);
        std::puts("\n  TICK-TO-TRADE (packet-in -> match -> BBO -> strategy "
                  "-> quote resting):");
        report("tick-to-trade", samples, ghz);
    }

    std::puts("\nbenchmark complete.");
    return 0;
}
