#pragma once
// ============================================================================
// arena_ring.hpp -- Circular Zero-Copy Ring-Buffer Arena (SPSC).
//
// WHAT THIS IS
// ------------
// A pre-allocated, fixed-size, contiguous array of cache-line-aligned slots,
// each large enough to hold one maximum-size ITCH frame, combined with the
// SPSC head/tail publication protocol from spsc_ring.hpp.
//
// The difference from SpscRing<T>: NOTHING is copied through the queue.
// The producer ("mock network layer" / NIC DMA in production) writes raw
// packet bytes ONCE into a slot; the queue publishes only a sequence number.
// The consumer receives a raw pointer to that same memory and decodes +
// executes DIRECTLY out of it. The payload is stationary from reception to
// consumption -- the only cross-core traffic is:
//     1. the slot's own cache line (unavoidable: that's the data), and
//     2. the head/tail index lines (amortised by the cached-peer trick).
//
// CONSTRAINT MAPPING
// ------------------
// * RING-FENCED SLOTS: `Slot` is alignas(CACHE_LINE) and SlotBytes must be a
//   whole multiple of the line size, so no two packets ever share a line and
//   a slot never straddles into its neighbour (no false sharing between the
//   producer writing slot i+1 and the consumer reading slot i).
//
// * PASS-BY-POINTER DEQUEUE: acquire()/front() hand out raw std::byte*.
//   There is no T, no operator=, no memcpy inside the queue itself.
//
// * CACHE LINE SEPARATION: head_ (producer-owned) and tail_ (consumer-owned)
//   live on distinct alignas(64) lines, each bundled with that side's
//   private shadow/cache indices so steady-state operation touches the
//   peer's line only when the ring looks full/empty.
//
// * COMPILER/HW BARRIERS: publication uses an EXPLICIT
//   std::atomic_thread_fence(release) before a relaxed head store, paired
//   with an explicit atomic_thread_fence(acquire) after the consumer's
//   relaxed head load. This is the standalone-fence formulation of the
//   release/acquire pairing: the fence orders ALL preceding slot writes
//   (plain, non-atomic stores) before the index becomes visible, so the
//   consumer core can never observe the new head yet read stale slot bytes.
//   On x86 both fences compile to zero instructions (TSO) -- they exist to
//   stop the COMPILER reordering across the publication point, and to keep
//   the code correct on ARM/POWER.
// ============================================================================

#include "common.hpp"
#include <atomic>

namespace lob {

template <std::size_t SlotBytes, std::size_t CapacityPow2>
class SpscArenaRing {
    static_assert((CapacityPow2 & (CapacityPow2 - 1)) == 0 && CapacityPow2 >= 2,
                  "capacity must be a power of two");
    static_assert(SlotBytes % CACHE_LINE == 0,
                  "slot must be a whole number of cache lines");
    static constexpr std::uint64_t MASK = CapacityPow2 - 1;

    struct alignas(CACHE_LINE) Slot { std::byte bytes[SlotBytes]; };

public:
    SpscArenaRing() = default;
    SpscArenaRing(const SpscArenaRing&)            = delete;
    SpscArenaRing& operator=(const SpscArenaRing&) = delete;

    static constexpr std::size_t slot_bytes() noexcept { return SlotBytes; }

    // ---- PRODUCER SIDE (exactly one thread) --------------------------------
    // Claim the next slot for writing. Returns nullptr if the ring is full
    // (caller spins / drops / backpressures). No atomic ops on the fast path:
    // head_shadow_ is producer-private; tail_ is only re-read on apparent full.
    LOB_FORCE_INLINE std::byte* try_acquire() noexcept {
        if (LOB_UNLIKELY(head_shadow_ - tail_cache_ >= CapacityPow2)) {
            tail_cache_ = tail_.load(std::memory_order_acquire);
            if (head_shadow_ - tail_cache_ >= CapacityPow2) return nullptr;
        }
        return slots_[head_shadow_ & MASK].bytes;
    }

    // Publish the slot written after the last try_acquire(). The release
    // fence guarantees every byte stored into the slot is visible to the
    // consumer core BEFORE the incremented head index is.
    LOB_FORCE_INLINE void publish() noexcept {
        std::atomic_thread_fence(std::memory_order_release);   // slot -> index
        head_.store(++head_shadow_, std::memory_order_relaxed);
    }

    // ---- CONSUMER SIDE (exactly one thread) --------------------------------
    // Peek the oldest unconsumed slot. Returns nullptr if empty. The acquire
    // fence (issued only when a NEW head value is first observed) pairs with
    // the producer's release fence: after it, the slot bytes are guaranteed
    // current.
    LOB_FORCE_INLINE const std::byte* try_front() noexcept {
        if (LOB_UNLIKELY(tail_shadow_ >= head_cache_)) {
            head_cache_ = head_.load(std::memory_order_relaxed);
            if (tail_shadow_ >= head_cache_) return nullptr;    // truly empty
            std::atomic_thread_fence(std::memory_order_acquire); // index -> slot
        }
        return slots_[tail_shadow_ & MASK].bytes;
    }

    // Hand the slot back to the producer. Must be called after the consumer
    // has finished READING the slot returned by try_front(). The release
    // store pairs with the producer's acquire load of tail_ so the producer
    // can never overwrite bytes the consumer is still reading.
    LOB_FORCE_INLINE void release() noexcept {
        tail_.store(++tail_shadow_, std::memory_order_release);
    }

    LOB_FORCE_INLINE std::size_t size_approx() const noexcept {
        return static_cast<std::size_t>(head_.load(std::memory_order_acquire) -
                                        tail_.load(std::memory_order_acquire));
    }

private:
    // --- producer's private line: published head + private shadow/cache ---
    alignas(CACHE_LINE) std::atomic<std::uint64_t> head_{0};
    std::uint64_t head_shadow_{0};     // producer-only mirror of head_
    std::uint64_t tail_cache_{0};      // producer's stale view of tail_

    // --- consumer's private line ---
    alignas(CACHE_LINE) std::atomic<std::uint64_t> tail_{0};
    std::uint64_t tail_shadow_{0};     // consumer-only mirror of tail_
    std::uint64_t head_cache_{0};      // consumer's stale view of head_

    // --- the arena: contiguous, pre-allocated, line-aligned slots ---
    alignas(CACHE_LINE) Slot slots_[CapacityPow2];
};

} // namespace lob
