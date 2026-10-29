#include "lob/flat_hash.hpp"

#include <cstdio>
#include <cstdlib>
#include <limits>
#include <random>
#include <unordered_map>
#include <vector>

#define CHECK(condition) do { if (!(condition)) { \
    std::fprintf(stderr, "check failed at line %d: %s\n", __LINE__, #condition); \
    std::abort(); \
} } while (false)

static void reserved_key_is_a_noop() {
    lob::FlatHashMap<8> map;
    CHECK(!map.erase(0));
    CHECK(map.size() == 0);
    CHECK(!map.insert(0, 42));
    CHECK(map.size() == 0 && map.find(0) == 0);

    // Include keys whose hash home is zero: the sentinel must not alter
    // occupied probe chains or consume capacity, even on a populated map.
    std::vector<std::uint64_t> keys;
    for (std::uint64_t key = 1; keys.size() < 4; ++key) {
        if (decltype(map)::slot_of(key) == 0) {
            keys.push_back(key);
            CHECK(map.insert(key, key + 100));
        }
    }
    CHECK(!map.insert(0, 99));
    CHECK(!map.erase(0));
    CHECK(map.size() == keys.size());
    for (auto key : keys) CHECK(map.find(key) == key + 100);
    for (auto key : keys) CHECK(map.erase(key));
    CHECK(map.size() == 0);
    CHECK(!map.erase(0));
    CHECK(map.size() == 0);
}

static void overwrite_at_capacity() {
    lob::FlatHashMap<4> map;
    constexpr auto largest = std::numeric_limits<std::uint64_t>::max();
    CHECK(map.insert(1, 10));
    CHECK(map.insert(largest, 20));
    CHECK(map.size() == 2);
    CHECK(map.insert(1, 11));
    CHECK(map.insert(largest, 21));
    CHECK(map.size() == 2);
    CHECK(map.find(1) == 11 && map.find(largest) == 21);
    CHECK(!map.insert(2, 30));
    CHECK(map.size() == 2 && map.find(2) == 0);
    CHECK(map.erase(1));
    CHECK(map.insert(2, 30));
    CHECK(map.size() == 2 && map.find(2) == 30);
    CHECK(map.find(largest) == 21);
}

template <std::size_t Capacity, unsigned Load>
static void compare_with_reference() {
    lob::FlatHashMap<Capacity, Load> map;
    std::unordered_map<std::uint64_t, std::uint64_t> expected;
    std::mt19937_64 random(20261029);
    constexpr auto cap = Capacity * Load / 100;
    constexpr std::uint64_t spread = 0xC2B2AE3D27D4EB4Full;
    // Seed at capacity, so overwrites and rejected insertions occur even
    // in tiny maps. Odd multiplication generates distinct sparse keys.
    for (std::uint64_t i = 1; i <= cap; ++i) {
        CHECK(map.insert(i * spread, i));
        expected[i * spread] = i;
    }
    for (std::uint64_t step = 1; step <= 10000; ++step) {
        const auto key = (random() % (Capacity + 2)) * spread;
        const auto op = random() % 3;
        if (op == 0) {
            const bool accept = key != 0 &&
                (expected.count(key) != 0 || expected.size() < cap);
            CHECK(map.insert(key, step) == accept);
            if (accept) expected[key] = step;
        } else if (op == 1) {
            const bool removed = expected.erase(key) != 0;
            CHECK(map.erase(key) == removed);
        } else {
            const auto found = expected.find(key);
            CHECK(map.find(key) == (found == expected.end() ? 0 : found->second));
        }
        CHECK(map.size() == expected.size());
        for (std::uint64_t i = 0; i < Capacity + 2; ++i) {
            const auto candidate = i * spread;
            const auto found = expected.find(candidate);
            CHECK(map.find(candidate) == (found == expected.end() ? 0 : found->second));
        }
    }
}

int main() {
    reserved_key_is_a_noop();
    overwrite_at_capacity();
    compare_with_reference<4, 50>();
    compare_with_reference<8, 90>();
    compare_with_reference<64, 50>();
    std::puts("flat hash boundaries: sentinel, capacity and 30000 oracle operations passed");
}
