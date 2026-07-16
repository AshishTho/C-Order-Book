#pragma once
// ============================================================================
// journal.hpp -- Deterministic replay journal (event sourcing).
//                PHASE 5: institutional-grade resiliency.
//
// PRINCIPLE
// ---------
// The matching engine is a DETERMINISTIC function of its inbound message
// sequence: same bytes in => same book, same trades, same ids, bit for bit.
// So the complete recovery/audit/debug story is simply: KEEP THE BYTES.
//
//     live:    wire frame --> arena slot --> engine
//                    \--> Journal::append() (raw, byte-identical)
//
//     replay:  journal file --> ItchParser --> fresh engine
//              == provably the exact state the live engine reached.
//
// The journal is a BYTE-IDENTICAL copy of the length-framed wire stream --
// no records, no headers per message, no re-encoding. Replay therefore
// exercises THE SAME parser and THE SAME engine code as production, which
// is what makes the reconstruction claim provable rather than hopeful.
//
// WHY THE HOT PATH NEVER BLOCKS ON DISK (constraint 2)
// ----------------------------------------------------
// The file is pre-sized and MEMORY-MAPPED at open (cold path). append() is
// a ~40-byte memcpy into the mapping plus one release-store of the commit
// watermark -- it touches PAGE CACHE, not the device. The OS writes dirty
// pages back asynchronously on its own schedule; no syscall, no flush, no
// buffer management ever appears on the hot path. All pages are pre-
// touched at open() so appending never takes a soft page fault either.
// (A production deployment adds a background thread issuing periodic
// FlushViewOfFile/msync for a bounded-data-loss guarantee against OS/power
// failure; PROCESS crashes lose nothing -- the page cache survives them.)
//
// WHO CALLS append(): the FEED thread, right where it lands bytes into the
// arena -- the source buffer is in L1 at that instant, so the journal copy
// is the cheapest it can ever be. The MATCHING thread is untouched: its
// code does not change by one instruction when journaling is enabled.
//
// CRASH-CONSISTENT COMMIT WATERMARK
// ---------------------------------
// Header field `committed` is advanced with a release-store AFTER the
// frame bytes are in place. A reader (replay tool, or a live tail-reader
// in another process) acquiring `committed` therefore always sees a valid
// length-framed prefix -- never a torn final frame.
//
// ZERO HEAP: open() maps the file; append() allocates nothing, ever.
// ============================================================================

#include "common.hpp"
#include <atomic>
#include <cstring>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <sys/mman.h>
#  include <sys/stat.h>
#  include <unistd.h>
#endif

namespace lob {

// ---------------------------------------------------------------------------
// On-disk layout: one 4KB header page, then raw wire bytes.
// ---------------------------------------------------------------------------
inline constexpr std::uint64_t JOURNAL_MAGIC = 0x314C4E524A424F4Cull; // "LOBJRNL1"
inline constexpr std::size_t   JOURNAL_HDR   = 4096;

struct JournalHeader {
    std::uint64_t magic;        // format identifier + version
    std::uint64_t capacity;     // data bytes usable after the header page
    std::uint64_t committed;    // valid data bytes (release-stored watermark)
    std::uint64_t dropped;      // bytes discarded because the file filled
    // rest of the 4KB page is reserved (session metadata, tsc calibration..)
};
static_assert(sizeof(JournalHeader) <= JOURNAL_HDR);

// ---------------------------------------------------------------------------
// Journal -- single-writer, append-only, memory-mapped. Owned by the feed
// thread; no other thread writes it (lock-free by having nothing to lock).
// ---------------------------------------------------------------------------
class Journal {
public:
    Journal() = default;
    Journal(const Journal&)            = delete;
    Journal& operator=(const Journal&) = delete;
    ~Journal() { close(); }

    // COLD PATH. Creates/overwrites `path`, pre-sizes it to hold
    // `data_capacity` journal bytes, maps it read-write, pre-touches every
    // page (so the hot path never soft-faults), and writes the header.
    bool open(const char* path, std::size_t data_capacity) {
        const std::size_t total = JOURNAL_HDR + data_capacity;
#if defined(_WIN32)
        file_ = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;
        map_ = CreateFileMappingA(file_, nullptr, PAGE_READWRITE,
                                  DWORD(std::uint64_t(total) >> 32),
                                  DWORD(total & 0xFFFFFFFFull), nullptr);
        if (!map_) { close(); return false; }
        base_ = static_cast<std::byte*>(
            MapViewOfFile(map_, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!base_) { close(); return false; }
#else
        fd_ = ::open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
        if (fd_ < 0) return false;
        if (::ftruncate(fd_, off_t(total)) != 0) { close(); return false; }
        void* v = ::mmap(nullptr, total, PROT_READ | PROT_WRITE,
                         MAP_SHARED, fd_, 0);
        if (v == MAP_FAILED) { close(); return false; }
        base_ = static_cast<std::byte*>(v);
#endif
        total_ = total;
        cap_   = data_capacity;
        data_  = base_ + JOURNAL_HDR;
        off_   = 0;

        // Pre-touch every page NOW so append() never soft-faults mid-session.
        for (std::size_t i = 0; i < total; i += 4096) base_[i] = std::byte{0};

        hdr_ = reinterpret_cast<JournalHeader*>(base_);
        hdr_->magic     = JOURNAL_MAGIC;
        hdr_->capacity  = cap_;
        hdr_->committed = 0;
        hdr_->dropped   = 0;
        return true;
    }

    // ========================================================================
    // HOT PATH (feed thread) -- append one wire frame, byte-identical.
    // Cost: bounds check (never-taken branch) + short memcpy + two plain
    // stores (the release-store compiles to a plain MOV on x86/TSO).
    // ========================================================================
    LOB_FORCE_INLINE void append(const std::byte* p, std::size_t n) noexcept {
        if (LOB_UNLIKELY(off_ + n > cap_)) {       // full: drop, never block
            hdr_->dropped += n;
            return;
        }
        std::memcpy(data_ + off_, p, n);
        off_ += n;
        // Publish the new watermark AFTER the bytes: any acquire-reader
        // sees a complete, untorn, length-framed prefix.
        std::atomic_ref<std::uint64_t>(hdr_->committed)
            .store(off_, std::memory_order_release);
    }

    std::uint64_t committed() const noexcept { return off_; }
    std::uint64_t dropped()   const noexcept { return hdr_ ? hdr_->dropped : 0; }

    // COLD PATH. Synchronous flush of the dirty mapping to the device --
    // end-of-session / checkpoint use only, NEVER during trading.
    void flush() noexcept {
        if (!base_) return;
#if defined(_WIN32)
        FlushViewOfFile(base_, 0);
        FlushFileBuffers(file_);
#else
        ::msync(base_, total_, MS_SYNC);
#endif
    }

    void close() noexcept {
#if defined(_WIN32)
        if (base_) { UnmapViewOfFile(base_); base_ = nullptr; }
        if (map_)  { CloseHandle(map_);      map_  = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
#else
        if (base_) { ::munmap(base_, total_); base_ = nullptr; }
        if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
#endif
        hdr_ = nullptr; data_ = nullptr;
    }

private:
    std::byte*     base_ = nullptr;   // header page
    std::byte*     data_ = nullptr;   // journal bytes (base_ + 4KB)
    JournalHeader* hdr_  = nullptr;
    std::size_t    total_ = 0, cap_ = 0, off_ = 0;
#if defined(_WIN32)
    HANDLE file_ = INVALID_HANDLE_VALUE;
    HANDLE map_  = nullptr;
#else
    int fd_ = -1;
#endif
};

// ---------------------------------------------------------------------------
// JournalReplay -- read-only view of a journal's committed bytes. Feed
// {data(), size()} straight into ItchParser to reconstruct engine state.
// Entirely cold path.
// ---------------------------------------------------------------------------
class JournalReplay {
public:
    ~JournalReplay() { close(); }

    bool open(const char* path) {
#if defined(_WIN32)
        file_ = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;
        map_ = CreateFileMappingA(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!map_) { close(); return false; }
        base_ = static_cast<const std::byte*>(
            MapViewOfFile(map_, FILE_MAP_READ, 0, 0, 0));
        if (!base_) { close(); return false; }
#else
        fd_ = ::open(path, O_RDONLY);
        if (fd_ < 0) return false;
        struct stat st{};
        if (fstat(fd_, &st) != 0) { close(); return false; }
        total_ = std::size_t(st.st_size);
        void* v = ::mmap(nullptr, total_, PROT_READ, MAP_PRIVATE, fd_, 0);
        if (v == MAP_FAILED) { close(); return false; }
        base_ = static_cast<const std::byte*>(v);
#endif
        const auto* h = reinterpret_cast<const JournalHeader*>(base_);
        if (h->magic != JOURNAL_MAGIC) { close(); return false; }
        // Acquire pairs with the writer's release: the committed prefix is
        // fully visible (matters when tailing a LIVE journal cross-process).
        len_ = std::atomic_ref<const std::uint64_t>(h->committed)
                   .load(std::memory_order_acquire);
        return true;
    }

    const std::byte* data() const noexcept { return base_ + JOURNAL_HDR; }
    std::size_t      size() const noexcept { return std::size_t(len_); }

    void close() noexcept {
#if defined(_WIN32)
        if (base_) { UnmapViewOfFile(base_); base_ = nullptr; }
        if (map_)  { CloseHandle(map_);      map_  = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
#else
        if (base_) { ::munmap(const_cast<std::byte*>(base_), total_); base_ = nullptr; }
        if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
#endif
    }

private:
    const std::byte* base_ = nullptr;
    std::uint64_t    len_  = 0;
#if defined(_WIN32)
    HANDLE file_ = INVALID_HANDLE_VALUE;
    HANDLE map_  = nullptr;
#else
    int         fd_    = -1;
    std::size_t total_ = 0;
#endif
};

} // namespace lob
