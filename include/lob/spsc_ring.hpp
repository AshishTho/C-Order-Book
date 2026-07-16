#pragma once
// ============================================================================
// spsc_ring.hpp -- Lock-free Single-Producer/Single-Consumer ring buffer.
//                  (LMAX Disruptor pattern, bounded, wait-free per op.)
//
// DESIGN NOTES
// ------------
// * MEMORY ORDERING. The only synchronisation is a release-store of the
//   producer's head (publishes the written slot) paired with an
//   acquire-load by the consumer (makes the slot's contents visible), and
//   symmetrically for the tail on the return path. On x86 these compile to
//   plain MOVs (TSO gives acq/rel for free) -- the std::atomic annotations
//   cost nothing at runtime but keep the code correct on ARM and stop the
//   COMPILER from reordering across the publication point. seq_cst would
//   emit an unnecessary LOCK XCHG / MFENCE (~20ns each); mutexes would add
//   syscalls and priority-inversion risk. Neither is acceptable.
//
// * FALSE SHARING. head_ (producer-written) and tail_ (consumer-written)
//   each get a private alignas(64) cache line. If they shared a line, every
//   enqueue would invalidate the consumer's cached copy of tail_ and vice
//   versa -- the classic ping-pong that turns 5ns ops into 100ns ops.
//
// * CACHED PEER INDEX. The producer keeps a stale local copy of tail_
//   (tail_cache_) and only re-reads the real atomic when the ring LOOKS
//   full; the consumer mirrors this with head_cache_. This is the single
//   biggest Disruptor trick: in steady state each side runs entirely out of
//   its own cache line and touches the shared line only once per wrap.
//
// * POWER-OF-TWO capacity: index masking is one AND instead of a divide
//   (integer div = 20-40 cycles; and = 1).
//
// * Indices are monotonically increasing uint64 sequence numbers (never
//   wrapped). Full = head - tail == capacity. A uint64 at 1 billion
//   msgs/sec takes 584 years to overflow.
// ============================================================================

#include "common.hpp"
#include <atomic>

namespace lob {

template <typename T, std::size_t CapacityPow2>
class SpscRing {
    static_assert((CapacityPow2 & (CapacityPow2 - 1)) == 0 && CapacityPow2 >= 2,
                  "capacity must be a power of two");
    static constexpr std::size_t MASK = CapacityPow2 - 1;

public:
    SpscRing() = default;
    SpscRing(const SpscRing&)            = delete;
    SpscRing& operator=(const SpscRing&) = delete;

    // ---- PRODUCER SIDE (exactly one thread) --------------------------------
    // Wait-free. Returns false if the ring is full (caller decides policy:
    // spin, drop, or backpressure upstream).
    LOB_FORCE_INLINE bool try_push(const T& item) noexcept {
        const std::uint64_t head = head_.load(std::memory_order_relaxed); // own var
        if (LOB_UNLIKELY(head - tail_cache_ >= CapacityPow2)) {
            // Looks full -- refresh the cached consumer position. ACQUIRE
            // pairs with the consumer's release-store of tail_: it guarantees
            // the consumer really has finished READING any slot we are about
            // to overwrite.
            tail_cache_ = tail_.load(std::memory_order_acquire);
            if (head - tail_cache_ >= CapacityPow2) return false;   // truly full
        }
        buf_[head & MASK] = item;
        // RELEASE publishes the slot: the consumer's acquire-load of head_
        // is guaranteed to see the buf_ write above, never a torn/stale slot.
        head_.store(head + 1, std::memory_order_release);
        return true;
    }

    // ---- CONSUMER SIDE (exactly one thread) --------------------------------
    LOB_FORCE_INLINE bool try_pop(T& out) noexcept {
        const std::uint64_t tail = tail_.load(std::memory_order_relaxed); // own var
        if (LOB_UNLIKELY(tail >= head_cache_)) {
            // Looks empty -- refresh cached producer position. ACQUIRE pairs
            // with the producer's release-store of head_ (see above).
            head_cache_ = head_.load(std::memory_order_acquire);
            if (tail >= head_cache_) return false;                  // truly empty
        }
        out = buf_[tail & MASK];
        // RELEASE hands the slot back: the producer's acquire-load of tail_
        // cannot observe the new tail before our read of buf_ completed.
        tail_.store(tail + 1, std::memory_order_release);
        return true;
    }

    LOB_FORCE_INLINE std::size_t size_approx() const noexcept {
        return static_cast<std::size_t>(head_.load(std::memory_order_acquire) -
                                        tail_.load(std::memory_order_acquire));
    }

private:
    // --- producer's private line: its own index + cached view of consumer ---
    alignas(CACHE_LINE) std::atomic<std::uint64_t> head_{0};
    std::uint64_t tail_cache_{0};      // same line as head_: only producer touches it

    // --- consumer's private line ---
    alignas(CACHE_LINE) std::atomic<std::uint64_t> tail_{0};
    std::uint64_t head_cache_{0};      // same line as tail_: only consumer touches it

    // --- the data slots (padded away from the index lines) ---
    alignas(CACHE_LINE) T buf_[CapacityPow2];
};

} // namespace lob
