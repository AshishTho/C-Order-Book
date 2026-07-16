// ============================================================================
// bench_flat_hash.cpp -- Sparse order-ref map: correctness + latency proof.
//
// 1. CORRECTNESS: 300k randomized insert/erase/find ops cross-checked
//    against a trivially-correct dense reference table (the key domain is
//    deliberately small, so an array indexed by key ordinal is a perfect
//    oracle). Exercises collisions, key reuse after erase, and
//    backward-shift compaction.
//
// 2. MICROCOST: batched RDTSC for insert / find-hit / find-miss / erase
//    with 1M SPARSE random 64-bit keys in a 2M-slot table (48% load --
//    deliberately at the cap, the worst case we provision for), plus
//    per-op find percentiles from 64-op blocks.
//
// 3. THE REALITY CHECK: the pipeline's dense ref array (refs[order_ref],
//    only possible because the synthetic feed numbers refs 1,2,3...) is
//    replaced by the hash map fed BIJECTIVELY SPARSIFIED refs (ref *
//    odd-constant mod 2^64 -- the same ids a real exchange would look
//    like), and the full paced pipeline is re-measured A/B. This is the
//    number that says what production sparse-id translation really costs.
// ============================================================================

#include "pipeline_common.hpp"
#include "lob/arena_ring.hpp"
#include "lob/flat_hash.hpp"
#include <thread>

using namespace lob;
using namespace lob::bench;

using Arena = SpscArenaRing<64, 1u << 14>;
static Arena g_arena;

volatile std::uint64_t g_sink;

// ---------------------------------------------------------------------------
// Gateway variant: identical business logic to bench::Gateway, but order-ref
// translation goes through the sparse map. Refs are sparsified through a
// bijective odd-multiplier so the map sees realistic scattered 64-bit keys
// (multiplying by an odd constant is invertible mod 2^64 and never maps a
// non-zero ref to the map's 0 sentinel).
// ---------------------------------------------------------------------------
struct HashGateway {
    std::unique_ptr<Book>       book;
    FlatHashMap<1u << 21>       refs;     // 2M slots, one 32MB block, no rehash
    std::uint64_t               trades = 0, traded_lots = 0;

    explicit HashGateway(std::uint64_t) : book(std::make_unique<Book>(BASE)) {}

    LOB_FORCE_INLINE static std::uint64_t sparse(std::uint64_t ref) noexcept {
        return ref * 0xC2B2AE3D27D4EB4Full;   // odd => bijective, non-zero
    }

    LOB_FORCE_INLINE void apply(const Message& m) noexcept {
        switch (m.type) {
        case MsgType::NewOrder: {
            const OrderId eng = book->new_order(m.side, m.price, m.qty,
                                                [](const Trade&) noexcept {});
            refs.insert(sparse(m.id), eng);
            break;
        }
        case MsgType::Execute: {
            const Qty filled = book->reduce(refs.find(sparse(m.id)), m.qty);
            trades      += filled > 0;
            traded_lots += std::uint64_t(filled);
            break;
        }
        case MsgType::Reduce:
            book->reduce(refs.find(sparse(m.id)), m.qty);
            break;
        case MsgType::Cancel: {                 // full delete: drop the ref
            const std::uint64_t k = sparse(m.id);
            book->cancel(refs.find(k));
            refs.erase(k);                      // backward-shift keeps chains short
            break;
        }
        }
    }
};

// Sink usable with either gateway type (same shape as bench::GatewaySink).
template <typename GW>
struct Sink {
    GW& gw;
    LOB_FORCE_INLINE void operator()(const itch::AddOrder& a) noexcept {
        Message m{};
        m.type  = MsgType::NewOrder;
        m.side  = a.side == 'B' ? Side::Buy : Side::Sell;
        m.price = Price(itch::bswap(a.price));
        m.qty   = Qty(itch::bswap(a.shares));
        m.id    = itch::bswap(a.order_ref);
        gw.apply(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderExecuted& e) noexcept {
        Message m{};
        m.type = MsgType::Execute;
        m.qty  = Qty(itch::bswap(e.executed_shares));
        m.id   = itch::bswap(e.order_ref);
        gw.apply(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderCancel& x) noexcept {
        Message m{};
        m.type = MsgType::Reduce;
        m.qty  = Qty(itch::bswap(x.canceled_shares));
        m.id   = itch::bswap(x.order_ref);
        gw.apply(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderDelete& d) noexcept {
        Message m{};
        m.type = MsgType::Cancel;
        m.id   = itch::bswap(d.order_ref);
        gw.apply(m);
    }
};

// ---------------------------------------------------------------------------
// Paced pipeline session, gateway type as template parameter.
// ---------------------------------------------------------------------------
template <typename GW>
static Pcts run_session(const std::byte* feed, const FeedStats& st,
                        double ghz, std::uint64_t gap,
                        std::vector<std::uint64_t>& t0,
                        std::vector<std::uint32_t>& samples,
                        const char* name) {
    GW gw(st.max_ref);
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
        one.next(Sink<GW>{gw});
        g_arena.release();
        samples[done] = std::uint32_t(__rdtsc() - t0[done]);
        ++done;
    }
    producer.join();

    std::printf("  matches: %llu fills  %s\n", (unsigned long long)gw.trades,
                gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]");
    return report(name, samples, ghz);
}

// ===========================================================================
int main() {
    pin_thread(2);
    const double ghz = calibrate_tsc_ghz();
    std::printf("TSC calibration: %.3f GHz\n", ghz);

    // =======================================================================
    // 1. CORRECTNESS vs a dense reference oracle (cold path, test only).
    // Keys are ordinal * golden-ratio-odd (sparse, collision-heavy in an
    // 8K table); the oracle is simply expect[ordinal].
    // =======================================================================
    {
        constexpr std::size_t DOM = 3000;   // live size < 4096 load cap
        auto fm = std::make_unique<FlatHashMap<1u << 13>>();  // 8K slots
        static std::uint64_t expect[DOM] = {};   // 0 == absent
        std::size_t ref_size = 0;
        Rng r(0xF1A5Bull);
        bool ok = true;
        for (int i = 0; i < 300'000 && ok; ++i) {
            const std::uint64_t v   = r.next();
            const std::uint64_t ord = v % DOM;
            const std::uint64_t k   = (ord + 1) * 0x9E3779B97F4A7C15ull;
            switch ((v >> 32) % 3) {
            case 0:
                ok = fm->insert(k, v | 1);
                ref_size += (expect[ord] == 0);
                expect[ord] = v | 1;
                break;
            case 1:
                ok = fm->erase(k) == (expect[ord] != 0);
                ref_size -= (expect[ord] != 0);
                expect[ord] = 0;
                break;
            default:
                ok = fm->find(k) == expect[ord];
            }
        }
        ok = ok && fm->size() == ref_size;
        for (std::size_t ord = 0; ord < DOM; ++ord)      // final-state audit
            ok = ok && fm->find((ord + 1) * 0x9E3779B97F4A7C15ull)
                           == expect[ord];
        std::printf("\n== CORRECTNESS (300k randomized ops vs oracle) ==\n"
                    "  final size %zu/%zu   %s\n", fm->size(), ref_size,
                    ok ? "[VERIFIED]" : "[BROKEN!]");
        if (!ok) return 1;
    }

    // =======================================================================
    // 2. MICROCOST -- 1M sparse keys, 2M-slot table (48% load = at the cap).
    // =======================================================================
    {
        constexpr std::size_t N = 1'000'000;
        static std::vector<std::uint64_t> keys(N), miss(N);
        Rng r(0xB16B00B5ull);
        for (auto& k : keys) do { k = r.next(); } while (k == 0);
        for (auto& k : miss) do { k = r.next(); } while (k == 0);

        auto m = std::make_unique<FlatHashMap<1u << 21>>();
        auto lap = [&](const char* name, auto&& fn) {
            const std::uint64_t c0 = tsc_begin();
            fn();
            const std::uint64_t c1 = tsc_end();
            std::printf("  %-10s %6.2f cycles  %6.2f ns/op\n",
                        name, double(c1 - c0) / N, double(c1 - c0) / N / ghz);
        };
        std::printf("\n== MICROCOST (1M sparse keys, %u%% load) ==\n",
                    unsigned(100.0 * N / (1u << 21)));
        std::uint64_t acc = 0;
        lap("insert",    [&] { for (auto k : keys) m->insert(k, k ^ 1); });
        lap("find hit",  [&] { for (auto k : keys) acc += m->find(k); });
        lap("find miss", [&] { for (auto k : miss) acc += m->find(k); });
        lap("erase",     [&] { for (auto k : keys) m->erase(k); });
        g_sink = acc;

        // per-op find percentiles (64-op blocks, overhead-subtracted)
        for (auto k : keys) m->insert(k, k ^ 1);          // refill
        const std::uint64_t ovh = measure_tsc_overhead();
        static std::vector<std::uint32_t> blk(N / 64);
        for (std::size_t b = 0; b < N / 64; ++b) {
            const std::uint64_t t0 = tsc_begin();
            for (std::size_t j = 0; j < 64; ++j)
                acc += m->find(keys[b * 64 + j]);
            const std::uint64_t t1 = tsc_end();
            blk[b] = std::uint32_t(((t1 - t0 > ovh) ? t1 - t0 - ovh : 0) / 64);
        }
        g_sink = acc;
        std::puts("");
        report("find (per-op, 64-blocks)", blk, ghz);
    }

    // =======================================================================
    // 3. PIPELINE A/B -- dense array (synthetic luxury) vs sparse hash map
    //    (production reality), full paced wire-to-book latency.
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

    std::puts("\n== RUN A: dense ref array (sequential refs only) ==");
    const Pcts a = run_session<Gateway>(feed, st, ghz, gap, t0, samples,
                                        "dense array");
    std::puts("\n== RUN B: sparse hash map (production ids) ==");
    const Pcts b = run_session<HashGateway>(feed, st, ghz, gap, t0, samples,
                                            "flat hash map");

    std::printf("\n  sparse-id translation cost (B - A, ns):"
                "  P50 %+.1f  P90 %+.1f  P99 %+.1f\n",
                b.p50 - a.p50, b.p90 - a.p90, b.p99 - a.p99);

    std::puts("\nbenchmark complete.");
    return 0;
}
