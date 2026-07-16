# =============================================================================
# Ultra-Low Latency Limit Order Book -- build configuration.
#
# FLAG RATIONALE
#   -O3               : full optimisation, aggressive inlining (the engine is
#                       header-only by design so everything inlines into the
#                       caller's translation unit).
#   -march=native     : emit TZCNT/LZCNT/BMI2 for the bitmap, and schedule for
#                       the actual microarchitecture. A production build farm
#                       would pin an explicit -march (e.g. icelake-server).
#   -fno-exceptions   : no EH frames / landing pads on the hot path; every
#   -fno-rtti           hot function is noexcept anyway. Smaller, straighter
#                       code => better icache utilisation.
#   -fno-plt          : direct calls into shared libs (irrelevant here since
#                       we're static, but standard ULL hygiene).
#   -static           : (MinGW) no DLL loader indirection at startup.
# =============================================================================
cmake_minimum_required(VERSION 3.16)
project(ull_orderbook CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_library(lob INTERFACE)
target_include_directories(lob INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(MSVC)
    set(HOT_FLAGS /O2 /Ob3 /arch:AVX2 /GR- /EHs-c- /DNOMINMAX)
else()
    set(HOT_FLAGS -O3 -march=native -fno-exceptions -fno-rtti -fno-plt
                  -Wall -Wextra)
endif()

# --- correctness suite (run first: numbers are meaningless if wrong) ---
add_executable(test_correctness tests/test_correctness.cpp)
target_link_libraries(test_correctness PRIVATE lob)
target_compile_options(test_correctness PRIVATE ${HOT_FLAGS})

# --- RDTSC benchmark ---
add_executable(bench_rdtsc bench/bench_rdtsc.cpp)
target_link_libraries(bench_rdtsc PRIVATE lob)
target_compile_options(bench_rdtsc PRIVATE ${HOT_FLAGS})

# --- ITCH 5.0 gateway pipeline benchmark ---
add_executable(bench_itch_pipeline bench/bench_itch_pipeline.cpp)
target_link_libraries(bench_itch_pipeline PRIVATE lob)
target_compile_options(bench_itch_pipeline PRIVATE ${HOT_FLAGS})

# --- struct-copy ring vs zero-copy arena comparison ---
add_executable(bench_zero_copy bench/bench_zero_copy.cpp)
target_link_libraries(bench_zero_copy PRIVATE lob)
target_compile_options(bench_zero_copy PRIVATE ${HOT_FLAGS})

# --- Phase 3: closed trading loop (BBO publisher -> strategy -> engine) ---
add_executable(bench_trading_loop bench/bench_trading_loop.cpp)
target_link_libraries(bench_trading_loop PRIVATE lob)
target_compile_options(bench_trading_loop PRIVATE ${HOT_FLAGS})

# --- Phase 4: pre-trade risk gateway microbenchmark ---
add_executable(bench_risk bench/bench_risk.cpp)
target_link_libraries(bench_risk PRIVATE lob)
target_compile_options(bench_risk PRIVATE ${HOT_FLAGS})

# --- Phase 5: deterministic replay journal (cost + replay proof) ---
add_executable(bench_journal bench/bench_journal.cpp)
target_link_libraries(bench_journal PRIVATE lob)
target_compile_options(bench_journal PRIVATE ${HOT_FLAGS})

# --- Phase 6: sparse order-ref flat hash map ---
add_executable(bench_flat_hash bench/bench_flat_hash.cpp)
target_link_libraries(bench_flat_hash PRIVATE lob)
target_compile_options(bench_flat_hash PRIVATE ${HOT_FLAGS})

if(NOT MSVC)
    find_package(Threads REQUIRED)
    target_link_libraries(test_correctness PRIVATE Threads::Threads)
    target_link_libraries(bench_rdtsc PRIVATE Threads::Threads)
    target_link_libraries(bench_itch_pipeline PRIVATE Threads::Threads)
    target_link_libraries(bench_zero_copy PRIVATE Threads::Threads)
    target_link_libraries(bench_trading_loop PRIVATE Threads::Threads)
    target_link_libraries(bench_risk PRIVATE Threads::Threads)
    target_link_libraries(bench_journal PRIVATE Threads::Threads)
    target_link_libraries(bench_flat_hash PRIVATE Threads::Threads)
    if(WIN32)   # MinGW: fully static so no runtime DLL resolution
        target_link_options(bench_rdtsc PRIVATE -static)
        target_link_options(test_correctness PRIVATE -static)
        target_link_options(bench_itch_pipeline PRIVATE -static)
        target_link_options(bench_zero_copy PRIVATE -static)
        target_link_options(bench_trading_loop PRIVATE -static)
        target_link_options(bench_risk PRIVATE -static)
    endif()
endif()

enable_testing()
add_test(NAME correctness COMMAND test_correctness)
