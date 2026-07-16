#pragma once
// ============================================================================
// itch.hpp -- NASDAQ ITCH 5.0 binary feed: wire structs + zero-copy parser.
//
// WIRE FORMAT
// -----------
// We consume the standard NASDAQ "BinaryFILE" / SoupBinTCP-style framing:
// each message is prefixed by a 2-byte BIG-ENDIAN length, followed by the
// raw ITCH payload. Supported message types (all lengths per the official
// ITCH 5.0 spec):
//
//     'A'  Add Order (no MPID)        36 bytes
//     'E'  Order Executed             31 bytes
//     'X'  Order Cancel (partial)     23 bytes
//     'D'  Order Delete (full)        19 bytes
//
// ZERO-COPY DESIGN
// ----------------
// * The parser NEVER copies payload bytes, never allocates, and never
//   materialises intermediary objects. It hands the caller a `const&` to a
//   #pragma pack(1) struct reinterpret_cast'ed DIRECTLY over the network
//   buffer. The only data movement is the caller loading the 3-4 fields it
//   actually needs into registers.
//
// * #pragma pack(push, 1): ITCH fields are unaligned on the wire (e.g. the
//   64-bit order reference starts at byte offset 11). pack(1) makes the C++
//   struct layout byte-identical to the wire layout so offsets need no
//   arithmetic. x86 handles the resulting unaligned register loads natively
//   (a MOV on a split cache line costs ~1 extra cycle -- irrelevant).
//
// * ENDIANNESS: ITCH is big-endian; x86 is little-endian. Every integer
//   field goes through a single-instruction byte swap (BSWAP/MOVBE via
//   __builtin_bswapNN / _byteswap_*). No shifting-and-masking loops.
//
// * Strict-aliasing note: casting a byte buffer to a struct and reading it
//   is the universal idiom for wire decoding; GCC/Clang/MSVC all compile it
//   to the intended raw loads for char-backed buffers. The structs are
//   trivially-copyable PODs, which is what makes this well-behaved.
// ============================================================================

#include "common.hpp"

#if defined(_MSC_VER)
#  include <cstdlib>   // _byteswap_ushort / _ulong / _uint64
#endif

namespace lob::itch {

// ---------------------------------------------------------------------------
// Byte-order conversion -- one hardware instruction each.
// ---------------------------------------------------------------------------
LOB_FORCE_INLINE std::uint16_t bswap(std::uint16_t v) noexcept {
#if defined(_MSC_VER)
    return _byteswap_ushort(v);
#else
    return __builtin_bswap16(v);
#endif
}
LOB_FORCE_INLINE std::uint32_t bswap(std::uint32_t v) noexcept {
#if defined(_MSC_VER)
    return _byteswap_ulong(v);
#else
    return __builtin_bswap32(v);
#endif
}
LOB_FORCE_INLINE std::uint64_t bswap(std::uint64_t v) noexcept {
#if defined(_MSC_VER)
    return _byteswap_uint64(v);
#else
    return __builtin_bswap64(v);
#endif
}

// Pointer-based big-endian load (used for the 2-byte framing prefix).
LOB_FORCE_INLINE std::uint16_t load_be16(const std::byte* p) noexcept {
    return bswap(*reinterpret_cast<const std::uint16_t*>(p));
}

// ---------------------------------------------------------------------------
// Wire structs: byte-exact ITCH 5.0 layouts. All multi-byte integers are
// BIG-ENDIAN ON THE WIRE -- always read them through bswap().
// ---------------------------------------------------------------------------
#pragma pack(push, 1)

struct MsgHeader {                    // common 11-byte ITCH prefix
    char          type;               // 'A' / 'E' / 'X' / 'D'
    std::uint16_t stock_locate;       // big-endian
    std::uint16_t tracking_number;    // big-endian
    std::uint8_t  timestamp[6];       // 48-bit ns since midnight, big-endian
};
static_assert(sizeof(MsgHeader) == 11);

struct AddOrder {                     // 'A' -- 36 bytes
    MsgHeader     h;
    std::uint64_t order_ref;          // big-endian
    char          side;               // 'B' or 'S'
    std::uint32_t shares;             // big-endian
    char          stock[8];           // space-padded symbol (NOT a string!)
    std::uint32_t price;              // big-endian, 4 implied decimals --
                                      // identical to our PRICE_SCALE=10^4,
                                      // so NO arithmetic conversion needed.
};
static_assert(sizeof(AddOrder) == 36);

struct OrderExecuted {                // 'E' -- 31 bytes
    MsgHeader     h;
    std::uint64_t order_ref;          // big-endian
    std::uint32_t executed_shares;    // big-endian
    std::uint64_t match_number;       // big-endian
};
static_assert(sizeof(OrderExecuted) == 31);

struct OrderCancel {                  // 'X' -- 23 bytes (partial cancel)
    MsgHeader     h;
    std::uint64_t order_ref;          // big-endian
    std::uint32_t canceled_shares;    // big-endian
};
static_assert(sizeof(OrderCancel) == 23);

struct OrderDelete {                  // 'D' -- 19 bytes (full removal)
    MsgHeader     h;
    std::uint64_t order_ref;          // big-endian
};
static_assert(sizeof(OrderDelete) == 19);

#pragma pack(pop)

// 48-bit big-endian timestamp -> uint64 (6 single-byte loads, no branches).
LOB_FORCE_INLINE std::uint64_t timestamp_ns(const MsgHeader& h) noexcept {
    return (std::uint64_t(h.timestamp[0]) << 40) |
           (std::uint64_t(h.timestamp[1]) << 32) |
           (std::uint64_t(h.timestamp[2]) << 24) |
           (std::uint64_t(h.timestamp[3]) << 16) |
           (std::uint64_t(h.timestamp[4]) <<  8) |
            std::uint64_t(h.timestamp[5]);
}

// ---------------------------------------------------------------------------
// ItchParser -- a zero-copy cursor over a length-framed ITCH byte stream
// (e.g. an mmap'ed capture file or a kernel-bypass NIC ring).
//
// Usage (hot path):
//     ItchParser p(data, len);
//     while (p.next(sink)) {}
//
// `sink` is any object with operator() overloads for the four wire structs;
// it is a template parameter, so dispatch is fully inlined -- no virtual
// calls, no std::function, no allocation. The struct references passed to
// the sink point INTO the source buffer and are valid only during the call
// (the sink must copy out the few fields it needs -- into registers).
// ---------------------------------------------------------------------------
class ItchParser {
public:
    ItchParser(const std::byte* data, std::size_t len) noexcept
        : cur_(data), end_(data + len) {}

    // Decode one message and feed it to the sink. Returns false at end of
    // stream (or on a malformed/unknown frame, which halts the session --
    // in production a desync on a TCP-style stream is unrecoverable anyway).
    template <typename Sink>
    LOB_FORCE_INLINE bool next(Sink&& sink) noexcept {
        // Framing: 2-byte big-endian payload length.
        if (LOB_UNLIKELY(cur_ + 2 > end_)) return false;
        const std::uint16_t len = load_be16(cur_);
        const std::byte* p = cur_ + 2;
        if (LOB_UNLIKELY(p + len > end_)) return false;

        // ZERO-COPY: overlay the wire struct straight onto the buffer.
        // The switch compiles to a jump table; each arm is a single
        // reinterpret_cast (free) + the sink body (inlined).
        switch (static_cast<char>(*p)) {
        case 'A':
            if (LOB_UNLIKELY(len != sizeof(AddOrder))) return false;
            sink(*reinterpret_cast<const AddOrder*>(p));
            break;
        case 'E':
            if (LOB_UNLIKELY(len != sizeof(OrderExecuted))) return false;
            sink(*reinterpret_cast<const OrderExecuted*>(p));
            break;
        case 'X':
            if (LOB_UNLIKELY(len != sizeof(OrderCancel))) return false;
            sink(*reinterpret_cast<const OrderCancel*>(p));
            break;
        case 'D':
            if (LOB_UNLIKELY(len != sizeof(OrderDelete))) return false;
            sink(*reinterpret_cast<const OrderDelete*>(p));
            break;
        default:
            return false;             // unknown type: stop (stream desync)
        }
        cur_ = p + len;
        return true;
    }

    LOB_FORCE_INLINE bool exhausted() const noexcept { return cur_ >= end_; }
    LOB_FORCE_INLINE const std::byte* cursor() const noexcept { return cur_; }

private:
    const std::byte* cur_;
    const std::byte* end_;
};

} // namespace lob::itch
