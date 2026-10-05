// Strict single-instrument L3 replay. File I/O and the reference map belong
// to offline tooling, outside the matching engine's allocation-free hot path.
#include "lob/order_book.hpp"
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <string_view>
#include <unordered_map>

namespace {
using Book = lob::OrderBook<1u << 16, 1u << 16>;
struct Live { lob::OrderId id; lob::Qty qty; };
template<class T> bool number(std::string_view text, T& value) {
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    return !text.empty() && result.ec == std::errc{} && result.ptr == text.data() + text.size();
}
bool fields(const std::string& line, std::array<std::string_view, 6>& out) {
    std::size_t begin = 0;
    for (std::size_t i = 0; i < out.size(); ++i) {
        const auto end = line.find(',', begin);
        if ((i + 1 == out.size()) != (end == std::string::npos)) return false;
        out[i] = std::string_view(line).substr(begin, end == std::string::npos ? end : end - begin);
        begin = end + 1;
    }
    return true;
}
}

int main(int argc, char** argv) {
    std::string input, output;
    lob::Price base = 0;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--help") {
            std::cout << "replay_events --input events.csv --output bbo.csv [--base-tick 0]\n"
                      << "Ladder: [base, base+65535]; max live orders: 65536. Integer tick prices.\n";
            return 0;
        }
        if (i + 1 >= argc) { std::cerr << "Missing argument value\n"; return 2; }
        const std::string value = argv[++i];
        if (arg == "--input") input = value;
        else if (arg == "--output") output = value;
        else if (arg == "--base-tick" && number(value, base)) {}
        else { std::cerr << "Unknown or invalid option: " << arg << '\n'; return 2; }
    }
    if (input.empty() || output.empty() || base < 0 ||
        base > std::numeric_limits<lob::Price>::max() - 65535) {
        std::cerr << "Input/output and a nonnegative, representable base tick are required\n";
        return 2;
    }
    std::error_code ec;
    if (std::filesystem::exists(output, ec)) {
        std::cerr << "Output already exists; choose a new path to preserve earlier results\n";
        return 2;
    }
    const std::string temporary = output + ".partial";
    if (std::filesystem::exists(temporary, ec)) {
        std::cerr << "Temporary output already exists: " << temporary << '\n'; return 2;
    }
    std::ifstream src(input, std::ios::binary);
    std::string line;
    if (!std::getline(src, line)) { std::cerr << "Cannot read input/header\n"; return 2; }
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line != "seq,type,order_ref,side,price,qty") {
        std::cerr << "Expected header seq,type,order_ref,side,price,qty\n"; return 2;
    }
    std::ofstream dst(temporary);
    if (!dst) { std::cerr << "Cannot create output\n"; return 2; }
    auto fail = [&](std::size_t row, const char* why) {
        std::cerr << "line " << row << ": " << why << '\n';
        dst.close(); std::filesystem::remove(temporary, ec); return 1;
    };
    dst << "seq,bid_price,ask_price,bid_qty,ask_qty\n";
    Book book(base);
    std::unordered_map<std::uint64_t, Live> live;
    live.reserve(65536);
    std::uint64_t prior = 0, snapshots = 0, events = 0;
    std::size_t row = 1;
    while (std::getline(src, line)) {
        ++row;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::array<std::string_view, 6> f;
        std::uint64_t seq = 0, ref = 0;
        lob::Price price = 0; lob::Qty qty = 0;
        if (!fields(line, f) || !number(f[0], seq) || !number(f[2], ref) ||
            !number(f[4], price) || !number(f[5], qty) || ref == 0)
            return fail(row, "invalid CSV fields or numeric value");
        if (seq == 0 || seq <= prior || (events && seq - prior != 1))
            return fail(row, "sequence must be positive and contiguous within this session");
        if (f[1] == "A") {
            if (live.count(ref)) return fail(row, "duplicate live order reference");
            if (qty <= 0 || price < base || price - base >= 65536 ||
                (f[3] != "B" && f[3] != "S")) return fail(row, "invalid add side/price/quantity");
            const auto side = f[3] == "B" ? lob::Side::Buy : lob::Side::Sell;
            // Historical adds must never silently match against this book.
            const auto id = book.new_order(side, price, qty, [](const lob::Trade&) {}, true);
            if (!id) return fail(row, "add rejected: crossed book, pool full, or depth overflow");
            live.emplace(ref, Live{id, qty});
        } else if (f[1] == "E" || f[1] == "X" || f[1] == "D") {
            if (f[3] != "-" || price != 0) return fail(row, "non-add requires side '-' and price 0");
            const auto it = live.find(ref);
            if (it == live.end()) return fail(row, "unknown/dead order reference");
            if (f[1] == "D") {
                if (qty != 0) return fail(row, "delete requires quantity 0");
                if (!book.cancel(it->second.id)) return fail(row, "engine cancel mismatch");
                live.erase(it);
            } else {
                if (qty <= 0 || qty > it->second.qty) return fail(row, "reduction exceeds remaining quantity or is nonpositive");
                if (book.reduce(it->second.id, qty) != qty) return fail(row, "engine quantity mismatch");
                it->second.qty -= qty;
                if (!it->second.qty) live.erase(it);
            }
        } else return fail(row, "unsupported event type (use A/E/X/D)");
        prior = seq; ++events;
        const auto bid = book.best_bid(), ask = book.best_ask();
        if (bid >= 0 && ask >= 0) {
            if (bid >= ask) return fail(row, "crossed final book");
            dst << seq << ',' << bid << ',' << ask << ',' << book.level_qty(bid) << ',' << book.level_qty(ask) << '\n';
            ++snapshots;
        }
    }
    if (src.bad() || !events || !snapshots) return fail(row, "read error or no valid two-sided observations");
    dst.flush();
    if (!dst) return fail(row, "output write failed");
    dst.close();
    std::filesystem::rename(temporary, output, ec);
    if (ec) return fail(row, "could not publish output");
    std::cout << "{\"events\":" << events << ",\"snapshots\":" << snapshots
              << ",\"live_orders\":" << live.size() << ",\"price_unit\":\"tick\"}\n";
    return 0;
}
