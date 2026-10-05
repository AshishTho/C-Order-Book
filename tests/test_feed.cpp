#include "lob/itch.hpp"
#include "lob/signals.hpp"
#include "lob/bbo.hpp"
#include "lob/journal.hpp"
#include "lob/spsc_ring.hpp"

#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <random>
#include <type_traits>
#include <vector>

#define CHECK(condition) do { if (!(condition)) { \
    std::fprintf(stderr, "FAILED: %s (%s:%d)\n", #condition, __FILE__, __LINE__); \
    std::abort(); } } while (0)

using namespace lob;

struct CountingSink {
    unsigned count = 0;
    std::uint64_t reference = 0;
    template <typename T> void operator()(const T& message) noexcept {
        ++count;
        reference = itch::bswap(message.order_ref);
    }
};

template <typename T>
std::vector<std::byte> frame(const T& message) {
    std::vector<std::byte> bytes(sizeof(T) + 2);
    bytes[0] = std::byte(sizeof(T) >> 8);
    bytes[1] = std::byte(sizeof(T) & 255);
    std::memcpy(bytes.data() + 2, &message, sizeof(message));
    return bytes;
}

void parser_tests() {
    itch::AddOrder add{};
    add.h.type = 'A'; add.order_ref = itch::bswap(std::uint64_t(42));
    const auto wire = frame(add);
    CountingSink sink;
    itch::ItchParser empty(nullptr, 0);
    CHECK(!empty.next(sink)); CHECK(empty.exhausted());
    CHECK(empty.status() == itch::ParseStatus::End);
    CHECK(empty.cursor() == nullptr);
    itch::ItchParser null(nullptr, 5);
    CHECK(!null.next(sink)); CHECK(!null.exhausted());
    CHECK(null.status() == itch::ParseStatus::NullInput);

    // Every truncated prefix must stop without invoking a sink or reading past it.
    for (std::size_t n = 1; n < wire.size(); ++n) {
        itch::ItchParser truncated(wire.data(), n);
        CHECK(!truncated.next(sink)); CHECK(sink.count == 0);
        CHECK(truncated.offset() == 0);
        CHECK(truncated.status() == (n == 1 ? itch::ParseStatus::TruncatedLength
                                          : itch::ParseStatus::TruncatedPayload));
        CHECK(!truncated.next(sink)); // errors are sticky
    }
    const std::array<std::byte, 2> zero{};
    itch::ItchParser zero_parser(zero.data(), zero.size());
    CHECK(!zero_parser.next(sink)); CHECK(zero_parser.status() == itch::ParseStatus::EmptyFrame);
    const std::array<std::byte, 3> short_add{std::byte{0}, std::byte{1}, std::byte{'A'}};
    itch::ItchParser short_parser(short_add.data(), short_add.size());
    CHECK(!short_parser.next(sink)); CHECK(short_parser.status() == itch::ParseStatus::InvalidLength);
    const std::array<std::byte, 3> unknown{std::byte{0}, std::byte{1}, std::byte{'S'}};
    itch::ItchParser unsupported(unknown.data(), unknown.size());
    CHECK(!unsupported.next(sink));
    CHECK(unsupported.status() == itch::ParseStatus::UnsupportedType);
    CHECK(unsupported.message_type() == 'S');

    // A deliberately odd address verifies that length and payload decoding do
    // not rely on natural alignment or struct objects in the source buffer.
    std::vector<std::byte> odd(wire.size() + 1);
    std::memcpy(odd.data() + 1, wire.data(), wire.size());
    itch::ItchParser valid(odd.data() + 1, wire.size());
    CHECK(valid.next(sink)); CHECK(sink.count == 1); CHECK(sink.reference == 42);
    CHECK(valid.offset() == wire.size()); CHECK(valid.exhausted());
    CHECK(!valid.next(sink)); CHECK(valid.status() == itch::ParseStatus::End);

    itch::OrderExecuted execution{}; execution.h.type = 'E';
    itch::OrderCancel cancel{}; cancel.h.type = 'X';
    itch::OrderDelete deletion{}; deletion.h.type = 'D';
    auto stream = wire;
    for (const auto& bytes : {frame(execution), frame(cancel), frame(deletion)})
        stream.insert(stream.end(), bytes.begin(), bytes.end());
    stream.push_back(std::byte{0}); // bad tail after four valid messages
    CountingSink all;
    itch::ItchParser sequence(stream.data(), stream.size());
    while (sequence.next(all)) {}
    CHECK(all.count == 4); CHECK(sequence.offset() == stream.size() - 1);
    CHECK(sequence.status() == itch::ParseStatus::TruncatedLength);
    std::puts("  bounded ITCH parsing ........ OK");
}

struct QuoteBook {
    Price bid = -1, ask = -1;
    Qty bid_size = 0, ask_size = 0;
    Price best_bid() const noexcept { return bid; }
    Price best_ask() const noexcept { return ask; }
    Qty level_qty(Price price) const noexcept { return price == bid ? bid_size : ask_size; }
};

void signal_tests() {
    QuoteBook book;
    SpscRing<SignalUpdate, 16> ring{};
    SignalPublisher publisher(book, ring);
    SignalUpdate update{};
    publisher.on_message_applied(0); CHECK(!ring.try_pop(update));
    auto observe = [&](std::int64_t ofi, std::int64_t micro) {
        publisher.on_message_applied(9);
        CHECK(ring.try_pop(update)); CHECK(update.ofi == ofi); CHECK(update.micro_px == micro);
        CHECK(update.origin_tsc == 9); CHECK(!ring.try_pop(update));
    };
    book.ask = 110; book.ask_size = 30; observe(-30, -1);
    book.bid = 100; book.bid_size = 10; observe(-20, 102 * 256 + 128);
    book.ask_size = 35; observe(-25, (100 * 35 + 110 * 10) * 256 / 45);
    book.bid = 101; book.bid_size = 20; observe(-5, (101 * 35 + 110 * 20) * 256 / 55);
    book.ask = -1; book.ask_size = 0; observe(30, -1);
    book.bid = -1; book.bid_size = 0; observe(10, -1);
    CHECK(publisher.saturations() == 0);

    constexpr Qty maximum = (std::numeric_limits<Qty>::max)();
    constexpr Price high = 4'000'000'000;
    CHECK(weighted_midpoint_q8(high, high + 10, maximum, maximum) == (high + 5) * 256);
    CHECK(weighted_midpoint_q8(100, 110, maximum, 1) == 110 * 256 - 1);
    CHECK(weighted_midpoint_q8(100, 110, 1, maximum) == 100 * 256);
    CHECK(weighted_midpoint_q8(101, 100, 10, 10) == -1);
    CHECK(weighted_midpoint_q8(-1, 100, 0, 10) == -1);
    CHECK(weighted_midpoint_q8(100, 110, -1, 10) == -1);
    CHECK(weighted_midpoint_q8(100, maximum, 10, 10) == -1);
    // Independently compute exact expected values within uint64 bounds.
    std::mt19937_64 random(917);
    for (int i = 0; i < 10000; ++i) {
        const auto bp = Price(random() % 1'000'000);
        const auto ap = bp + Price(random() % 1000);
        const auto bq = Qty(random() % 1'000'000 + 1), aq = Qty(random() % 1'000'000 + 1);
        CHECK(weighted_midpoint_q8(bp, ap, bq, aq) == (bp * aq + ap * bq) * 256 / (bq + aq));
        const auto a = random() >> 1;
        const auto d = random() | 1;
        const auto b = random() % d;
        CHECK(signal_detail::mul_div_portable(a, b, d) == signal_detail::mul_div(a, b, d));
    }

    QuoteBook large;
    SpscRing<SignalUpdate, 2> wide_ring{};
    SignalPublisher wide(large, wide_ring);
    large.bid = high; large.bid_size = 9'000'000'000;
    wide.on_message_applied(1); CHECK(wide_ring.try_pop(update));
    CHECK(update.bid_px == high); CHECK(update.bid_qty == 9'000'000'000);
    large.bid_size = maximum; wide.on_message_applied(2); CHECK(wide_ring.try_pop(update));
    large.bid += 1; wide.on_message_applied(3); CHECK(wide_ring.try_pop(update));
    CHECK(wide.saturations() > 0); CHECK(update.ofi == maximum);
    std::puts("  OFI / exact weighted midpoint OK");
}

template <typename Update, template <typename, typename> class Publisher>
void conflation_test() {
    QuoteBook book;
    SpscRing<Update, 2> ring{};
    Publisher<QuoteBook, decltype(ring)> publisher(book, ring);
    book.bid = 100;
    for (int size = 1; size <= 4; ++size) {
        book.bid_size = size;
        publisher.on_message_applied(std::uint64_t(size));
    }
    CHECK(publisher.pending()); CHECK(publisher.published() == 2); CHECK(publisher.dropped() == 2);
    Update update{};
    CHECK(ring.try_pop(update)); CHECK(update.seq == 1);
    CHECK(ring.try_pop(update)); CHECK(update.seq == 2);
    publisher.on_message_applied(99); // unchanged event retries without changing original timestamp
    CHECK(ring.try_pop(update)); CHECK(update.seq == 4);
    CHECK(update.bid_qty == 4); CHECK(update.origin_tsc == 4);
    CHECK(!publisher.pending()); CHECK(publisher.flush_pending()); CHECK(!ring.try_pop(update));
    book.bid_size = 5; publisher.on_message_applied(5);
    book.bid_size = 6; publisher.on_message_applied(6);
    book.bid_size = 7; publisher.on_message_applied(7);
    CHECK(publisher.pending());
    CHECK(ring.try_pop(update)); CHECK(ring.try_pop(update));
    CHECK(publisher.flush_pending()); // quiet-feed recovery
    CHECK(ring.try_pop(update)); CHECK(update.seq == 7); CHECK(update.bid_qty == 7);
}

void journal_tests() {
    static_assert(!std::is_copy_constructible_v<JournalReplay>);
    const auto path = std::filesystem::temp_directory_path() /
        ("lob_feed_test_" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) + ".bin");
    const auto filename = path.string();
    Journal journal;
    JournalReplay replay;
    CHECK(replay.data() == nullptr); CHECK(replay.size() == 0);
    CHECK(!journal.append(nullptr, 1)); CHECK(!journal.flush());
    itch::OrderDelete message{}; message.h.type = 'D';
    const auto bytes = frame(message);
    CHECK(journal.open(filename.c_str(), bytes.size() + 1));
    CHECK(journal.append(bytes.data(), bytes.size()));
    CHECK(journal.committed() == bytes.size());
    CHECK(!journal.append(bytes.data(), bytes.size())); CHECK(journal.failed());
    CHECK(!journal.append(bytes.data(), 1)); // preserve a prefix after first drop
    CHECK(journal.dropped() == bytes.size() + 1); CHECK(journal.committed() == bytes.size());
    CHECK(journal.flush()); journal.close();
    CHECK(journal.committed() == 0); CHECK(!journal.append(bytes.data(), 1));
    CHECK(replay.open(filename.c_str())); CHECK(replay.size() == bytes.size());
    CHECK(replay.dropped() == bytes.size() + 1);
    CHECK(std::memcmp(replay.data(), bytes.data(), bytes.size()) == 0);
    CountingSink sink;
    itch::ItchParser parser(replay.data(), replay.size());
    CHECK(parser.next(sink)); CHECK(!parser.next(sink)); CHECK(parser.exhausted());
    CHECK(replay.open(filename.c_str())); // reopen closes old mapping safely
    replay.close(); CHECK(replay.data() == nullptr); CHECK(replay.size() == 0);
    CHECK(!journal.open(filename.c_str(), (std::numeric_limits<std::size_t>::max)()));

    auto write_header = [&](JournalHeader header, std::size_t size) {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        CHECK(bool(out));
        if (size >= sizeof(header)) out.write(reinterpret_cast<const char*>(&header), sizeof(header));
        if (size > 0) { out.seekp(std::streamoff(size - 1)); out.put('\0'); }
        out.close(); CHECK(!out.fail());
    };
    write_header({JOURNAL_MAGIC, 1, 0, 0}, 0); CHECK(!replay.open(filename.c_str()));
    write_header({JOURNAL_MAGIC, 1, 0, 0}, sizeof(JournalHeader)); CHECK(!replay.open(filename.c_str()));
    write_header({0, 0, 0, 0}, JOURNAL_HDR); CHECK(!replay.open(filename.c_str()));
    write_header({JOURNAL_MAGIC, 2, 0, 0}, JOURNAL_HDR + 1); CHECK(!replay.open(filename.c_str()));
    write_header({JOURNAL_MAGIC, 1, 2, 0}, JOURNAL_HDR + 1); CHECK(!replay.open(filename.c_str()));
    write_header({JOURNAL_MAGIC, 0, 0, 0}, JOURNAL_HDR); CHECK(replay.open(filename.c_str()));
    CHECK(replay.size() == 0); replay.close();
    CHECK(journal.open(filename.c_str(), 1));
    CHECK(journal.open(filename.c_str(), 1)); // writer reopen releases old OS handles
    CHECK(journal.append(nullptr, 0)); CHECK(!journal.append(nullptr, 1));
    journal.close();
    CHECK(std::filesystem::remove(path));
    std::puts("  journal bounds / round-trip .. OK");
}

int main() {
    parser_tests();
    signal_tests();
    conflation_test<BboUpdate, BboPublisher>();
    conflation_test<SignalUpdate, SignalPublisher>();
    std::puts("  final snapshot conflation .... OK");
    journal_tests();
    std::puts("Feed, signal, and journal regression tests passed.");
}
