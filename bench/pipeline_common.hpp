#pragma once
// ============================================================================
// pipeline_common.hpp -- Shared scaffolding for the gateway benchmarks:
// OS helpers (thread pinning, file mapping), the deterministic ITCH feed
// generator, the Message-copying ring sink, the engine-side gateway, and
// the percentile reporter. All of this is COLD PATH except RingSink /
// Gateway, whose hot members are force-inlined into the benchmark loops.
// ============================================================================

#include "lob/itch.hpp"
#include "lob/order_book.hpp"
#include "lob/spsc_ring.hpp"
#include "lob/rdtsc.hpp"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <memory>
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

namespace lob::bench {

// ---------------------------------------------------------------------------
// OS helpers (cold path)
// ---------------------------------------------------------------------------
inline void pin_thread(unsigned core) {
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
inline const std::byte* map_file(const char* path, std::size_t& out_len) {
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

inline constexpr Price       BASE   = 1'000'000;      // $100.0000 in ticks
inline constexpr Price       MID    = BASE + (1 << 15);
inline constexpr std::size_t N_MSGS = 1'000'000;
inline const char*           FEED_FILE = "build/itch_feed.bin";

struct FeedStats {
    std::uint64_t adds = 0, execs = 0, cancels = 0, deletes = 0;
    std::uint64_t exec_lots = 0, max_ref = 0;
};

inline void put_be16(std::vector<std::uint8_t>& b, std::uint16_t v) {
    b.push_back(std::uint8_t(v >> 8)); b.push_back(std::uint8_t(v));
}
inline void put_be32(std::vector<std::uint8_t>& b, std::uint32_t v) {
    for (int i = 3; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
inline void put_be48(std::vector<std::uint8_t>& b, std::uint64_t v) {
    for (int i = 5; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
inline void put_be64(std::vector<std::uint8_t>& b, std::uint64_t v) {
    for (int i = 7; i >= 0; --i) b.push_back(std::uint8_t(v >> (8 * i)));
}
inline void put_header(std::vector<std::uint8_t>& b, char type,
                       std::uint64_t ts) {
    b.push_back(std::uint8_t(type));
    put_be16(b, 1);                       // stock locate
    put_be16(b, 0);                       // tracking number
    put_be48(b, ts);                      // ns since midnight
}

inline FeedStats generate_feed(const char* path, std::size_t n_msgs,
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
// Producer sink: ITCH wire struct -> engine Message -> SPSC ring (the
// LEGACY struct-copying pipeline). All four operator() overloads inline
// into the parser's dispatch. Message.id carries the exchange ORDER
// REFERENCE; the engine core owns the ref -> engine-id table.
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

// Engine-side sink for the ZERO-COPY pipeline: decodes the ITCH wire struct
// (which lives in a stationary arena slot) and applies it to the book
// directly -- the Message below exists only in registers.
struct GatewaySink {
    Gateway& gw;

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
// Percentile reporter. Sorts in place; returns the key percentiles in ns.
// ---------------------------------------------------------------------------
struct Pcts { double p50, p90, p99, p999, mx; };

inline Pcts report(const char* name, std::vector<std::uint32_t>& cyc,
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
    return { pct(50) / ghz, pct(90) / ghz, pct(99) / ghz,
             pct(99.9) / ghz, cyc.back() / ghz };
}

} // namespace lob::bench
