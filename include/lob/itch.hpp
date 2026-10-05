#pragma once
// NASDAQ ITCH 5.0 A/E/X/D subset with 2-byte big-endian BinaryFILE framing.
// SoupBinTCP packet headers are NOT supported. Decoding is allocation-free.
// Payloads are copied into typed local objects to respect C++ object lifetime
// and alignment rules; sink references are valid only for that invocation.

#include "common.hpp"
#include <cstring>

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
    return (std::uint16_t(p[0]) << 8) | std::uint16_t(p[1]);
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

enum class ParseStatus {
    Ready, End, NullInput, TruncatedLength, EmptyFrame, TruncatedPayload,
    InvalidLength, UnsupportedType
};

inline const char* status_name(ParseStatus status) noexcept {
    switch (status) {
    case ParseStatus::Ready: return "ready";
    case ParseStatus::End: return "end of stream";
    case ParseStatus::NullInput: return "null input with nonzero length";
    case ParseStatus::TruncatedLength: return "truncated frame length";
    case ParseStatus::EmptyFrame: return "zero-length frame";
    case ParseStatus::TruncatedPayload: return "truncated frame payload";
    case ParseStatus::InvalidLength: return "invalid payload length for message type";
    case ParseStatus::UnsupportedType: return "unsupported ITCH message type";
    }
    return "unknown parser status";
}

// A strict subset parser: unknown message types fail explicitly. A production
// historical feed needs additional message types and transport/session logic.
// Framing validation belongs here; semantic validation belongs in the sink.
class ItchParser {
public:
    ItchParser(const std::byte* data, std::size_t len) noexcept
        : data_(data), len_(len), status_(data || len == 0
              ? ParseStatus::Ready : ParseStatus::NullInput) {}

    template <typename Sink>
    LOB_FORCE_INLINE bool next(Sink&& sink) noexcept {
        if (status_ != ParseStatus::Ready) return false;
        const std::size_t remaining = len_ - offset_;
        if (remaining == 0) return fail(ParseStatus::End);
        if (remaining < 2) return fail(ParseStatus::TruncatedLength);
        const auto len = load_be16(data_ + offset_);
        if (len == 0) return fail(ParseStatus::EmptyFrame);
        if (len > remaining - 2) return fail(ParseStatus::TruncatedPayload);
        const std::byte* p = data_ + offset_ + 2;
        message_type_ = static_cast<char>(*p);
        bool decoded = false;
        switch (message_type_) {
        case 'A': decoded = dispatch<AddOrder>(p, len, sink); break;
        case 'E': decoded = dispatch<OrderExecuted>(p, len, sink); break;
        case 'X': decoded = dispatch<OrderCancel>(p, len, sink); break;
        case 'D': decoded = dispatch<OrderDelete>(p, len, sink); break;
        default: return fail(ParseStatus::UnsupportedType);
        }
        if (!decoded) return fail(ParseStatus::InvalidLength);
        offset_ += std::size_t(len) + 2;
        return true;
    }

    bool exhausted() const noexcept { return offset_ == len_ && data_valid(); }
    const std::byte* cursor() const noexcept { return data_ ? data_ + offset_ : nullptr; }
    std::size_t offset() const noexcept { return offset_; }
    ParseStatus status() const noexcept { return status_; }
    char message_type() const noexcept { return message_type_; }

private:
    template <typename Message, typename Sink>
    static LOB_FORCE_INLINE bool dispatch(const std::byte* p, std::uint16_t len,
                                         Sink& sink) noexcept {
        if (len != sizeof(Message)) return false;
        Message message;
        std::memcpy(&message, p, sizeof(message));
        sink(message);
        return true;
    }
    bool fail(ParseStatus status) noexcept { status_ = status; return false; }
    bool data_valid() const noexcept { return data_ != nullptr || len_ == 0; }
    const std::byte* data_;
    std::size_t len_;
    std::size_t offset_ = 0;
    ParseStatus status_;
    char message_type_ = '\0';
};

} // namespace lob::itch