# Ultra-Low Latency Limit Order Book (C++20)

Production-grade price-time-priority matching engine engineered for
deterministic, nanosecond-scale execution. No standard-library containers on
the hot path, no heap traffic after initialisation, no locks anywhere.

## Measured results (GCC 16.1, `-O3 -march=native`, Windows x64, TSC @ 1.498 GHz)

2,000,000 operations producing **1,012,294 individual order matches**, each
op timed with a serialised RDTSC pair (`LFENCE;RDTSC` … `RDTSCP;LFENCE`),
harness overhead measured and subtracted:

| Percentile | Engine op latency |
|-----------:|------------------:|
| P50        | **15.4 ns** (27.4 ns raw incl. harness) |
| P90        | 34.1 ns  |
| P99        | 80.8 ns  |
| P99.9      | 399 ns   |
| P99.99     | 5.8 µs (OS timer interrupts — see note) |

- Streaming throughput (no per-op fencing): **11.5 ns/op, 87 M ops/sec**
- SPSC pipeline (producer core → engine core): **31.2 M msgs/sec sustained**
- SPSC ring one-way cross-core latency: **P50 ≈ 159 ns** (bounded by the
  hardware cache-coherence hop, ~100–150 ns on any multi-core x86)

> Tail note: everything ≤ P99.9 is the engine. The sparse ≥ P99.99 spikes are
> OS preemption/interrupts — on a production deployment these are removed
> with core isolation (`isolcpus`, `nohz_full`, IRQ steering), not code.

## Architecture

```
 network thread (core 0)                     engine thread (core 2)
┌──────────────────────┐   lock-free SPSC   ┌───────────────────────────────┐
│  feed decode /       │   ring buffer      │  OrderBook (single writer)    │
│  order gateway       │ ═════ 16k slots ══▶│   ├─ flat PriceLevel[65536]   │
│  (producer)          │  acquire/release   │   ├─ hierarchical bitmaps     │
└──────────────────────┘   atomics only     │   ├─ intrusive order FIFOs    │
                                            │   └─ MemoryPool<Order> arena  │
                                            └───────────────────────────────┘
```

| File | Component | Key latency decisions |
|------|-----------|----------------------|
| `include/lob/common.hpp` | types & constants | fixed-point int64 prices (no FP), 64B cache-line constant, branch hints |
| `include/lob/order.hpp` | `Order` | exactly one cache line, `alignas(64)`, intrusive `next`/`prev` embedded |
| `include/lob/memory_pool.hpp` | arena | one up-front allocation, pre-touched pages, LIFO intrusive free list, placement-new acquire — **zero heap ops on hot path** |
| `include/lob/spsc_ring.hpp` | LMAX-style queue | acquire/release atomics only, producer/consumer indices on separate cache lines, cached peer index to avoid coherence ping-pong |
| `include/lob/level_bitmap.hpp` | occupancy index | 3-level bitmap + TZCNT ⇒ "next non-empty level" is O(1) regardless of price gap — this is what bounds the tail |
| `include/lob/order_book.hpp` | matching engine | flat array price ladder (O(1) lookup, no tree walk), O(1) intrusive cancel, generation-checked ids (no hash map) |
| `include/lob/rdtsc.hpp` | timing | serialised TSC reads, GHz calibration, harness-overhead subtraction |
| `bench/bench_rdtsc.cpp` | benchmark suite | pinned threads, warm-up, deterministic workload, full percentile report |
| `tests/test_correctness.cpp` | functional tests | matching, price-time priority, cancels, generation safety, 4M-item cross-thread ring test |

### Why not `std::map` / `std::list` / `std::shared_ptr`

- `std::map` is a red-black tree: every lookup is ~log₂(N) pointer chases to
  heap nodes scattered across memory. One DRAM miss (~100 ns) already blows a
  50 ns budget. The flat ladder does one add + one indexed load.
- `std::list` allocates a control node per element. The intrusive list stores
  links inside the 64-byte `Order` itself: one cache line per order, O(1)
  unlink for cancels, zero allocations.
- `std::shared_ptr` means atomic refcount traffic and non-deterministic
  destruction. The arena owns all storage for the process lifetime; ids carry
  a generation counter so stale references are rejected in O(1).

## Build & run

```powershell
# CMake
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
# or directly
g++ -std=c++20 -O3 -march=native -fno-exceptions -fno-rtti -Iinclude `
    tests/test_correctness.cpp -o build/test_correctness.exe
g++ -std=c++20 -O3 -march=native -fno-exceptions -fno-rtti -Iinclude `
    bench/bench_rdtsc.cpp -o build/bench_rdtsc.exe

./build/test_correctness.exe   # must print ALL TESTS PASSED
./build/bench_rdtsc.exe        # RDTSC latency report
```

Requires x86-64 (RDTSC/RDTSCP, TZCNT). C++20. No external dependencies.
