#pragma once
// ============================================================================
// rdtsc.hpp -- Hardware timestamp-counter measurement primitives.
//
// WHY RDTSC AND NOT std::chrono
// -----------------------------
// std::chrono::high_resolution_clock on Windows routes through QPC (~20-30ns
// per call, kernel-mediated); on Linux through a vDSO clock_gettime (~20ns).
// When the thing being measured takes 20-50ns, the clock itself would be
// the dominant cost. RDTSC reads the CPU's invariant TSC register directly:
// ~7 cycles, userspace-only, and on every CPU from the last 15 years it is
// INVARIANT -- it ticks at a constant rate regardless of frequency scaling
// or C-states, so it is a legitimate wall-clock proxy.
//
// SERIALISATION: RDTSC alone may execute BEFORE earlier instructions retire
// (out-of-order execution) -- your "stop" timestamp could be taken before
// the measured work finished. The standard fix:
//   start:  serialise, then read     -> CPUID/LFENCE ; RDTSC
//   stop :  RDTSCP (waits for prior instructions) ; LFENCE (stops later
//           instructions creeping up)
// We wrap these as tsc_begin()/tsc_end().
// ============================================================================

#include <cstdint>

#if defined(_MSC_VER)
#  include <intrin.h>
#else
#  include <x86intrin.h>
#endif

#include <chrono>
#include <thread>

namespace lob {

// Serialising "start" read: LFENCE guarantees all older instructions have
// completed before RDTSC executes (Intel SDM-sanctioned, far cheaper than
// the traditional CPUID which costs 100+ cycles and would pollute results).
inline std::uint64_t tsc_begin() noexcept {
    _mm_lfence();
    return __rdtsc();
}

// Serialising "stop" read: RDTSCP waits until every preceding instruction
// has executed; the trailing LFENCE stops subsequent instructions from
// starting before the read (which would not skew THIS sample but keeps the
// next sample's work out of this window).
inline std::uint64_t tsc_end() noexcept {
    unsigned aux;
    const std::uint64_t t = __rdtscp(&aux);
    _mm_lfence();
    return t;
}

// --------------------------------------------------------------------------
// Calibration: measure TSC ticks per nanosecond against the steady clock.
// Cold path only (called once before the run).
// --------------------------------------------------------------------------
inline double calibrate_tsc_ghz() {
    using clock = std::chrono::steady_clock;
    const auto t0 = clock::now();
    const std::uint64_t c0 = __rdtsc();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    const std::uint64_t c1 = __rdtsc();
    const auto t1 = clock::now();
    const double ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();
    return static_cast<double>(c1 - c0) / ns;   // ticks per ns == GHz
}

// Measurement overhead (the tsc_begin/tsc_end pair itself), to subtract
// from every sample. Determined empirically at startup.
inline std::uint64_t measure_tsc_overhead() {
    std::uint64_t best = ~0ull;
    for (int i = 0; i < 10'000; ++i) {
        const std::uint64_t a = tsc_begin();
        const std::uint64_t b = tsc_end();
        if (b - a < best) best = b - a;
    }
    return best;
}

} // namespace lob
