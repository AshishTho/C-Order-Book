// ============================================================================
// bench_risk.cpp -- RDTSC proof of the pre-trade risk gateway's footprint.
//
// CLAIM UNDER TEST: RiskGateway::check() costs < 20ns per order on the
// accept path (the path every real order takes).
//
// METHOD
// ------
// Measuring a ~5ns function with a ~25ns serialised RDTSC pair per call
// would measure the clock, not the code. Two complementary views instead:
//
//   1. BATCHED AVERAGE: one tsc_begin/tsc_end pair around 10M back-to-back
//      calls; total/N. Amortises the clock to noise and reflects the
//      STEADY-STATE cost with warm caches + trained predictor -- exactly
//      the regime a quoting strategy runs in.
//
//   2. BLOCK PERCENTILES: time blocks of 64 calls, subtract the calibrated
//      RDTSC-pair overhead, divide by 64. Exposes tail events (TLB miss,
//      stamp-ring line refill, interrupt) that an average hides.
//
// The input stream (sides / prices / qtys) is pre-generated into small
// power-of-two tables and consumed with AND-indexing, so the timed loop
// contains the gateway and nothing else. A volatile sink consumes every
// verdict so the optimiser cannot delete the work.
//
// The REJECT paths are timed separately (batched) with always-violating
// inputs -- they mispredict the final branch by construction, which is the
// worst case and still lands ~tens of ns.
// ============================================================================

#include "lob/risk.hpp"
#include "lob/rdtsc.hpp"
#include "pipeline_common.hpp"   // pin_thread, report(), Pcts

using namespace lob;
using namespace lob::bench;

static constexpr std::size_t N_CALLS  = 10'000'000;
static constexpr std::size_t TAB      = 1u << 16;      // input table size
static constexpr std::size_t BLOCK    = 64;            // percentile grain

volatile std::uint32_t g_sink;      // defeats dead-code elimination

int main() {
    pin_thread(2);
    const double        ghz = calibrate_tsc_ghz();
    const std::uint64_t ovh = measure_tsc_overhead();
    std::printf("TSC calibration: %.3f GHz, rdtsc pair overhead: %llu cycles\n",
                ghz, (unsigned long long)ovh);

    // ---- pre-generated in-band order stream (cold path) -------------------
    static Side  sides [TAB];
    static Price prices[TAB];
    static Qty   qtys  [TAB];
    Rng r(0x515Cull);
    for (std::size_t i = 0; i < TAB; ++i) {
        const std::uint64_t v = r.next();
        sides [i] = (v & 1) ? Side::Buy : Side::Sell;
        prices[i] = BASE + Price(v % 60'000);            // inside the collar
        qtys  [i] = 1 + Qty((v >> 8) & 63);              // 1..64 lots
    }

    // Gateway limits chosen so the stream is 100% ACCEPT: this measures the
    // real-traffic path. (Rejects are timed separately below.)
    // window: 256 msgs per 1ns -- i.e. rate limiting armed but untriggerable,
    // so the stamp-ring load+compare+store is still fully exercised.
    RiskGateway<256> gw(/*max_qty*/ 64, /*collar*/ BASE, BASE + 65'535,
                        /*pos_limit*/ Qty(1) << 60, /*window_ticks*/ 1);

    // =======================================================================
    // 1. BATCHED AVERAGE -- accept path.
    // =======================================================================
    std::uint32_t acc = 0;
    const std::uint64_t now0 = __rdtsc();
    const std::uint64_t a0 = tsc_begin();
    for (std::size_t i = 0; i < N_CALLS; ++i) {
        const std::size_t k = i & (TAB - 1);
        // `now` advances monotonically without an rdtsc per call (a real
        // strategy passes the stamp it already took for tick-to-trade).
        acc |= gw.check(sides[k], prices[k], qtys[k], now0 + i);
    }
    const std::uint64_t a1 = tsc_end();
    g_sink = acc;

    const double cyc_per = double(a1 - a0) / N_CALLS;
    std::printf("\n== ACCEPT PATH (batched, %zu calls) ==\n", N_CALLS);
    std::printf("  %.2f cycles/order  =  %.2f ns/order   [%s 20ns budget]\n",
                cyc_per, cyc_per / ghz,
                cyc_per / ghz < 20.0 ? "MEETS" : "MISSES");
    std::printf("  accepted %llu, rejected %llu (expected 0 rejects)\n",
                (unsigned long long)gw.accepted(),
                (unsigned long long)gw.rejected());

    // =======================================================================
    // 2. BLOCK PERCENTILES -- accept path tails.
    // =======================================================================
    {
        static std::vector<std::uint32_t> samples(N_CALLS / BLOCK);
        RiskGateway<256> gw2(64, BASE, BASE + 65'535, Qty(1) << 60, 1);
        std::uint32_t acc2 = 0;
        for (std::size_t b = 0; b < N_CALLS / BLOCK; ++b) {
            const std::uint64_t t0 = tsc_begin();
            for (std::size_t j = 0; j < BLOCK; ++j) {
                const std::size_t k = (b * BLOCK + j) & (TAB - 1);
                acc2 |= gw2.check(sides[k], prices[k], qtys[k], t0 + j);
            }
            const std::uint64_t t1 = tsc_end();
            const std::uint64_t net = (t1 - t0 > ovh) ? t1 - t0 - ovh : 0;
            // stored per-block; report() prints cycles and ns per ORDER
            samples[b] = std::uint32_t(net / BLOCK);
        }
        g_sink = acc2;
        std::puts("\n== ACCEPT PATH (per-order, 64-call blocks, overhead-"
                  "subtracted) ==");
        report("risk check", samples, ghz);
    }

    // =======================================================================
    // 3. REJECT PATHS -- batched, always-violating inputs (worst case:
    //    final branch mispredicts, reason mask non-zero).
    // =======================================================================
    std::puts("\n== REJECT PATHS (batched) ==");
    struct Case { const char* name; Side s; Price p; Qty q; };
    const Case cases[] = {
        { "fat finger (qty)   ", Side::Buy,  BASE + 100,   9999 },
        { "price collar       ", Side::Sell, BASE - 5000,  1    },
        { "both (mask = QTY|PB)", Side::Buy, BASE - 5000,  9999 },
    };
    for (const Case& c : cases) {
        RiskGateway<256> gwr(64, BASE, BASE + 65'535, Qty(1) << 60, 1);
        std::uint32_t m = 0;
        const std::uint64_t r0 = tsc_begin();
        for (std::size_t i = 0; i < N_CALLS / 10; ++i)
            m |= gwr.check(c.s, c.p, c.q, now0 + i);
        const std::uint64_t r1 = tsc_end();
        g_sink = m;
        std::printf("  %s reason=0x%X  %.2f ns/order\n", c.name, m,
                    double(r1 - r0) / (N_CALLS / 10) / ghz);
    }

    // =======================================================================
    // 4. RATE LIMITER -- functional proof: window depth 4, wide window =>
    //    5th rapid order must bounce with RJ_RATE; after the window passes,
    //    orders flow again.
    // =======================================================================
    {
        const std::uint64_t win = std::uint64_t(1000.0 * ghz);   // 1us window
        RiskGateway<4> gwr(64, BASE, BASE + 65'535, Qty(1) << 60, win);
        std::uint64_t t = __rdtsc();
        std::uint32_t fifth = 0;
        for (int i = 0; i < 4; ++i)
            if (gwr.check(Side::Buy, BASE + 1, 1, t + i) != 0) fifth = 99;
        fifth |= gwr.check(Side::Buy, BASE + 1, 1, t + 4);        // 5th: over
        const std::uint32_t later =
            gwr.check(Side::Buy, BASE + 1, 1, t + win + 5);       // recovered
        std::printf("\n== RATE LIMITER ==\n"
                    "  burst of 4/4 accepted, 5th => 0x%X (want 0x%X), "
                    "after window => 0x%X (want 0)   %s\n",
                    fifth, RJ_RATE, later,
                    (fifth == RJ_RATE && later == RJ_NONE)
                        ? "[VERIFIED]" : "[BROKEN!]");
    }

    // =======================================================================
    // 5. POSITION LIMIT -- functional proof of exposure accounting.
    // =======================================================================
    {
        RiskGateway<256> gwp(100, BASE, BASE + 65'535, /*pos*/ 10, 1);
        std::uint32_t a = gwp.check(Side::Buy, BASE + 1, 8, 1000);  // open 8
        std::uint32_t b = gwp.check(Side::Buy, BASE + 1, 5, 2000);  // 13 > 10
        gwp.on_cancel(Side::Buy, 8);                                // pull it
        std::uint32_t c = gwp.check(Side::Buy, BASE + 1, 5, 3000);  // 5: fine
        gwp.on_fill(Side::Buy, 5);                                  // long 5
        std::uint32_t d = gwp.check(Side::Buy, BASE + 1, 6, 4000);  // 11 > 10
        std::printf("\n== POSITION LIMIT ==\n"
                    "  open8=0x%X breach=0x%X afterCancel=0x%X "
                    "afterFill+6=0x%X (want 0, 0x%X, 0, 0x%X)   %s\n",
                    a, b, c, d, RJ_POSITION, RJ_POSITION,
                    (a == 0 && b == RJ_POSITION && c == 0 && d == RJ_POSITION)
                        ? "[VERIFIED]" : "[BROKEN!]");
    }

    std::puts("\nbenchmark complete.");
    return 0;
}
