#pragma once
// ============================================================================
// memory_pool.hpp -- Pre-allocated object arena. ZERO heap traffic after init.
//
// DESIGN NOTES
// ------------
// * ONE allocation, at construction time (the cold path). After that,
//   acquire()/release() are pointer pushes/pops on an intrusive free list:
//   O(1), branchless on the happy path, no syscalls, no locks, no malloc
//   metadata walks, and -- critically -- no page faults once the arena has
//   been pre-touched (we write every page up front so the OS maps physical
//   frames before trading starts; a soft page fault mid-session costs ~1us).
//
// * The free list is LIFO: the most recently released slot is handed out
//   next. That slot's cache line is almost certainly still resident in L1/L2,
//   so allocation "warmth" is maximised. A FIFO free list would cycle through
//   the whole arena and thrash the cache instead.
//
// * The free list reuses the object's own `next` pointer (intrusive again):
//   the pool needs no side-table and consumes zero extra memory per slot.
//
// * acquire() uses PLACEMENT NEW: we construct the object into pre-owned
//   storage. release() destroys it in place. `new`/`delete` never run on the
//   hot path -- the arena's backing store lives for the process lifetime.
// ============================================================================

#include "common.hpp"
#include <cstdlib>
#include <cstring>
#include <new>
#include <type_traits>

namespace lob {

template <typename T>
class MemoryPool {
    static_assert(alignof(T) >= alignof(void*),
                  "free-list reuse requires pointer-alignable storage");
public:
    // ---- COLD PATH: called once at startup, heap allocation allowed ----
    explicit MemoryPool(std::size_t capacity)
        : capacity_(capacity)
    {
        // Aligned to the object's own alignment (64 for Order) so the
        // "one object == one cache line" invariant holds across the arena.
#if defined(_WIN32)
        storage_ = static_cast<std::byte*>(_aligned_malloc(capacity * sizeof(T), alignof(T)));
#else
        storage_ = static_cast<std::byte*>(std::aligned_alloc(alignof(T), capacity * sizeof(T)));
#endif
        if (!storage_) std::abort();

        // Zero the whole arena: pre-touches every page (so the OS maps
        // physical frames NOW, not via a ~1us soft fault mid-session) and
        // guarantees never-used slots read as inert (e.g. Order::live ==
        // false), so a lookup of a stale/forged id is well-defined.
        std::memset(storage_, 0, capacity * sizeof(T));

        // Thread the free list through the zeroed slots. Each node's
        // lifetime is begun with placement new so all later accesses are
        // well-defined (no type-punning UB for the optimiser to exploit).
        // Building the list back-to-front means slot 0 is on top of the
        // LIFO stack, so early allocations walk memory sequentially --
        // perfect for the hardware prefetcher during warm-up.
        free_head_ = nullptr;
        for (std::size_t i = capacity; i-- > 0;) {
            free_head_ = ::new (static_cast<void*>(storage_ + i * sizeof(T)))
                             FreeNode{free_head_};
        }
    }

    ~MemoryPool() {
#if defined(_WIN32)
        _aligned_free(storage_);
#else
        std::free(storage_);
#endif
    }

    MemoryPool(const MemoryPool&)            = delete;
    MemoryPool& operator=(const MemoryPool&) = delete;

    // ---- HOT PATH ----------------------------------------------------------
    // Pop a slot off the free list and placement-new the object into it.
    // Cost: one predictable branch + one pointer chase (usually L1-hot).
    template <typename... Args>
    LOB_FORCE_INLINE T* acquire(Args&&... args) noexcept {
        FreeNode* n = free_head_;
        if (LOB_UNLIKELY(n == nullptr)) return nullptr;   // arena exhausted
        free_head_ = n->next;
        // PLACEMENT NEW: construct into storage we already own. This compiles
        // to plain member stores -- no allocator is invoked.
        return ::new (static_cast<void*>(n)) T(static_cast<Args&&>(args)...);
    }

    // Destroy in place and push the slot back. For trivially-destructible T
    // (like Order) the destructor call is elided entirely by the compiler.
    // The FreeNode overlay is placement-new'd so its lifetime formally
    // begins -- release() still compiles to exactly two stores.
    LOB_FORCE_INLINE void release(T* obj) noexcept {
        obj->~T();
        free_head_ = ::new (static_cast<void*>(obj)) FreeNode{free_head_};
    }
    // ------------------------------------------------------------------------

    // Slot index of an object (stable for the arena's lifetime). Lets us use
    // a flat array as the id -> order map instead of a hash table.
    LOB_FORCE_INLINE std::size_t index_of(const T* obj) const noexcept {
        return static_cast<std::size_t>(
            reinterpret_cast<const std::byte*>(obj) - storage_) / sizeof(T);
    }
    LOB_FORCE_INLINE T* at(std::size_t idx) const noexcept {
        return reinterpret_cast<T*>(storage_ + idx * sizeof(T));
    }

    std::size_t capacity() const noexcept { return capacity_; }

private:
    struct FreeNode { FreeNode* next; };  // overlays the dead object's bytes

    std::byte*  storage_   = nullptr;
    FreeNode*   free_head_ = nullptr;
    std::size_t capacity_  = 0;
};

} // namespace lob
