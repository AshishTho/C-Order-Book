# Ultra-Low Latency Limit Order Book & Closed-Loop HFT Ecosystem (C++20)

A production-grade, price-time-priority matching engine embedded in a complete
wire-to-wire trading loop: zero-copy NASDAQ ITCH 5.0 ingestion, lock-free
inter-core routing, pre-trade risk, deterministic replay journaling, sparse
order-id translation, and on-engine microstructure signal generation
(OFI / micro-price). Built over seven phases; every phase ships with an
RDTSC-instrumented benchmark that proves its latency claim on real hardware.

---

## 1. Design Philosophy

Three rules govern every line on the hot path:

- **Zero heap allocation.** No `new`/`delete`/`malloc`/`free` and no
  allocating STL containers after initialisation. All storage — the order
  arena, the ring buffers, the hash map, the journal mapping — is provisioned
  once, up front, pre-touched, and reused for the process lifetime.
  Allocation is not "slow"; it is *non-deterministic*, and determinism is the
  actual product.
- **Lock-free, single-writer concurrency.** Every shared structure is an SPSC
  ring with exactly one producing and one consuming thread. Synchronisation
  is acquire/release atomics and explicit `std::atomic_thread_fence` pairs —
  which compile to plain `MOV`s on x86/TSO. No mutexes, no syscalls, no
  priority inversion, and no cache-line ping-pong: each side runs out of its
  own cache line and touches its peer's only when the ring looks full/empty
  (the cached-peer-index Disruptor trick).
- **Cache-line discipline.** `alignas(64)` on every cross-thread structure;
  producer and consumer indices on separate lines; the 64-byte `Order` with
  intrusive links embedded; arena slots that never straddle a line; wire
  messages (`SignalUpdate`) packed to exactly one line. A false-sharing bug
  turns a 1 ns L1 hit into a 100 ns coherence stall — the layout *is* the
  optimisation.

Fixed-point `int64` arithmetic everywhere (prices in ticks, 4 implied
decimals, identical to ITCH's own scaling). Floating point is banned on the
hot path: variable latency, non-associativity, and unsafe comparisons have no
place in a matching engine.

---

## 2. System Architecture

```
 FEED THREAD (core 0)                          ENGINE THREAD (core 2)
┌─────────────────────────┐                   ┌──────────────────────────────────┐
│ Mock network layer      │                   │ ItchParser (zero-copy overlay)   │
│ (mmap'ed ITCH capture;  │  Zero-Copy Arena  │   └─> FlatHashMap ref -> id      │
│  stands in for NIC DMA) │ ═════════════════▶│ OrderBook (single writer)        │
│                         │  64B-aligned      │   ├─ flat PriceLevel ladder      │
│   └─> Journal::append() │  slots, ptr pass, │   ├─ 3-level occupancy bitmap    │
│       (mmap append-only │  release/acquire  │   ├─ intrusive order FIFOs       │
│        raw wire bytes)  │  fences           │   └─ MemoryPool<Order> arena     │
└─────────────────────────┘                   │ SignalPublisher (BBO diff +      │
                                              │   OFI + micro-price, Q56.8)      │
                                              └───────────────┬──────────────────┘
                                                              │ SPSC signal ring
                                                              │ (1-line updates,
                                                              │  drop-on-full
                                                              ▼  conflation)
 ENGINE THREAD (inbound order ring)           STRATEGY THREAD (core 4)
┌─────────────────────────┐   SPSC order      ┌──────────────────────────────────┐
│ Matching engine applies │◀════ ring ════════│ Strategy brain (cancel/replace   │
│ strategy quotes         │                   │  quoting on BBO + signals)       │
└─────────────────────────┘                   │   └─ RiskGateway::check() gates  │
                                              │       EVERY order (15c3-5)       │
                                              └──────────────────────────────────┘
```

Data is **stationary**: a packet lands once in its cache-line-fenced arena
slot and the engine decodes and matches directly out of that memory. The
queues move pointers and sequence numbers, never payloads. The journal tap
runs on the feed thread while the bytes are still L1-hot; the matching thread
does not change by one instruction when journaling is enabled.

---

## 3. Component Map

| File | Component | Load-bearing decision |
|------|-----------|-----------------------|
| `include/lob/common.hpp` | Types, constants | Fixed-point `int64` prices; branch hints; 64B line constant |
| `include/lob/order.hpp` | `Order` | Exactly one cache line, intrusive `next`/`prev` embedded |
| `include/lob/memory_pool.hpp` | Arena | One up-front allocation, pre-touched, LIFO intrusive free list |
| `include/lob/level_bitmap.hpp` | Occupancy index | 3-level bitmap + `TZCNT`: next non-empty level in O(1) — bounds the tail |
| `include/lob/order_book.hpp` | Matching engine | Flat ladder (no tree walk), O(1) intrusive cancel, generation-checked ids |
| `include/lob/spsc_ring.hpp` | SPSC queue | Split index lines, cached peer index, acquire/release only |
| `include/lob/arena_ring.hpp` | Zero-copy arena | Pointer-passing slot ring; explicit `atomic_thread_fence` publication |
| `include/lob/itch.hpp` | ITCH 5.0 parser | `#pragma pack(1)` wire-overlay structs, single-instruction `BSWAP` fields |
| `include/lob/risk.hpp` | Pre-trade risk | Branch-free bitmask verdict; O(1) exact sliding-window rate limit |
| `include/lob/journal.hpp` | Replay journal | mmap append-only raw wire bytes; release-stored commit watermark |
| `include/lob/flat_hash.hpp` | Sparse id map | Open addressing, Fibonacci hash, backward-shift deletion, 50% load cap |
| `include/lob/bbo.hpp` / `signals.hpp` | Market data out | Diff-and-publish; OFI + micro-price in branchless fixed point |
| `bench/*.cpp` | Proof suite | One RDTSC benchmark per phase; percentiles, verified business totals |
| `tests/test_correctness.cpp` | Functional tests | Matching, priority, cancels, generation safety, cross-thread ring |

---

## 4. Latency & Hardware Optimization

Measured on Windows x64, GCC 16.1 `-O3 -march=native`, invariant TSC @ 1.498
GHz, pinned threads. Timing uses serialised RDTSC pairs
(`LFENCE;RDTSC` … `RDTSCP;LFENCE`) with harness overhead measured and
subtracted; batched averages are used where a single op is smaller than the
clock read itself.

| Path | Cost | Benchmark |
|------|------|-----------|
| Engine op (add/execute/cancel), P50 | **15.4 ns** (P99 80.8 ns) | `bench_rdtsc` |
| Engine streaming throughput | **87 M ops/sec** | `bench_rdtsc` |
| Wire-to-book pipeline (parse → arena → match), P50 | **~175–190 ns** | `bench_zero_copy`, `bench_journal` |
| **Tick-to-trade** (packet-in → match → BBO → strategy → quote resting), P50 | **~700–840 ns** | `bench_trading_loop` |
| Risk gate accept path (all four checks) | **2.17 ns/order** (P99.9 12 ns) | `bench_risk` |
| Journal append (38-byte frame) | **3.9 ns**; replay at **25.1 M msgs/sec** | `bench_journal` |
| Sparse hash map find / insert (47% load, random 64-bit keys) | **10.7 / 12.6 ns** | `bench_flat_hash` |
| OFI + micro-price arithmetic per top-of-book change | **3.6 ns** | `bench_signals` |

Tail note: below ~P99.9 the numbers are the code; the sparse spikes above it
are OS preemption and timer interrupts on a desktop Windows box. In
production these are removed with core isolation and IRQ steering, not with
different code.

Hardware techniques in play throughout:

- **Serialised TSC measurement** so out-of-order execution cannot move the
  stop-stamp before the measured work retires.
- **Branch-shaping**: hot paths are written so the common case falls through;
  `LOB_LIKELY`/`UNLIKELY` document and enforce the layout. Where the data is
  *market-random* (see OFI below), branches are eliminated entirely.
- **`#pragma pack(1)` wire overlays + `BSWAP`**: ITCH fields decode as raw
  unaligned register loads plus one byte-swap instruction each — there is no
  "deserialisation" step to optimise because none exists.
- **Power-of-two everything**: ring capacities, table sizes, and rate windows
  are AND-masked, never divided.

---

## 5. Why It Is Built This Way

**Why a flat ladder instead of `std::map`.** A red-black tree lookup is
log₂(N) pointer chases to heap nodes scattered across DRAM; one miss (~100
ns) exceeds the entire operation budget. The ladder is one add and one
indexed load, and the 3-level bitmap finds the next occupied level with three
`TZCNT`s regardless of how sparse the book is — that property is what keeps
the *tail* flat, not just the median.

**Why the queues pass pointers, not structs.** Copying a decoded struct into
a ring and out again pays twice for data the consumer could read in place.
The arena ring publishes a sequence number with a release fence; the consumer
acquires it and decodes directly from the producer-written slot. The payload
crosses the core boundary exactly once — as the cache line itself.

**Why the risk gate is branch-free.** `check()` computes all four checks
(fat-finger, collar, worst-case exposure, rate) as 0/1 integers OR'd into a
reason mask, with exactly one conditional branch at the end — "mask == 0" —
which is virtually always true and therefore perfectly predicted. A rejected
order gets the complete set of violated checks for the drop-copy log at no
extra cost. The rate limit is an *exact* sliding window in O(1): a ring of
the last W accept timestamps; if the accept W messages ago is still inside
the window, admitting one more would exceed W-per-window.

**Why the journal is raw bytes, not records.** The engine is a deterministic
function of its inbound byte sequence, so the complete recovery story is:
keep the bytes. The journal is a byte-identical copy of the length-framed
wire stream, appended via mmap (a short memcpy plus a release-stored commit
watermark — no syscalls, no flushes, no blocking on the hot path). Replay
pushes the file through the *same* parser into a *fresh* engine; the
benchmark requires identical trade counts, traded lots, BBO, and an FNV-1a
fingerprint over every price level's aggregate quantity. The claim is proved
bit-for-bit, not asserted.

**Why backward-shift deletion instead of tombstones.** An ITCH session
erases order-id mappings at nearly the rate it inserts them (every fill and
cancel). Tombstones never shrink probe chains — by the afternoon every
lookup crawls through the morning's graves, and P99 degrades monotonically
all session. Backward-shift deletion restores the table to the state as if
the erased key had never existed, so probe lengths *cannot* grow over time:
the P99 at 15:59 equals the P99 at 09:31. The map itself is one contiguous
64-byte-aligned block of 16-byte `{key, value}` slots (four per cache line),
Fibonacci multiply-shift hashing (one `IMUL` + `SHR`), a 50% load cap
enforced at insert, and **no rehash path at all** — capacity is provisioned
like the order pool, and exceeding it rejects upstream rather than ever
allocating mid-session.

**Why OFI is computed with bool-to-int multiplies.** The OFI increment
(Cont/Kukanov/Stoikov) needs four indicator terms comparing current vs.
previous top-of-book prices. Price-move direction is market-random: a
branched implementation mispredicts ~50% of the time, and each mispredict
flushes the pipeline (~15–20 cycles — an eternity against the 5-cycle
budget). `Qty(bp >= last_bp) * bq` compiles to `SETcc` + `IMUL`: no jumps,
constant latency, immune to what the market does. The micro-price
`(Pb·qa + Pa·qb)/(qb + qa)` is exact Q56.8 fixed point — sub-tick resolution
with integer math — and its divide-by-zero guard adds `(denom == 0)` to the
denominator instead of testing it. Signals are computed on the engine core
because the inputs are already in registers there; the strategy receives
decision-ready numbers in a single-cache-line update.

**Why conflation drops instead of blocking.** If the market data ring fills,
the publisher drops the update and counts it. A market-maker needs the
*current* top of book, not a faithful history of stale ones; blocking the
engine to preserve intermediate BBOs would invert the system's priorities.
The strategy detects gaps via sequence numbers.

---

## 6. Resiliency & Verification

- **Deterministic replay**: journal → parser → fresh engine reproduces the
  live session's business state exactly (`[DETERMINISTIC]` gate in
  `bench_journal`, fingerprint over the full ladder).
- **Every benchmark self-verifies**: fill counts and traded lots are checked
  against the feed generator's ground truth (`[VERIFIED]` gates); the risk
  gate's rate and position limits have functional proofs; the hash map is
  cross-checked over 300k randomized operations against a dense oracle,
  including a full final-state audit.
- **Pre-trade risk** (SEC 15c3-5 posture): every strategy order is gated
  inline; a pending cancel is not treated as a confirmed cancel (worst-case
  exposure accounting), and limits are enforced before the order can enter
  the ring.
- **Crash safety**: the journal's commit watermark is release-stored after
  the frame bytes, so any reader — including a cross-process tailer — sees a
  valid, untorn, length-framed prefix. A process crash loses nothing that
  reached the page cache.

---

## 7. Build & Run

Requirements: x86-64 (RDTSC/RDTSCP, TZCNT), C++20 compiler (GCC/MinGW
tested), CMake ≥ 3.16. No external dependencies.

```powershell
# CMake
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build

# or direct GCC/MinGW invocation (any single target)
g++ -std=c++20 -O3 -march=native -fno-exceptions -fno-rtti -Iinclude `
    bench/bench_trading_loop.cpp -o build/bench_trading_loop.exe -static
```

Run order (each is standalone; the suite is the proof of the phase claims):

```powershell
./build/test_correctness.exe     # must print ALL TESTS PASSED
./build/bench_rdtsc.exe          # core engine op latency percentiles
./build/bench_itch_pipeline.exe  # ITCH parse -> ring -> match pipeline
./build/bench_zero_copy.exe      # struct-copy ring vs zero-copy arena A/B
./build/bench_trading_loop.exe   # closed loop: tick-to-trade percentiles
./build/bench_risk.exe           # risk gate: 2.2ns accept path proof
./build/bench_journal.exe        # journal cost + deterministic replay gate
./build/bench_flat_hash.exe      # sparse id map: oracle check + pipeline A/B
./build/bench_signals.exe        # OFI/micro-price: scripted proofs + A/B
```

All benchmarks pin threads, pre-touch every page they will touch, calibrate
the TSC against the steady clock, and report P50/P90/P99/P99.9/max with the
measurement overhead subtracted.
