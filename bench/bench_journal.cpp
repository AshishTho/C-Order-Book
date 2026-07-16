// ============================================================================
// bench_journal.cpp -- Deterministic replay journal: cost + correctness.
//
// THREE CLAIMS UNDER TEST
// -----------------------
// 1. COST (hot path): journaling on the feed thread does not degrade the
//    pipeline's tail latency. A/B: identical paced runs (2.5M msgs/sec)
//    through the zero-copy arena into the engine, journal OFF vs ON;
//    compare P50..P99.9.
//
// 2. COST (isolated): batched RDTSC average of Journal::append() itself
//    for a realistic ~38-byte frame.
//
// 3. DETERMINISM (the point of the exercise): after the live journaled
//    run, map the journal back, push it through the SAME ItchParser into
//    a FRESH engine, and require bit-identical business state: trade
//    count, traded lots, best bid/ask, and an FNV-1a fingerprint folded
//    over the aggregate quantity of EVERY price level in the ladder.
//    If any order had been lost, reordered, torn, or re-encoded, the
//    fingerprint diverges. [DETERMINISTIC] must print.
// ============================================================================

#include "pipeline_common.hpp"
#include "lob/arena_ring.hpp"
#include "lob/journal.hpp"
#include <thread>

using namespace lob;
using namespace lob::bench;

using Arena = SpscArenaRing<64, 1u << 14>;
static Arena g_arena;

static const char* JOURNAL_FILE = "build/itch_journal.bin";

// ---------------------------------------------------------------------------
// Business-state fingerprint: FNV-1a over best bid/ask and every level's
// aggregate resting quantity. Any divergence in book state changes it.
// (Cold path -- runs once after each session.)
// ---------------------------------------------------------------------------
static std::uint64_t book_fingerprint(const Book& b) {
    std::uint64_t h = 1469598103934665603ull;
    auto mix = [&](std::uint64_t v) { h ^= v; h *= 1099511628211ull; };
    mix(std::uint64_t(b.best_bid()));
    mix(std::uint64_t(b.best_ask()));
    for (Price p = BASE; p < BASE + (Price(1) << 16); ++p)
        mix(std::uint64_t(b.level_qty(p)));
    return h;
}

struct SessionState {
    std::uint64_t trades, lots, fp;
    Price bb, ba;
};

// ---------------------------------------------------------------------------
// One paced live session through the zero-copy pipeline. Journaling is a
// compile-time flag so the OFF run carries not even a dead branch.
// ---------------------------------------------------------------------------
template <bool WithJournal>
static Pcts run_session(const std::byte* feed, const FeedStats& st,
                        double ghz, std::uint64_t gap, Journal* jr,
                        std::vector<std::uint64_t>& t0,
                        std::vector<std::uint32_t>& samples,
                        SessionState& out, const char* name) {
    Gateway gw(st.max_ref);
    std::thread producer([&] {
        pin_thread(0);
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
            if constexpr (WithJournal)
                jr->append(cur, n);   // bytes are L1-hot right here
            g_arena.publish();
            cur += n;
        }
    });

    std::size_t done = 0;
    while (done < N_MSGS) {
        const std::byte* p = g_arena.try_front();
        if (!p) continue;
        itch::ItchParser one(p, std::size_t(2) + itch::load_be16(p));
        one.next(GatewaySink{gw});
        g_arena.release();
        samples[done] = std::uint32_t(__rdtsc() - t0[done]);
        ++done;
    }
    producer.join();

    out = { gw.trades, gw.traded_lots, book_fingerprint(*gw.book),
            gw.book->best_bid(), gw.book->best_ask() };
    std::printf("  matches: %llu fills  %s\n", (unsigned long long)gw.trades,
                gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]");
    return report(name, samples, ghz);
}

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

    // Pre-touch arena; pre-allocate sample buffers.
    for (std::size_t i = 0; i < (1u << 14); ++i) {
        std::byte* s = g_arena.try_acquire();
        if (!s) break;
        s[0] = std::byte{0};
        g_arena.publish();
    }
    while (g_arena.try_front()) g_arena.release();
    static std::vector<std::uint64_t> t0(N_MSGS);
    std::vector<std::uint32_t> samples(N_MSGS);
    const std::uint64_t gap = std::uint64_t(400.0 * ghz);   // 2.5M msgs/sec

    // =======================================================================
    // 1a. BASELINE -- journal OFF.
    // =======================================================================
    std::puts("\n== RUN A: pipeline latency, journal OFF ==");
    SessionState base{};
    const Pcts a = run_session<false>(feed, st, ghz, gap, nullptr,
                                      t0, samples, base, "journal OFF");

    // =======================================================================
    // 1b. LIVE -- journal ON (opened + pre-touched before the run).
    // =======================================================================
    std::puts("\n== RUN B: pipeline latency, journal ON ==");
    Journal jr;
    if (!jr.open(JOURNAL_FILE, feed_len + 4096)) {
        std::puts("journal open failed"); return 1;
    }
    SessionState live{};
    const Pcts b = run_session<true>(feed, st, ghz, gap, &jr,
                                     t0, samples, live, "journal ON");
    std::printf("  journalled %llu bytes, dropped %llu\n",
                (unsigned long long)jr.committed(),
                (unsigned long long)jr.dropped());
    jr.flush();          // cold path: end-of-session durability point
    jr.close();

    std::printf("\n  journal cost at the percentiles (ON - OFF, ns):"
                "  P50 %+.1f  P99 %+.1f  P99.9 %+.1f\n",
                b.p50 - a.p50, b.p99 - a.p99, b.p999 - a.p999);

    // =======================================================================
    // 2. ISOLATED APPEND COST -- batched RDTSC, realistic frame size.
    // =======================================================================
    {
        Journal jm;
        if (!jm.open("build/itch_journal_micro.bin",
                     std::size_t(40) * 2'000'000)) {
            std::puts("micro journal open failed"); return 1;
        }
        alignas(64) std::byte frame[38] = {};
        constexpr std::size_t N = 2'000'000;
        const std::uint64_t m0 = tsc_begin();
        for (std::size_t i = 0; i < N; ++i) jm.append(frame, sizeof(frame));
        const std::uint64_t m1 = tsc_end();
        std::printf("\n== APPEND MICROCOST ==\n"
                    "  %.2f cycles (%.2f ns) per 38-byte frame, batched x%zu\n",
                    double(m1 - m0) / N, double(m1 - m0) / N / ghz, N);
        jm.close();
    }

    // =======================================================================
    // 3. DETERMINISTIC REPLAY -- rebuild a fresh engine from the journal
    //    alone; business state must match the live session exactly.
    // =======================================================================
    std::puts("\n== REPLAY: reconstructing engine state from the journal ==");
    {
        JournalReplay rv;
        if (!rv.open(JOURNAL_FILE)) { std::puts("replay open failed"); return 1; }
        std::printf("  journal: %zu committed bytes\n", rv.size());

        Gateway gw(st.max_ref);                 // FRESH engine, empty book
        const std::uint64_t r0 = tsc_begin();
        itch::ItchParser parser(rv.data(), rv.size());
        GatewaySink sink{gw};
        std::size_t n = 0;
        while (parser.next(sink)) ++n;
        const std::uint64_t r1 = tsc_end();

        const SessionState rep = { gw.trades, gw.traded_lots,
                                   book_fingerprint(*gw.book),
                                   gw.book->best_bid(), gw.book->best_ask() };
        const double ns = (r1 - r0) / ghz;
        std::printf("  replayed %zu msgs in %.1f ms (%.1f M msgs/sec, "
                    "single thread)\n", n, ns / 1e6, n / (ns / 1e9) / 1e6);

        const bool ok = rep.trades == live.trades && rep.lots == live.lots &&
                        rep.bb == live.bb && rep.ba == live.ba &&
                        rep.fp == live.fp;
        std::printf("  trades %llu/%llu  lots %llu/%llu  bb %lld/%lld  "
                    "ba %lld/%lld\n",
                    (unsigned long long)rep.trades, (unsigned long long)live.trades,
                    (unsigned long long)rep.lots,   (unsigned long long)live.lots,
                    (long long)rep.bb, (long long)live.bb,
                    (long long)rep.ba, (long long)live.ba);
        std::printf("  book fingerprint: %016llX vs %016llX   %s\n",
                    (unsigned long long)rep.fp, (unsigned long long)live.fp,
                    ok ? "[DETERMINISTIC]" : "[DIVERGED!]");
        rv.close();
        if (!ok) return 1;
    }

    std::puts("\nbenchmark complete.");
    return 0;
}
