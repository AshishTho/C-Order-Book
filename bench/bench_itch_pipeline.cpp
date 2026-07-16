// ============================================================================
// bench_itch_pipeline.cpp -- End-to-end market-data gateway benchmark:
//
//     ITCH 5.0 capture file (memory-mapped)
//        -> ItchParser (zero-copy, big-endian decode)      [producer core]
//        -> lock-free SPSC ring                            [core-to-core]
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
// SETUP (all cold-path, before any timing):
// * The generator writes a length-framed binary ITCH file: ~1M messages,
//   ~55% Add / ~25% Executed / ~12% partial Cancel / ~8% Delete, refs
//   assigned sequentially like a real feed. Adds never cross (on a real
//   ITCH feed the exchange already matched aggressive flow -- liquidity
//   removal arrives as Order Executed, and that is what drives matches).
// * The file is mapped (mmap / MapViewOfFile) and pre-touched so the timed
//   run measures the pipeline, not disk paging.
// * The order-reference -> engine-id table is a flat pre-allocated array
//   (refs are dense integers) -- O(1) lookup, no hash, no rehash spikes.
// ============================================================================

#include "lob/itch.hpp"
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
#  include <fcntl.h>
#  include <pthread.h>
#  include <sys/mman.h>
#  include <sys/stat.h>
#  include <unistd.h>
#endif

using namespace lob;

// ---------------------------------------------------------------------------
// OS helpers (cold path)
// ---------------------------------------------------------------------------
static void pin_thread(unsigned core) {
#if defined(_WIN32)
    SetThreadAffinityMask(GetCurrentThread(), 1ull << core);
    if (!SetPriorityClass(GetCurrentProcess(), REALTIME_PRIORITY_CLASS))
        SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
#else
    cpu_set_t set; CPU_ZERO(&set); CPU_SET(core, &set);
    pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
#endif
}

// Map a file read-only into the address space. Returns pointer + length.
static const std::byte* map_file(const char* path, std::size_t& out_len) {
#if defined(_WIN32)
    HANDLE f = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE) return nullptr;
    LARGE_INTEGER sz;
    if (!GetFileSizeEx(f, &sz)) { CloseHandle(f); return nullptr; }
    HANDLE m = CreateFileMappingA(f, nullptr, PAGE_READONLY, 0, 0, nullptr);
    CloseHandle(f);                       // mapping holds its own reference
    if (!m) return nullptr;
    void* v = MapViewOfFile(m, FILE_MAP_READ, 0, 0, 0);
    CloseHandle(m);                       // view holds its own reference
    out_len = static_cast<std::size_t>(sz.QuadPart);
    return static_cast<const std::byte*>(v);
#else
    int fd = ::open(path, O_RDONLY);
    if (fd < 0) return nullptr;
    struct stat st{};
    fstat(fd, &st);
    void* v = ::mmap(nullptr, st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
    ::close(fd);
    out_len = static_cast<std::size_t>(st.st_size);
    return v == MAP_FAILED ? nullptr : static_cast<const std::byte*>(v);
#endif
}

// ---------------------------------------------------------------------------
// Feed generator (cold path -- std containers and file I/O are fine here).
// Emits a deterministic, internally-consistent ITCH session: every 'E'/'X'/
// 'D' references a live order with sufficient remaining shares.
// ---------------------------------------------------------------------------
struct Rng {
    std::uint64_t s;
    explicit Rng(std::uint64_t seed) : s(seed) {}
    std::uint64_t next() noexcept {
        s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
        return s * 0x2545F4914F6CDD1Dull;
    }
};

static constexpr Price       BASE   = 1'000'000;      // $100.0000 in ticks
static constexpr Price       MID    = BASE + (1 << 15);
static constexpr std::size_t N_MSGS = 1'000'000;
static const char*           FEED_FILE = "build/itch_feed.bin";

struct FeedStats {
    std::uint64_t adds = 0, execs = 0, cancels = 0, deletes = 0;
    std::uint64_t exec_lots = 0, max_ref = 0;
};

static void put_be16(std::vector<std::uint8_t>& b, std::uint16_t v) {
    b.push_back(std::uint8_t(v >> 8)); b.push_back(std::uint8_t(v));
}
static void put_be32(std::vector<std::uint8_t>& b, std::uint32_t v) {
    for (int i = 3; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
static void put_be48(std::vector<std::uint8_t>& b, std::uint64_t v) {
    for (int i = 5; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
static void put_be64(std::vector<std::uint8_t>& b, std::uint64_t v) {
    for (int i = 7; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
static void put_header(std::vector<std::uint8_t>& b, char type,
                       std::uint64_t ts) {
    b.push_back(std::uint8_t(type));
    put_be16(b, 1);                       // stock locate
    put_be16(b, 0);                       // tracking number
    put_be48(b, ts);                      // ns since midnight
}

static FeedStats generate_feed(const char* path, std::size_t n_msgs,
                               std::uint64_t seed) {
    std::vector<std::uint8_t> buf;
    buf.reserve(n_msgs * 40);
    Rng rng(seed);
    FeedStats st;

    struct Live { std::uint64_t ref; std::uint32_t shares; };
    std::vector<Live> live;
    live.reserve(1 << 20);
    std::uint64_t next_ref = 1, ts = 34'200'000'000'000ull;  // 09:30 in ns

    for (std::size_t i = 0; i < n_msgs; ++i) {
        const std::uint64_t r = rng.next();
        unsigned bucket = unsigned(r % 100);
        if (live.empty()) bucket = 0;     // nothing to hit -> force an Add
        ts += 1 + (r & 1023);             // monotone feed timestamps

        if (bucket < 55) {                                     // ---- 'A'
            const bool buy = (r >> 8) & 1;
            const Price off = 1 + Price((r >> 16) & 31);       // 1..32 ticks
            const std::uint32_t shares = 4 + std::uint32_t((r >> 24) & 31);
            const std::uint64_t ref = next_ref++;

            put_be16(buf, sizeof(itch::AddOrder));
            put_header(buf, 'A', ts);
            put_be64(buf, ref);
            buf.push_back(std::uint8_t(buy ? 'B' : 'S'));
            put_be32(buf, shares);
            const char sym[8] = {'L','O','B','X',' ',' ',' ',' '};
            buf.insert(buf.end(), sym, sym + 8);
            // Adds never cross: bids strictly below MID, asks above.
            put_be32(buf, std::uint32_t(buy ? MID - off : MID + off));

            live.push_back({ref, shares});
            ++st.adds; st.max_ref = ref;
        } else if (bucket < 80) {                              // ---- 'E'
            Live& o = live[(r >> 32) % live.size()];
            const std::uint32_t ex =
                std::min<std::uint32_t>(1 + std::uint32_t((r >> 16) & 7),
                                        o.shares);
            put_be16(buf, sizeof(itch::OrderExecuted));
            put_header(buf, 'E', ts);
            put_be64(buf, o.ref);
            put_be32(buf, ex);
            put_be64(buf, ++st.execs);    // match number
            st.exec_lots += ex;
            o.shares -= ex;
            if (o.shares == 0) { o = live.back(); live.pop_back(); }
        } else if (bucket < 92) {                              // ---- 'X'
            Live& o = live[(r >> 32) % live.size()];
            if (o.shares < 2) { --i; continue; }  // partial needs >=2 shares
            const std::uint32_t cx = 1 + std::uint32_t((r >> 16) % (o.shares - 1));
            put_be16(buf, sizeof(itch::OrderCancel));
            put_header(buf, 'X', ts);
            put_be64(buf, o.ref);
            put_be32(buf, cx);
            o.shares -= cx;
            ++st.cancels;
        } else {                                               // ---- 'D'
            const std::size_t k = (r >> 32) % live.size();
            put_be16(buf, sizeof(itch::OrderDelete));
            put_header(buf, 'D', ts);
            put_be64(buf, live[k].ref);
            live[k] = live.back(); live.pop_back();
            ++st.deletes;
        }
    }

    std::FILE* f = std::fopen(path, "wb");
    if (!f) std::abort();
    std::fwrite(buf.data(), 1, buf.size(), f);
    std::fclose(f);
    std::printf("  feed: %zu msgs, %.1f MB  (A=%llu E=%llu X=%llu D=%llu)\n",
                n_msgs, buf.size() / 1048576.0,
                (unsigned long long)st.adds, (unsigned long long)st.execs,
                (unsigned long long)st.cancels, (unsigned long long)st.deletes);
    return st;
}

// ---------------------------------------------------------------------------
// Producer sink: ITCH wire struct -> engine Message -> SPSC ring.
// All four operator() overloads are inlined into the parser's dispatch;
// the byte swaps are the only work between the buffer and the ring slot.
// Message.id carries the exchange ORDER REFERENCE; the engine core owns
// the ref -> engine-id table (single-writer, so it needs no synchronisation).
// ---------------------------------------------------------------------------
using Ring = SpscRing<Message, 1u << 14>;

struct RingSink {
    Ring& ring;

    LOB_FORCE_INLINE void push(const Message& m) noexcept {
        while (!ring.try_push(m)) { /* backpressure spin */ }
    }
    LOB_FORCE_INLINE void operator()(const itch::AddOrder& a) noexcept {
        Message m{};
        m.type  = MsgType::NewOrder;
        m.side  = a.side == 'B' ? Side::Buy : Side::Sell;
        m.price = Price(itch::bswap(a.price));   // same 1e-4 fixed point
        m.qty   = Qty(itch::bswap(a.shares));
        m.id    = itch::bswap(a.order_ref);
        push(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderExecuted& e) noexcept {
        Message m{};
        m.type = MsgType::Execute;
        m.qty  = Qty(itch::bswap(e.executed_shares));
        m.id   = itch::bswap(e.order_ref);
        push(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderCancel& x) noexcept {
        Message m{};
        m.type = MsgType::Reduce;
        m.qty  = Qty(itch::bswap(x.canceled_shares));
        m.id   = itch::bswap(x.order_ref);
        push(m);
    }
    LOB_FORCE_INLINE void operator()(const itch::OrderDelete& d) noexcept {
        Message m{};
        m.type = MsgType::Cancel;
        m.id   = itch::bswap(d.order_ref);
        push(m);
    }
};

// ---------------------------------------------------------------------------
// Engine-side gateway: order-ref translation + book mutation + accounting.
// ---------------------------------------------------------------------------
using Book = OrderBook<1u << 16, 1u << 20>;

struct Gateway {
    std::unique_ptr<Book>  book;
    std::vector<OrderId>   refs;      // flat ref -> engine id (pre-allocated)
    std::uint64_t          trades = 0, traded_lots = 0;

    explicit Gateway(std::uint64_t max_ref)
        : book(std::make_unique<Book>(BASE)), refs(max_ref + 1, 0) {}

    LOB_FORCE_INLINE void apply(const Message& m) noexcept {
        switch (m.type) {
        case MsgType::NewOrder: {
            const OrderId eng = book->new_order(m.side, m.price, m.qty,
                                                [](const Trade&) noexcept {});
            refs[m.id] = eng;
            break;
        }
        case MsgType::Execute: {          // a real fill: count as a match
            const Qty filled = book->reduce(refs[m.id], m.qty);
            trades      += filled > 0;
            traded_lots += std::uint64_t(filled);
            break;
        }
        case MsgType::Reduce:
            book->reduce(refs[m.id], m.qty);
            break;
        case MsgType::Cancel:
            book->cancel(refs[m.id]);
            break;
        }
    }
};

// ---------------------------------------------------------------------------
static void report(const char* name, std::vector<std::uint32_t>& cyc,
                   double ghz) {
    std::sort(cyc.begin(), cyc.end());
    auto pct = [&](double p) {
        return cyc[std::size_t(p / 100.0 * (cyc.size() - 1))];
    };
    auto row = [&](const char* label, std::uint32_t c) {
        std::printf("    %-8s %6u cycles  %8.1f ns\n", label, c, c / ghz);
    };
    std::printf("  %s (%zu samples):\n", name, cyc.size());
    row("P50",    pct(50));
    row("P90",    pct(90));
    row("P99",    pct(99));
    row("P99.9",  pct(99.9));
    row("max",    cyc.back());
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
