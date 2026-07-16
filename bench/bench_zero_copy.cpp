// ============================================================================
// bench_zero_copy.cpp -- Struct-copying ring vs. Circular Zero-Copy
//                        Ring-Buffer Arena, head to head.
//
// PIPELINE A ("copy"): the original design. The producer core parses each
// ITCH frame out of the capture and COPIES a decoded Message struct into
// the SPSC ring; the consumer copies it back out and applies it.
// Payload crosses the thread boundary by value -- twice.
//
// PIPELINE B ("arena"): the mock network layer writes the raw frame ONCE
// into a pre-allocated, cache-line-aligned arena slot (in production this
// write IS the NIC's DMA -- it does not exist as CPU work) and publishes
// only a sequence number. The engine core decodes and executes DIRECTLY
// out of that stationary slot, then releases it. No payload copy crosses
// the boundary; the queue moves pointers, never data.
//
// MEASUREMENTS (both pipelines, identical feed, identical book work):
//   1. free-run throughput (aggregate ns/msg)
//   2. paced latency  -- uniform 100ns inter-arrival (10M msgs/sec)
//   3. burst latency  -- 64-message back-to-back bursts at the same average
//      10M msgs/sec rate: the extreme-burst regime where copy bandwidth and
//      queue occupancy drive tail jitter.
// Producer stamps RDTSC at pipeline entry; engine core stamps after the
// book update. Reported: P50/P90/P99/P99.9/max, plus a final jitter table.
// ============================================================================

#include "pipeline_common.hpp"
#include "lob/arena_ring.hpp"
#include <thread>

using namespace lob;
using namespace lob::bench;

// One ITCH frame = 2-byte BE length + payload (max 36B here) -> one 64B line.
using Arena = SpscArenaRing<64, 1u << 14>;
static_assert(2 + sizeof(itch::AddOrder) <= Arena::slot_bytes());

static Ring  g_ring;    // pipeline A (static: .bss, no heap)
static Arena g_arena;   // pipeline B

// ---------------------------------------------------------------------------
// Pacing: burst_len messages back-to-back, then wait so the AVERAGE rate is
// one message per `gap` ticks. burst_len == 1 -> uniform pacing.
// ---------------------------------------------------------------------------
struct Pacer {
    std::uint64_t gap, burst_len, next;
    LOB_FORCE_INLINE void start() noexcept { next = __rdtsc() + gap * burst_len; }
    LOB_FORCE_INLINE void tick(std::size_t i) noexcept {
        if (i % burst_len == 0) {
            while (__rdtsc() < next) { }
            next += gap * burst_len;
        }
    }
};

// ---------------------------------------------------------------------------
// Pipeline A: parse on producer, copy Message through the ring.
// ---------------------------------------------------------------------------
static Pcts run_copy(const std::byte* feed, std::size_t feed_len,
                     const FeedStats& st, double ghz,
                     std::uint64_t gap, std::uint64_t burst,
                     std::vector<std::uint64_t>& t0,
                     std::vector<std::uint32_t>& samples,
                     const char* name) {
    Gateway gw(st.max_ref);
    std::thread producer([&] {
        pin_thread(0);
        itch::ItchParser parser(feed, feed_len);
        RingSink sink{g_ring};
        Pacer pace{gap, burst, 0};
        pace.start();
        for (std::size_t i = 0; i < N_MSGS; ++i) {
            pace.tick(i);
            t0[i] = __rdtsc();                      // pipeline entry stamp
            parser.next(sink);
        }
    });

    std::size_t done = 0;
    Message m;
    while (done < N_MSGS) {
        if (!g_ring.try_pop(m)) continue;
        gw.apply(m);
        samples[done] = std::uint32_t(__rdtsc() - t0[done]);
        ++done;
    }
    producer.join();

    std::printf("  matches: %llu fills  %s\n", (unsigned long long)gw.trades,
                gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]");
    return report(name, samples, ghz);
}

// ---------------------------------------------------------------------------
// Pipeline B: raw frame -> stationary arena slot -> decode+match in place.
// ---------------------------------------------------------------------------
static Pcts run_arena(const std::byte* feed, std::size_t feed_len,
                      const FeedStats& st, double ghz,
                      std::uint64_t gap, std::uint64_t burst,
                      std::vector<std::uint64_t>& t0,
                      std::vector<std::uint32_t>& samples,
                      const char* name) {
    Gateway gw(st.max_ref);
    std::thread producer([&] {
        pin_thread(0);
        const std::byte* cur = feed;
        const std::byte* end = feed + feed_len;
        Pacer pace{gap, burst, 0};
        pace.start();
        for (std::size_t i = 0; i < N_MSGS && cur + 2 <= end; ++i) {
            pace.tick(i);
            t0[i] = __rdtsc();                      // pipeline entry stamp
            // Mock network layer: land the frame in its ring-fenced slot.
            // (Real deployment: the NIC DMAs straight into slots_[]; the
            // producer core never touches the payload at all.)
            const std::size_t n = 2 + itch::load_be16(cur);
            std::byte* slot;
            while (!(slot = g_arena.try_acquire())) { /* backpressure spin */ }
            std::memcpy(slot, cur, n);
            g_arena.publish();      // release fence, THEN head index
            cur += n;
        }
    });

    std::size_t done = 0;
    while (done < N_MSGS) {
        const std::byte* p = g_arena.try_front();   // acquire-fenced peek
        if (!p) continue;
        // Decode + match DIRECTLY out of the producer-written slot: the
        // frame never moved since "reception".
        itch::ItchParser one(p, std::size_t(2) + itch::load_be16(p));
        one.next(GatewaySink{gw});
        g_arena.release();          // slot may now be overwritten
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

    // Pre-touch arena + sample buffers so the timed runs never page-fault.
    for (std::size_t i = 0; i < N_MSGS; ++i) {
        std::byte* s = g_arena.try_acquire();
        if (!s) break;
        s[0] = std::byte{0};
        g_arena.publish();
    }
    while (g_arena.try_front()) g_arena.release();

    static std::vector<std::uint64_t> t0(N_MSGS);
    std::vector<std::uint32_t> samples(N_MSGS);
    const std::uint64_t gap = std::uint64_t(100.0 * ghz);   // 100ns => 10M/s
    std::printf("  pacing: %.0f ns avg inter-arrival (%.1f M msgs/sec), "
                "burst mode = 64 back-to-back\n", gap / ghz, 1e3 / (gap / ghz));

    // ---- free-run throughput --------------------------------------------
    std::puts("\n== THROUGHPUT (free-run, gap=0) ==");
    std::puts("  [A: struct-copy ring]");
    Pcts tc = run_copy (feed, feed_len, st, ghz, 0, 1, t0, samples,
                        "copy  free-run  (latency here = queue depth)");
    std::puts("  [B: zero-copy arena]");
    Pcts ta = run_arena(feed, feed_len, st, ghz, 0, 1, t0, samples,
                        "arena free-run  (latency here = queue depth)");
    (void)tc; (void)ta;

    // ---- uniform 10M msgs/sec -------------------------------------------
    std::puts("\n== LATENCY: uniform pacing @ 10M msgs/sec ==");
    std::puts("  [A: struct-copy ring]");
    const Pcts uc = run_copy (feed, feed_len, st, ghz, gap, 1, t0, samples,
                              "copy  uniform");
    std::puts("  [B: zero-copy arena]");
    const Pcts ua = run_arena(feed, feed_len, st, ghz, gap, 1, t0, samples,
                              "arena uniform");

    // ---- 64-msg bursts @ 10M msgs/sec average ---------------------------
    std::puts("\n== LATENCY: 64-msg bursts @ 10M msgs/sec average ==");
    std::puts("  [A: struct-copy ring]");
    const Pcts bc = run_copy (feed, feed_len, st, ghz, gap, 64, t0, samples,
                              "copy  burst64");
    std::puts("  [B: zero-copy arena]");
    const Pcts ba = run_arena(feed, feed_len, st, ghz, gap, 64, t0, samples,
                              "arena burst64");

    // ---- jitter summary ---------------------------------------------------
    // Jitter = spread of the tail above the median (P99/P99.9 - P50): the
    // part of latency the strategy CANNOT plan for.
    std::puts("\n== TAIL LATENCY / JITTER SUMMARY (ns) ==");
    std::printf("  %-16s %8s %8s %8s | %10s %10s\n",
                "pipeline", "P50", "P99", "P99.9", "P99-P50", "P99.9-P50");
    auto line = [](const char* n, const Pcts& p) {
        std::printf("  %-16s %8.1f %8.1f %8.1f | %10.1f %10.1f\n",
                    n, p.p50, p.p99, p.p999, p.p99 - p.p50, p.p999 - p.p50);
    };
    line("copy  uniform",  uc);
    line("arena uniform",  ua);
    line("copy  burst64",  bc);
    line("arena burst64",  ba);
    std::printf("\n  burst64 P99.9 jitter: copy %.1f ns vs arena %.1f ns "
                "(%+.1f%%)\n",
                bc.p999 - bc.p50, ba.p999 - ba.p50,
                100.0 * ((ba.p999 - ba.p50) / (bc.p999 - bc.p50) - 1.0));

    std::puts("\nbenchmark complete.");
    return 0;
}
