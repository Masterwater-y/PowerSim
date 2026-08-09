cmake_minimum_required(VERSION 3.13)
project(FastSim VERSION 0.1.0 LANGUAGES CXX)

option(FASTSIM_BUILD_TESTS "Build FastSim unit tests" ON)
option(FASTSIM_ENABLE_NATIVE "Tune release builds for the current host" OFF)
option(FASTSIM_ENABLE_IPO
       "Enable interprocedural optimization for release builds" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)

add_library(fastsim_lib
    src/cache.cpp
    src/config.cpp
    src/interval_core.cpp
    src/predictor.cpp
    src/simulator.cpp
    src/trace.cpp
)
target_include_directories(fastsim_lib PUBLIC include)
target_link_libraries(fastsim_lib PUBLIC Threads::Threads)
target_compile_options(fastsim_lib PRIVATE
    -Wall -Wextra -Wpedantic -Wconversion -Wshadow
    $<$<CONFIG:Release>:-O3>
)
if(FASTSIM_ENABLE_NATIVE)
    target_compile_options(fastsim_lib PRIVATE $<$<CONFIG:Release>:-march=native>)
endif()

add_executable(fastsim src/main.cpp)
target_link_libraries(fastsim PRIVATE fastsim_lib)

if(FASTSIM_ENABLE_IPO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT FASTSIM_IPO_SUPPORTED
                        OUTPUT FASTSIM_IPO_ERROR
                        LANGUAGES CXX)
    if(FASTSIM_IPO_SUPPORTED)
        set_property(TARGET fastsim_lib PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET fastsim PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
    else()
        message(WARNING
                "FASTSIM_ENABLE_IPO=ON, but IPO is unavailable: "
                "${FASTSIM_IPO_ERROR}")
    endif()
endif()

if(FASTSIM_BUILD_TESTS)
    enable_testing()
    target_compile_definitions(
        fastsim_lib PRIVATE FASTSIM_ENABLE_TEST_HOOKS)
    add_executable(fastsim_tests tests/test_main.cpp)
    target_link_libraries(fastsim_tests PRIVATE fastsim_lib)
    if(FASTSIM_ENABLE_IPO AND FASTSIM_IPO_SUPPORTED)
        set_property(TARGET fastsim_tests PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
    endif()
    target_compile_definitions(
        fastsim_tests PRIVATE
        FASTSIM_ENABLE_TEST_HOOKS
        FASTSIM_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
    add_test(NAME fastsim_tests COMMAND fastsim_tests)
endif()
