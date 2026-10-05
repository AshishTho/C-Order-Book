# Contracts and design choices

## Matching versus historical reconstruction

The matching engine accepts new orders that may trade against resting orders.
Historical reconstruction instead applies exchange events to reproduce the
observed state. `replay_events` uses post-only adds to **reject** crossed or
locked historical adds rather than create unobserved fills. Executions and
cancellations reduce the referenced order's remaining quantity. The tool is
single instrument, session-local, starts from an empty book, and rejects gaps.
It does not decode a full raw exchange session or implement snapshot recovery.

The normalized CSV header is exactly:

```csv
seq,type,order_ref,side,price,qty
1,A,1,B,100,20
2,A,2,S,102,30
3,E,1,-,0,5
4,X,2,-,0,7
5,D,1,-,0,0
```

`seq` is a positive contiguous unsigned 64-bit application sequence (the first
value need not be 1). `order_ref` is a nonzero unsigned 64-bit reference, unique
among live orders. `A` requires side `B`/`S`, integer tick price in the configured
ladder, and positive signed-64-bit lots. `E`/`X` require `-`, price 0, and positive
lots no larger than the live remainder. `D` requires `-`, price 0, quantity 0.
An exchange adapter must assign this sequence only after validating source
transport sequencing; renumbering a gapped capture cannot recover its state.

Input is deliberately plain unquoted CSV: exact header, six fields, no blank
rows, decimal integers without extra whitespace. CRLF and LF are accepted.
Malformed rows, duplicate live refs, dead refs, invalid reductions, ladder
overflow, and exhausted order capacity terminate the run with a line number.
Output is published only after successful completion. Existing output files
are refused; a failed run removes its own `.partial` file.

The output header is `seq,bid_price,ask_price,bid_qty,ask_qty`. Each event that
leaves a valid two-sided book emits one row, including unchanged BBOs. One-sided
states are omitted. Therefore research horizons count retained observations,
not source event sequence differences or nanoseconds. This filtering can bias
research; evaluate invalid/one-sided periods separately for real datasets.
Prices remain ticks and quantities remain lots, with **no implicit dollar conversion**.

## Engine semantics and memory

The ladder provides direct indexed access and bounded bitmap navigation,
trading memory and a fixed price interval for lookup simplicity. Intrusive
FIFO queues preserve price/time priority; matching work is proportional to
fills, not constant time. The arena and arrays allocate at construction;
the engine's ordinary mutation operations do not allocate. The offline CSV
tool and Python lab intentionally allocate and perform I/O.

IDs combine a 32-bit slot-plus-one and generation. Odd generations denote
live orders; zero/even generations are dead. Generation wrap remains possible
after 2^31 lifecycles of one slot: identifiers are not persistent global IDs.
The all-ones return ID denotes a fully filled taker. Return zero means no
remainder was accepted, which can follow earlier fills if resting fails.
Trade callbacks must not throw or re-enter the book. Gateway users must track
fills and reconcile any unaccepted remainder; a return ID is not a complete
exchange execution report. Taker IDs in current trade callbacks are zero.

Post-only orders that would cross are rejected before matching. Icebergs expose
one clip; replenished reserve rejoins the back of the price-level FIFO.
`reduce()` cancels hidden quantity first; `execute()` consumes visible quantity
and replenishes reserve. These are simulation policies, not assertions that
all venues use identical priority rules. Aggregate visible-plus-reserve depth
is bounded to prevent later replenishment from overflowing a price level.

## Feed, signals, and backpressure

The parser supports only A/E/X/D payloads with two-byte big-endian lengths.
This is not full SoupBinTCP or MoldUDP64 transport. Unsupported message types
produce explicit failure. Bounded `memcpy` into typed locals avoids unaligned
access and object-lifetime violations; compilers may optimize these fixed-size
copies. Arena routing avoids copying whole queued payloads; decoding is not
advertised as a literal zero-copy struct overlay.

OFI handles transitions into and out of empty sides explicitly. Cumulative
OFI uses saturating arithmetic; downstream users must treat saturation as loss
of signal information. The size-weighted midpoint returns -1 for a missing
side or an unrepresentable Q56.8 result. It retains integer precision, but
truncation at 1/256 tick remains part of the representation.

`BboPublisher` and `SignalPublisher` keep the most recent failed publication.
Call `flush_pending()` during idle/drain periods if no new market event arrives.
Sequence gaps indicate omitted updates; cumulative OFI retains those changes
until numerical saturation. The consumer cannot reconstruct every omitted BBO.
Each ring requires exactly one producer and one consumer.

## Risk and persistence boundaries

Risk tracks accepted open exposure and realized position. An admitted order
reserves exposure before entering the engine. Only an acknowledged cancel or
engine rejection releases it; fills transfer it to position. The caller must
correlate/deduplicate reports per order. Aggregate counters reject impossible
reports but cannot detect every duplicate while other open orders remain.
Clock regression fails closed; timestamp zero is valid. A zero rate window
disables throttling. This is not a complete firm-level or regulatory control.

The journal exposes a CPU-visible committed prefix and checks explicit OS flush
results. It validates capacity/header boundaries and latches an append failure
so later frames cannot hide a hole. Replay is supported after closing the writer
on the same host ABI/endianness. It has no checksums, durable transaction protocol,
cross-process live-reader guarantee, or power-loss recovery promise.

## Measurement

Keep correctness and sanitizers separate from timing. Compare identical build
flags, hardware, workload seed, and system load; retain repeated distributions
and outliers. `scripts/benchmark.py` records raw stdout/stderr, binary and source
hashes, OS/processor strings, and the CMake cache. Requested affinity can fail,
and successful pinning does not prevent preemption. TSC availability/calibration
and cross-core synchronization depend on the machine or hypervisor. Do not
translate a desktop microbenchmark into an exchange latency promise.
