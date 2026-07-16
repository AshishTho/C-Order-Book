// ============================================================================
// bench_rdtsc.cpp -- Hardware-level latency verification.
//
// WHAT IS MEASURED
// ----------------
// 1. ENGINE BENCH: 1,000,000 matching-engine operations (limit orders that
//    post, cross, and sweep, plus cancels), each individually timed with a
//    serialised RDTSC pair. Reports the full latency distribution:
//    P50 / P90 / P99 / P99.9 / P99.99 / max, in cycles and nanoseconds.
//
// 2. THROUGHPUT BENCH: the same stream timed as ONE block (no per-op fences)
//    -- shows the engine's real streaming rate, since the LFENCE/RDTSC
//    harness itself costs ~15-25ns per sample and serialises the pipeline.
//
// 3. SPSC PIPELINE BENCH: producer thread (simulated network RX) pushes the
//    stream through the lock-free ring to the engine thread on another core.
//    Measures sustained end-to-end throughput of the full architecture.
//
// 4. SPSC RING LATENCY: cross-core ping-pong -- both timestamps taken on
//    the same core, so TSC skew cannot bias the number; one-way = RTT/2.
//
// METHODOLOGY NOTES
// -----------------
// * Thread pinned to one core + top scheduling priority: eliminates
//   migration (cold caches) and preemption from the tail.
// * Workload is pre-generated OUTSIDE the timed region with a deterministic
//   xorshift RNG -- we never time the RNG, and runs are reproducible.
// * 100k-op warm-up populates the book, faults every page in, and trains
//   the branch predictors before the first timed sample.
// * Measurement overhead (the fenced RDTSC pair itself) is measured at
//   startup and subtracted from every sample.
// ============================================================================

#include "lob/order_book.hpp"
#include "lob/spsc_ring.hpp"
#include "lob/rdtsc.hpp"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
#include <thread>
#include <vector>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <pthread.h>
#  include <sched.h>
#endif

using namespace lob;

// ---------------------------------------------------------------------------
// OS knobs: pin the calling thread to `core` and raise its priority.
// Cold path -- called once per thread before measurement.
// ---------------------------------------------------------------------------
static void pin_thread(unsigned core) {
#if defined(_WIN32)
    SetThreadAffinityMask(GetCurrentThread(), 1ull << core);
    // Try REALTIME first (needs elevation; silently downgrades otherwise).
    if (!SetPriorityClass(GetCurrentProcess(), REALTIME_PRIORITY_CLASS))
        SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
#else
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
#endif
}

// ---------------------------------------------------------------------------
// Deterministic workload generator (xorshift64*). Produces a realistic mix:
//   ~55% passive posts (1-32 ticks behind the touch)
//   ~35% aggressive crossers (sweep 1-2 levels; these produce the matches)
//   ~10% cancels of random recent orders
// Mid price is pinned to the ladder centre so the book neither drifts off
// the array nor accumulates unbounded depth.
// ---------------------------------------------------------------------------
struct Rng {
    std::uint64_t s;
    explicit Rng(std::uint64_t seed) : s(seed) {}
    LOB_FORCE_INLINE std::uint64_t next() noexcept {
        s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
        return s * 0x2545F4914F6CDD1Dull;
    }
};

using Book = OrderBook<1u << 16, 1u << 20>;
static constexpr Price      BASE = 1'000'000;
static constexpr Price      MID  = BASE + (1 << 15);          // ladder centre
// 2M operations (~58% posts / 36% aggressive / 6% cancels) generate
// >1,000,000 individual maker fills -- the "1 million order matches" spec.
static constexpr std::size_t N_OPS   = 2'000'000;
static constexpr std::size_t N_WARM  = 100'000;

static std::vector<Message> make_workload(std::size_t n, std::uint64_t seed) {
    std::vector<Message> msgs;
    msgs.reserve(n);
    Rng rng(seed);

    // Ids of recently posted orders, for generating plausible cancels.
    // The generator can't know real engine ids ahead of time, so cancels
    // are marked with type=Cancel and a slot the driver fills in live.
    for (std::size_t i = 0; i < n; ++i) {
        Message m{};
        const std::uint64_t r = rng.next();
        const unsigned bucket = static_cast<unsigned>(r % 100);
        const Side side = (r >> 8) & 1 ? Side::Buy : Side::Sell;
        const Price off = static_cast<Price>(1 + ((r >> 16) & 31));   // 1..32

        if (bucket < 58) {              // passive post, behind the touch
            m.type  = MsgType::NewOrder;
            m.side  = side;
            m.price = side == Side::Buy ? MID - off : MID + off;
            m.qty   = static_cast<Qty>(1 + ((r >> 24) & 7));          // 1..8
        } else if (bucket < 94) {       // aggressive, crosses the spread
            // Sized ~3x the average passive order so each aggressor sweeps
            // ~3 resting makers => the 1M-op stream produces >1M matches.
            m.type  = MsgType::NewOrder;
            m.side  = side;
            m.price = side == Side::Buy ? MID + 8 : MID - 8;
            m.qty   = static_cast<Qty>(8 + ((r >> 24) & 15));         // 8..23
        } else {                        // cancel (target patched by driver)
            m.type  = MsgType::Cancel;
            m.id    = 0;
        }
        msgs.push_back(m);
    }
    return msgs;
}

// Rolling window of live order ids so Cancel messages hit real orders.
struct IdWindow {
    static constexpr std::size_t CAP = 4096;      // power of two
    OrderId ids[CAP] = {};
    std::size_t w = 0;
    LOB_FORCE_INLINE void push(OrderId id) noexcept { ids[w++ & (CAP - 1)] = id; }
    LOB_FORCE_INLINE OrderId pick(std::uint64_t r) const noexcept {
        return ids[r & (CAP - 1)];
    }
};

// ---------------------------------------------------------------------------
// Percentile report from a sorted sample array.
// ---------------------------------------------------------------------------
static void report(const char* name, std::vector<std::uint32_t>& cyc,
                   double ghz) {
    std::sort(cyc.begin(), cyc.end());
    auto pct = [&](double p) {
        const std::size_t i =
            static_cast<std::size_t>(p / 100.0 * (cyc.size() - 1));
        return cyc[i];
    };
    auto row = [&](const char* label, std::uint32_t c) {
        std::printf("    %-8s %6u cycles  %8.1f ns\n", label, c, c / ghz);
    };
    std::printf("  %s (%zu samples):\n", name, cyc.size());
    row("P50",    pct(50));
    row("P90",    pct(90));
    row("P99",    pct(99));
    row("P99.9",  pct(99.9));
    row("P99.99", pct(99.99));
    row("max",    cyc.back());
}

// Same, but prints the raw (overhead-inclusive) value beside the adjusted
// one. When an op is faster than the fenced-RDTSC pair itself, the adjusted
// number floors at 0 -- the raw column shows the true measurement ceiling.
static void report_adjusted(const char* name, std::vector<std::uint32_t>& cyc,
                            double ghz, std::uint64_t ovh) {
    std::sort(cyc.begin(), cyc.end());
    auto pct = [&](double p) {
        const std::size_t i =
            static_cast<std::size_t>(p / 100.0 * (cyc.size() - 1));
        return cyc[i];
    };
    auto row = [&](const char* label, std::uint32_t raw) {
        const std::uint64_t adj = raw > ovh ? raw - ovh : 0;
        std::printf("    %-8s raw %6u cyc (%7.1f ns)   minus harness: "
                    "%6llu cyc (%7.1f ns)\n",
                    label, raw, raw / ghz,
                    (unsigned long long)adj, adj / ghz);
    };
    std::printf("  %s (%zu samples, harness overhead %llu cyc):\n",
                name, cyc.size(), (unsigned long long)ovh);
    row("P50",    pct(50));
    row("P90",    pct(90));
    row("P99",    pct(99));
    row("P99.9",  pct(99.9));
    row("P99.99", pct(99.99));
    row("max",    cyc.back());
}

// ===========================================================================
// BENCH 1 + 2: single-threaded engine latency + throughput.
// ===========================================================================
static void bench_engine(double ghz, std::uint64_t tsc_overhead) {
    std::printf("\n== ENGINE BENCH: per-op serialised RDTSC, %zu ops ==\n",
                N_OPS);

    auto book = std::make_unique<Book>(BASE);      // cold-path allocation
    auto warm = make_workload(N_WARM, 0xDEADBEEF);
    auto work = make_workload(N_OPS, 0xC0FFEE);

    IdWindow ids;
    std::uint64_t trades = 0, traded_qty = 0;
    auto on_trade = [&](const Trade& t) noexcept {
        ++trades; traded_qty += static_cast<std::uint64_t>(t.qty);
    };

    // Drive one message through the book, patching cancels with live ids.
    Rng cancel_rng(0xABCD);
    auto drive = [&](const Message& m) {
        if (LOB_LIKELY(m.type == MsgType::NewOrder)) {
            const OrderId id = book->new_order(m.side, m.price, m.qty, on_trade);
            if (id != 0 && id != ~OrderId{0}) ids.push(id);
        } else {
            book->cancel(ids.pick(cancel_rng.next()));
        }
    };

    // ---- warm-up: build book state, fault pages, train predictors ----
    for (const Message& m : warm) drive(m);

    // ---- BENCH 1: per-op latency distribution (raw samples stored; the
    //      harness overhead is subtracted at report time so both numbers
    //      are visible) ----
    std::vector<std::uint32_t> samples(N_OPS);     // pre-touched by ctor
    for (std::size_t i = 0; i < N_OPS; ++i) {
        const std::uint64_t t0 = tsc_begin();
        drive(work[i]);
        const std::uint64_t t1 = tsc_end();
        samples[i] = static_cast<std::uint32_t>(t1 - t0);
    }
    // Re-measure harness overhead NOW, while the core is at its boosted
    // frequency (the TSC ticks at base clock, so an empty fenced pair costs
    // fewer TSC ticks when the core runs hot; the pre-run estimate taken on
    // a cool core over-states the overhead). Use the smaller of the two.
    const std::uint64_t ovh_hot = measure_tsc_overhead();
    const std::uint64_t ovh = ovh_hot < tsc_overhead ? ovh_hot : tsc_overhead;
    std::printf("  trades executed: %llu (%llu lots)\n",
                (unsigned long long)trades, (unsigned long long)traded_qty);
    report_adjusted("per-op latency", samples, ghz, ovh);

    // ---- BENCH 2: streaming throughput (no per-op serialisation) ----
    auto work2 = make_workload(N_OPS, 0xFACE);
    const std::uint64_t b0 = tsc_begin();
    for (const Message& m : work2) drive(m);
    const std::uint64_t b1 = tsc_end();
    const double ns_total = (b1 - b0) / ghz;
    std::printf("\n  streaming: %zu ops in %.2f ms  =>  %.1f ns/op,"
                "  %.1f M ops/sec\n",
                N_OPS, ns_total / 1e6, ns_total / N_OPS,
                N_OPS / (ns_total / 1e9) / 1e6);
}

// ===========================================================================
// BENCH 3: two-thread SPSC pipeline (network core -> engine core).
// Measures sustained end-to-end throughput with the engine consuming live.
// ===========================================================================
static SpscRing<Message, 1u << 14> g_ring;

static void bench_pipeline(double ghz) {
    std::puts("\n== PIPELINE BENCH: SPSC ring, producer core 0 -> engine core 2 ==");

    auto book = std::make_unique<Book>(BASE);
    auto work = make_workload(N_OPS, 0xBEEF);

    std::thread producer([&] {
        pin_thread(0);
        for (std::size_t i = 0; i < N_OPS; ++i) {
            while (!g_ring.try_push(work[i])) { /* backpressure spin */ }
        }
    });

    pin_thread(2);
    IdWindow ids;
    Rng cancel_rng(0xABCD);
    std::uint64_t trades = 0;
    auto on_trade = [&](const Trade&) noexcept { ++trades; };

    const std::uint64_t t0 = tsc_begin();
    std::size_t done = 0;
    Message m;
    while (done < N_OPS) {
        if (!g_ring.try_pop(m)) continue;            // busy-poll, no futex
        ++done;
        if (LOB_LIKELY(m.type == MsgType::NewOrder)) {
            const OrderId id = book->new_order(m.side, m.price, m.qty, on_trade);
            if (id != 0 && id != ~OrderId{0}) ids.push(id);
        } else {
            book->cancel(ids.pick(cancel_rng.next()));
        }
    }
    const std::uint64_t t1 = tsc_end();
    producer.join();

    const double ns_total = (t1 - t0) / ghz;
    std::printf("  end-to-end: %zu msgs in %.2f ms  =>  %.1f ns/msg,"
                "  %.1f M msgs/sec  (%llu trades)\n",
                N_OPS, ns_total / 1e6, ns_total / N_OPS,
                N_OPS / (ns_total / 1e9) / 1e6, (unsigned long long)trades);
}

// ===========================================================================
// BENCH 4: SPSC ring core-to-core latency, measured honestly via ping-pong.
//
// Why ping-pong: stamping messages inside a streaming test measures QUEUE
// DEPTH (how far the producer ran ahead), not the ring. Here each side waits
// for the other, so a round trip is exactly: push -> cache-line transfer ->
// pop -> push -> transfer -> pop. One-way latency = RTT / 2. Both timestamps
// come from the SAME core, so TSC skew between cores cannot bias the result.
// ===========================================================================
static SpscRing<std::uint64_t, 64> g_ping, g_pong;

static void bench_spsc_rtt(double ghz) {
    std::puts("\n== SPSC RING BENCH: cross-core ping-pong (one-way = RTT/2) ==");
    constexpr std::size_t ROUNDS = 100'000;

    std::thread echo([&] {
        pin_thread(0);
        std::uint64_t v;
        for (std::size_t i = 0; i < ROUNDS; ++i) {
            while (!g_ping.try_pop(v)) {}
            while (!g_pong.try_push(v)) {}
        }
    });

    pin_thread(2);
    std::vector<std::uint32_t> rtt(ROUNDS);
    std::uint64_t v;
    // Warm-up rounds hidden inside: first 10% overwritten below.
    for (std::size_t i = 0; i < ROUNDS; ++i) {
        const std::uint64_t t0 = __rdtsc();
        while (!g_ping.try_push(t0)) {}
        while (!g_pong.try_pop(v)) {}
        const std::uint64_t t1 = __rdtsc();
        rtt[i] = static_cast<std::uint32_t>((t1 - t0) / 2);   // one-way
    }
    echo.join();

    // Drop the first 10% (predictor/cache warm-up rounds).
    std::vector<std::uint32_t> steady(rtt.begin() + ROUNDS / 10, rtt.end());
    report("one-way latency", steady, ghz);
}

// ===========================================================================
int main() {
    pin_thread(2);   // engine core for the single-threaded benches

    const double ghz = calibrate_tsc_ghz();
    const std::uint64_t ovh = measure_tsc_overhead();
    std::printf("TSC calibration: %.3f GHz   |   fenced RDTSC pair overhead: "
                "%llu cycles (%.1f ns) -- subtracted from every sample\n",
                ghz, (unsigned long long)ovh, ovh / ghz);

    bench_engine(ghz, ovh);
    bench_pipeline(ghz);
    bench_spsc_rtt(ghz);

    std::puts("\nbenchmark complete.");
    return 0;
}
