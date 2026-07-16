// ============================================================================
// bench_signals.cpp -- OFI + micro-price generation: correctness + cost.
//
// 1. CORRECTNESS: a scripted book scenario with hand-computed OFI and
//    micro-price expectations after every transition (price up/down, size
//    grow/shrink at a standing level, both sides). [VERIFIED] required.
//
// 2. SIGNAL MATH MICROCOST: batched RDTSC over the OFI + micro arithmetic
//    alone (register inputs), isolating what the signals add on TOP of
//    the BBO diff that bbo.hpp already paid for.
//
// 3. PIPELINE A/B: the full paced arena pipeline with a draining consumer
//    thread -- plain BboPublisher vs SignalPublisher -- proving the
//    signal upgrade does not move wire-to-book latency percentiles.
// ============================================================================

#include "pipeline_common.hpp"
#include "lob/arena_ring.hpp"
#include "lob/bbo.hpp"
#include "lob/signals.hpp"
#include <atomic>
#include <thread>

using namespace lob;
using namespace lob::bench;

using Arena    = SpscArenaRing<64, 1u << 14>;
using BboRing  = SpscRing<BboUpdate,    1u << 15>;
using SigRing  = SpscRing<SignalUpdate, 1u << 15>;
static Arena   g_arena;
static BboRing g_bbo;
static SigRing g_sig;

volatile std::uint64_t g_sink;

// ===========================================================================
// 1. Scripted correctness scenario.
// ===========================================================================
static bool correctness(double) {
    using TinyRing = SpscRing<SignalUpdate, 1u << 4>;
    Book book(BASE);
    TinyRing ring;
    SignalPublisher<Book, TinyRing> pub(book, ring);
    auto none = [](const Trade&) noexcept {};

    bool ok = true;
    SignalUpdate u{};
    auto expect = [&](const char* what, std::int64_t ofi,
                      std::int64_t micro) {
        if (!ring.try_pop(u)) { std::printf("  %-34s NO UPDATE\n", what);
                                ok = false; return; }
        const bool good = u.ofi == ofi && u.micro_px == micro;
        std::printf("  %-34s ofi %lld (want %lld)  micro %lld (want %lld) %s\n",
                    what, (long long)u.ofi, (long long)ofi,
                    (long long)u.micro_px, (long long)micro,
                    good ? "ok" : "WRONG");
        ok &= good;
    };

    const Price B = BASE + 1000, A = BASE + 1010;

    // t1: opening bid 10 lots. priors {-1,-1,0,0}: e = 1*10 - 0 - 1*0 + 1*0
    //     = +10. micro: ask side empty => (B*0 + (-1)*10)<<8 / 10 ... ask
    //     px is -1 sentinel: micro = (-10<<8)/10 = -256 (garbage by design:
    //     one-sided books have no micro-price; consumers gate on px >= 0).
    book.new_order(Side::Buy, B, 10, none);
    pub.on_message_applied(1);
    expect("open bid 10", 10, ((B * 0 + (-1) * 10) << 8) / 10);

    // t2: opening ask 30. Bid unchanged (equal px: +10 -10 = 0). Ask
    //     priors {-1, 0}: A <= -1 false (no -qa term), A >= -1 true but
    //     qa' = 0. e = 0. cum = 10. micro = (B*30 + A*10)<<8 / 40.
    book.new_order(Side::Sell, A, 30, none);
    pub.on_message_applied(2);
    expect("open ask 30", 10, ((B * 30 + A * 10) << 8) / 40);

    // t3: bid size grows to 25 (join 15 at same px).
    //     e = 25 - 10 (equal px, both bid indicators) = +15. cum = 25.
    book.new_order(Side::Buy, B, 15, none);
    pub.on_message_applied(3);
    expect("bid join +15 (qty 25)", 25, ((B * 30 + A * 25) << 8) / 55);

    // t4: better bid appears at B+2, 7 lots. Pb rises:
    //     e = +7 (only the >= indicator fires). cum = 32.
    book.new_order(Side::Buy, B + 2, 7, none);
    pub.on_message_applied(4);
    expect("bid improves +2 ticks, 7", 32, (((B + 2) * 30 + A * 7) << 8) / 37);

    // t5: ask improves to A-3, 5 lots. Pa falls: e = -qa = -5. cum = 27.
    book.new_order(Side::Sell, A - 3, 5, none);
    pub.on_message_applied(5);
    expect("ask improves -3 ticks, 5", 27,
           (((B + 2) * 5 + (A - 3) * 7) << 8) / 12);

    // t6: aggressive sell 7 lots sweeps the best bid level (B+2 empties,
    //     top reverts to B qty 25). Pb falls: e = -qb' = -7. cum = 20.
    book.new_order(Side::Sell, B + 2, 7, none);
    pub.on_message_applied(6);
    expect("bid swept, top back to B", 20,
           ((B * 5 + (A - 3) * 25) << 8) / 30);

    std::printf("  cumulative OFI from publisher: %lld   %s\n",
                (long long)pub.ofi(),
                ok ? "[VERIFIED]" : "[BROKEN!]");
    return ok;
}

// ===========================================================================
// 3. Pipeline session with a publisher + drain thread, A/B-able by type.
// ===========================================================================
template <typename Pub, typename Ring>
static Pcts run_session(const std::byte* feed, const FeedStats& st,
                        double ghz, std::uint64_t gap, Ring& ring,
                        std::vector<std::uint64_t>& t0,
                        std::vector<std::uint32_t>& samples,
                        const char* name, std::uint64_t& published) {
    Gateway gw(st.max_ref);
    Pub pub(*gw.book, ring);
    std::atomic<bool> stop{false};

    std::thread drain([&] {          // strategy-lite: consume every update
        pin_thread(4);
        typename Ring::value_type u;
        std::uint64_t acc = 0;
        while (!stop.load(std::memory_order_acquire))
            if (ring.try_pop(u)) acc += std::uint64_t(u.seq);
        while (ring.try_pop(u)) acc += std::uint64_t(u.seq);
        g_sink = acc;
    });
    std::thread producer([&] {
        pin_thread(0);
        const std::byte* cur = feed;
        std::uint64_t next = __rdtsc() + gap;
        for (std::size_t i = 0; i < N_MSGS; ++i) {
            while (__rdtsc() < next) { }
            next += gap;
            t0[i] = __rdtsc();
            const std::size_t n = 2 + itch::load_be16(cur);
            std::byte* slot;
            while (!(slot = g_arena.try_acquire())) { }
            std::memcpy(slot, cur, n);
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
        pub.on_message_applied(t0[done]);
        samples[done] = std::uint32_t(__rdtsc() - t0[done]);
        ++done;
    }
    producer.join();
    stop.store(true, std::memory_order_release);
    drain.join();

    published = pub.published();
    std::printf("  matches: %llu fills %s   updates published: %llu "
                "(dropped %llu)\n",
                (unsigned long long)gw.trades,
                gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]",
                (unsigned long long)pub.published(),
                (unsigned long long)pub.dropped());
    return report(name, samples, ghz);
}

// ===========================================================================
int main() {
    pin_thread(2);
    const double ghz = calibrate_tsc_ghz();
    std::printf("TSC calibration: %.3f GHz\n", ghz);

    std::puts("\n== CORRECTNESS: scripted OFI / micro-price scenario ==");
    if (!correctness(ghz)) return 1;

    // =======================================================================
    // 2. Signal arithmetic microcost (inputs in registers, batched).
    // =======================================================================
    {
        constexpr std::size_t N = 10'000'000;
        Rng r(0x0F1ull);
        std::int64_t ofi = 0, acc = 0;
        Price lbp = BASE, lap = BASE + 10;
        Qty   lbq = 50,   laq = 50;
        const std::uint64_t c0 = tsc_begin();
        for (std::size_t i = 0; i < N; ++i) {
            const std::uint64_t v = r.next();
            const Price bp = BASE + Price(v & 31);
            const Price ap = BASE + 32 + Price((v >> 8) & 31);
            const Qty   bq = 1 + Qty((v >> 16) & 255);
            const Qty   aq = 1 + Qty((v >> 24) & 255);
            ofi += Qty(bp >= lbp) * bq - Qty(bp <= lbp) * lbq
                 - Qty(ap <= lap) * aq + Qty(ap >= lap) * laq;
            const Qty d = bq + aq;
            acc += ((bp * aq + ap * bq) << 8) / (d + Qty(d == 0));
            lbp = bp; lap = ap; lbq = bq; laq = aq;
        }
        const std::uint64_t c1 = tsc_end();
        g_sink = std::uint64_t(ofi) ^ std::uint64_t(acc);
        std::printf("\n== SIGNAL MATH MICROCOST (batched x%zu) ==\n"
                    "  OFI + micro-price: %.2f cycles (%.2f ns) per top-of-"
                    "book change\n", N,
                    double(c1 - c0) / N, double(c1 - c0) / N / ghz);
    }

    // =======================================================================
    // 3. Pipeline A/B.
    // =======================================================================
    std::puts("\n== SETUP (cold path) ==");
    const FeedStats st = generate_feed(FEED_FILE, N_MSGS, 0x17C4);
    std::size_t feed_len = 0;
    const std::byte* feed = map_file(FEED_FILE, feed_len);
    if (!feed) { std::puts("mmap failed"); return 1; }
    std::uint64_t sink_sum = 0;
    for (std::size_t i = 0; i < feed_len; i += 4096)
        sink_sum += std::uint8_t(feed[i]);
    std::printf("  mapped %zu bytes (checksum %llu)\n",
                feed_len, (unsigned long long)sink_sum);
    for (std::size_t i = 0; i < (1u << 14); ++i) {        // pre-touch arena
        std::byte* s = g_arena.try_acquire();
        if (!s) break;
        s[0] = std::byte{0};
        g_arena.publish();
    }
    while (g_arena.try_front()) g_arena.release();
    static std::vector<std::uint64_t> t0(N_MSGS);
    std::vector<std::uint32_t> samples(N_MSGS);
    const std::uint64_t gap = std::uint64_t(400.0 * ghz);
    std::uint64_t pub_a = 0, pub_b = 0;

    std::puts("\n== RUN A: plain BBO publisher ==");
    const Pcts a = run_session<BboPublisher<Book, BboRing>>(
        feed, st, ghz, gap, g_bbo, t0, samples, "BBO only", pub_a);

    std::puts("\n== RUN B: OFI + micro-price signal publisher ==");
    const Pcts b = run_session<SignalPublisher<Book, SigRing>>(
        feed, st, ghz, gap, g_sig, t0, samples, "signals", pub_b);

    std::printf("\n  signal upgrade cost (B - A, ns):  P50 %+.1f  P90 %+.1f"
                "  P99 %+.1f   (%llu vs %llu updates)\n",
                b.p50 - a.p50, b.p90 - a.p90, b.p99 - a.p99,
                (unsigned long long)pub_b, (unsigned long long)pub_a);

    std::puts("\nbenchmark complete.");
    return 0;
}
