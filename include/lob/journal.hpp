#pragma once
// Single-writer append-only mapped byte journal. Callers append complete frames.
// Release publication orders CPU visibility only: it is NOT a durable commit.
// flush() is an explicit checked OS flush; this format has no checksums, atomic
// disk commit protocol, or recovery from torn writes/power failure. Replay is
// supported after the writer is closed, on the same host byte-order/ABI.
// Pre-touching reduces first-touch faults, but cannot promise pages stay resident.

#include "common.hpp"
#include <atomic>
#include <cstring>
#include <limits>
#include <new>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <sys/mman.h>
#  include <sys/stat.h>
#  include <unistd.h>
#endif

namespace lob {
inline constexpr std::uint64_t JOURNAL_MAGIC = 0x314C4E524A424F4Cull;
inline constexpr std::size_t JOURNAL_HDR = 4096;
struct JournalHeader {
    std::uint64_t magic;
    std::uint64_t capacity;
    std::uint64_t committed;
    std::uint64_t dropped;
};
static_assert(sizeof(JournalHeader) <= JOURNAL_HDR);

class Journal {
public:
    Journal() = default;
    Journal(const Journal&) = delete;
    Journal& operator=(const Journal&) = delete;
    ~Journal() { close(); }

    // Truncates the destination; open/reopen and flush belong on the cold path.
    bool open(const char* path, std::size_t data_capacity) {
        close();
        if (!path || data_capacity > (std::numeric_limits<std::size_t>::max)() - JOURNAL_HDR)
            return false;
        const auto total = JOURNAL_HDR + data_capacity;
        if (total > std::uint64_t((std::numeric_limits<std::int64_t>::max)())) return false;
#if defined(_WIN32)
        file_ = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;
        map_ = CreateFileMappingA(file_, nullptr, PAGE_READWRITE,
                                  DWORD(std::uint64_t(total) >> 32), DWORD(total), nullptr);
        if (!map_) { close(); return false; }
        base_ = static_cast<std::byte*>(MapViewOfFile(map_, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!base_) { close(); return false; }
#else
        if (total > std::uint64_t((std::numeric_limits<off_t>::max)())) return false;
        fd_ = ::open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
        if (fd_ < 0) return false;
        if (::ftruncate(fd_, off_t(total)) != 0) { close(); return false; }
        void* mapping = ::mmap(nullptr, total, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, 0);
        if (mapping == MAP_FAILED) { close(); return false; }
        base_ = static_cast<std::byte*>(mapping);
#endif
        total_ = total;
        cap_ = data_capacity;
        data_ = base_ + JOURNAL_HDR;
        for (std::size_t i = 0; i < total; i += 4096) base_[i] = std::byte{0};
        hdr_ = ::new (static_cast<void*>(base_)) JournalHeader{JOURNAL_MAGIC, cap_, 0, 0};
        return true;
    }

    // On exhaustion latch the error: accepting a later smaller frame would
    // silently turn a contiguous prefix into a stream with missing events.
    bool append(const std::byte* p, std::size_t n) noexcept {
        if (!hdr_) return false;
        if ((n != 0 && !p) || failed_ || n > cap_ - off_) {
            failed_ = true;
            const auto max = (std::numeric_limits<std::uint64_t>::max)();
            hdr_->dropped = n > max - hdr_->dropped ? max : hdr_->dropped + n;
            return false;
        }
        if (n) std::memcpy(data_ + off_, p, n);
        off_ += n;
        std::atomic_ref<std::uint64_t>(hdr_->committed).store(off_, std::memory_order_release);
        return true;
    }
    std::uint64_t committed() const noexcept { return off_; }
    std::uint64_t dropped() const noexcept { return hdr_ ? hdr_->dropped : 0; }
    bool failed() const noexcept { return failed_; }

    bool flush() noexcept {
        if (!base_) return false;
#if defined(_WIN32)
        const bool view_ok = FlushViewOfFile(base_, 0) != 0;
        const bool file_ok = FlushFileBuffers(file_) != 0;
        return view_ok && file_ok;
#else
        const bool view_ok = ::msync(base_, total_, MS_SYNC) == 0;
        const bool file_ok = ::fsync(fd_) == 0;
        return view_ok && file_ok;
#endif
    }
    void close() noexcept {
#if defined(_WIN32)
        if (base_) UnmapViewOfFile(base_);
        if (map_) { CloseHandle(map_); map_ = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
#else
        if (base_) ::munmap(base_, total_);
        if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
#endif
        base_ = nullptr; data_ = nullptr; hdr_ = nullptr;
        total_ = cap_ = off_ = 0;
        failed_ = false;
    }
private:
    std::byte* base_ = nullptr;
    std::byte* data_ = nullptr;
    JournalHeader* hdr_ = nullptr;
    std::size_t total_ = 0, cap_ = 0, off_ = 0;
    bool failed_ = false;
#if defined(_WIN32)
    HANDLE file_ = INVALID_HANDLE_VALUE;
    HANDLE map_ = nullptr;
#else
    int fd_ = -1;
#endif
};

// Read-only offline snapshot. Validate physical file bounds before any header
// dereference, and validate header bounds before exposing committed bytes.
class JournalReplay {
public:
    JournalReplay() = default;
    JournalReplay(const JournalReplay&) = delete;
    JournalReplay& operator=(const JournalReplay&) = delete;
    ~JournalReplay() { close(); }

    bool open(const char* path) {
        close();
        if (!path) return false;
#if defined(_WIN32)
        file_ = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;
        LARGE_INTEGER size;
        if (!GetFileSizeEx(file_, &size) || size.QuadPart < std::int64_t(JOURNAL_HDR) ||
            std::uint64_t(size.QuadPart) > (std::numeric_limits<std::size_t>::max)()) {
            close(); return false;
        }
        total_ = std::size_t(size.QuadPart);
        map_ = CreateFileMappingA(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (!map_) { close(); return false; }
        base_ = static_cast<const std::byte*>(MapViewOfFile(map_, FILE_MAP_READ, 0, 0, 0));
        if (!base_) { close(); return false; }
#else
        fd_ = ::open(path, O_RDONLY);
        if (fd_ < 0) return false;
        struct stat st{};
        if (::fstat(fd_, &st) != 0 || st.st_size < off_t(JOURNAL_HDR) ||
            std::uint64_t(st.st_size) > (std::numeric_limits<std::size_t>::max)()) {
            close(); return false;
        }
        total_ = std::size_t(st.st_size);
        void* mapping = ::mmap(nullptr, total_, PROT_READ, MAP_PRIVATE, fd_, 0);
        if (mapping == MAP_FAILED) { close(); return false; }
        base_ = static_cast<const std::byte*>(mapping);
#endif
        JournalHeader header;
        std::memcpy(&header, base_, sizeof(header));
        if (header.magic != JOURNAL_MAGIC || header.capacity > total_ - JOURNAL_HDR ||
            header.committed > header.capacity) { close(); return false; }
        len_ = std::size_t(header.committed);
        dropped_ = header.dropped;
        return true;
    }
    const std::byte* data() const noexcept { return base_ ? base_ + JOURNAL_HDR : nullptr; }
    std::size_t size() const noexcept { return len_; }
    std::uint64_t dropped() const noexcept { return dropped_; }

    void close() noexcept {
#if defined(_WIN32)
        if (base_) UnmapViewOfFile(base_);
        if (map_) { CloseHandle(map_); map_ = nullptr; }
        if (file_ != INVALID_HANDLE_VALUE) { CloseHandle(file_); file_ = INVALID_HANDLE_VALUE; }
#else
        if (base_) ::munmap(const_cast<std::byte*>(base_), total_);
        if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
#endif
        base_ = nullptr;
        total_ = len_ = 0;
        dropped_ = 0;
    }
private:
    const std::byte* base_ = nullptr;
    std::size_t total_ = 0, len_ = 0;
    std::uint64_t dropped_ = 0;
#if defined(_WIN32)
    HANDLE file_ = INVALID_HANDLE_VALUE;
    HANDLE map_ = nullptr;
#else
    int fd_ = -1;
#endif
};
} // namespace lob
