// ============================================================================
// bench_itch_pipeline.cpp -- End-to-end market-data gateway benchmark:
//
//     ITCH 5.0 capture file (memory-mapped)
//        -> ItchParser (zero-copy, big-endian decode)      [producer core]
//        -> lock-free SPSC ring (Message copy)             [core-to-core]
//        -> order-ref translation + matching engine        [engine core]
//
// TWO MEASUREMENTS
// ----------------
// 1. THROUGHPUT: producer free-runs through the capture as fast as the
//    pipeline drains. Reports aggregate ns/msg. In this regime per-message
//    "latency" is meaningless -- it measures queue depth, not the code.
//
// 2. LATENCY: producer paces messages at a fixed inter-arrival gap (~10M
//    msgs/sec -- well above real ITCH peak rates) so the ring stays near
//    empty. Each message is stamped with RDTSC when the producer BEGINS
//    parsing it; the engine core reads RDTSC again after the book update
//    completes. Delta = Parse -> Queue -> Match, the complete pipeline.
//    Valid across cores because the invariant TSC is synchronised across
//    all cores of a socket on modern x86.
//
// (Feed generation, mmap, gateway and reporting live in pipeline_common.hpp;
//  the struct-copy vs zero-copy arena comparison lives in bench_zero_copy.)
// ============================================================================

#include "pipeline_common.hpp"
#include <thread>

using namespace lob;
using namespace lob::bench;

int main() {
    pin_thread(2);
    const double ghz = calibrate_tsc_ghz();
    std::printf("TSC calibration: %.3f GHz\n", ghz);

    std::puts("\n== SETUP (cold path) ==");
    const FeedStats st = generate_feed(FEED_FILE, N_MSGS, 0x17C4);

    std::size_t feed_len = 0;
    const std::byte* feed = map_file(FEED_FILE, feed_len);
    if (!feed) { std::puts("mmap failed"); return 1; }
    // Pre-touch the mapping: the benchmark measures the pipeline, not the
    // page cache. (A production gateway reads from NIC DMA buffers that are
    // resident by construction.)
    std::uint64_t sink_sum = 0;
    for (std::size_t i = 0; i < feed_len; i += 4096)
        sink_sum += std::uint8_t(feed[i]);
    std::printf("  mapped %zu bytes (checksum %llu)\n",
                feed_len, (unsigned long long)sink_sum);

    static Ring ring;   // static: placed in .bss, no heap

    // =======================================================================
    // RUN 1 -- THROUGHPUT: free-running producer.
    // =======================================================================
    std::puts("\n== RUN 1: free-run throughput (parse -> ring -> match) ==");
    {
        Gateway gw(st.max_ref);
        std::thread producer([&] {
            pin_thread(0);
            itch::ItchParser parser(feed, feed_len);
            RingSink sink{ring};
            while (parser.next(sink)) {}
        });

        std::size_t done = 0;
        Message m;
        const std::uint64_t t0 = tsc_begin();
        while (done < N_MSGS) {
            if (!ring.try_pop(m)) continue;
            ++done;
            gw.apply(m);
        }
        const std::uint64_t t1 = tsc_end();
        producer.join();

        const double ns = (t1 - t0) / ghz;
        std::printf("  %zu msgs in %.2f ms  =>  %.1f ns/msg,  %.1f M msgs/sec\n",
                    N_MSGS, ns / 1e6, ns / N_MSGS, N_MSGS / (ns / 1e9) / 1e6);
        std::printf("  matches: %llu fills, %llu lots  (expected %llu / %llu)"
                    "  %s\n",
                    (unsigned long long)gw.trades,
                    (unsigned long long)gw.traded_lots,
                    (unsigned long long)st.execs,
                    (unsigned long long)st.exec_lots,
                    gw.trades == st.execs && gw.traded_lots == st.exec_lots
                        ? "[VERIFIED]" : "[MISMATCH!]");
    }

    // =======================================================================
    // RUN 2 -- LATENCY: paced producer, per-message pipeline latency.
    // Producer stamps RDTSC when it starts parsing message i into t0[i];
    // engine core reads RDTSC after the book update. FIFO ordering makes
    // the consumer's pop count equal the producer's message index.
    // =======================================================================
    std::puts("\n== RUN 2: paced latency, complete pipeline "
              "(Parse -> Queue -> Match) ==");
    {
        // ~10M msgs/sec: far above sustained real-feed rates, low enough
        // that queueing delay does not dominate the measurement.
        const std::uint64_t gap = std::uint64_t(100.0 * ghz);   // 100ns
        std::printf("  inter-arrival gap: %llu TSC ticks (%.0f ns, %.1f M "
                    "msgs/sec)\n", (unsigned long long)gap, gap / ghz,
                    1e3 / (gap / ghz));

        Gateway gw(st.max_ref);
        static std::vector<std::uint64_t> t0(N_MSGS);   // producer-stamped
        std::memset(t0.data(), 0, N_MSGS * sizeof(std::uint64_t)); // pre-touch

        std::thread producer([&] {
            pin_thread(0);
            itch::ItchParser parser(feed, feed_len);
            RingSink sink{ring};
            std::uint64_t next = __rdtsc() + gap;
            for (std::size_t i = 0; i < N_MSGS; ++i) {
                while (__rdtsc() < next) { }            // pace the feed
                next += gap;
                t0[i] = __rdtsc();                      // pipeline entry stamp
                parser.next(sink);
            }
        });

        std::vector<std::uint32_t> samples(N_MSGS);
        std::size_t done = 0;
        Message m;
        while (done < N_MSGS) {
            if (!ring.try_pop(m)) continue;
            gw.apply(m);
            samples[done] = std::uint32_t(__rdtsc() - t0[done]);
            ++done;
        }
        producer.join();

        std::printf("  matches: %llu fills  %s\n",
                    (unsigned long long)gw.trades,
                    gw.trades == st.execs ? "[VERIFIED]" : "[MISMATCH!]");
        report("pipeline latency", samples, ghz);
    }

    std::puts("\nbenchmark complete.");
    return 0;
}
