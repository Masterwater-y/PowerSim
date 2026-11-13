cmake_minimum_required(VERSION 3.13)
project(FastSim VERSION 0.1.0 LANGUAGES CXX)

if(POLICY CMP0069)
    cmake_policy(SET CMP0069 NEW)
    set(CMAKE_POLICY_DEFAULT_CMP0069 NEW)
endif()

option(FASTSIM_BUILD_TESTS "Build FastSim unit tests" ON)
option(FASTSIM_BUILD_PYTHON "Build the pybind11 Python module" OFF)
set(FASTSIM_PYTHON_EXECUTABLE "" CACHE FILEPATH
    "Python interpreter/venv used to build and test fastsim_py")
option(FASTSIM_ENABLE_NATIVE "Tune release builds for the current host" OFF)
option(FASTSIM_ENABLE_IPO
       "Enable interprocedural optimization for release builds" ON)
set(FASTSIM_LTO_JOBS "16" CACHE STRING
    "Parallel GCC LTO code-generation jobs per link")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)
find_package(ZLIB REQUIRED)

add_library(fastsim_lib
    src/cache.cpp
    src/config.cpp
    src/interval_core.cpp
    src/spr_frontend.cpp
    src/retired_frontend_facts.cpp
    src/functional_sync.cpp
    src/recorded_scheduler.cpp
    src/bounded_store_timing.cpp
    src/store_request_engine.cpp
    src/mixed_dram.cpp
    src/predictor.cpp
    src/simulator.cpp
    src/fst_dynamic_translations.cpp
    src/fst_instruction_translations.cpp
    src/fst_core_functional_reader.cpp
    src/fst_mmu_control_reader.cpp
    src/fst_architectural_boundary_reader.cpp
    src/fst_mmu_control_v2_reader.cpp
    src/fst_static_uop_reader.cpp
    src/fst_store_facts_io.cpp
    src/trace.cpp
)
target_include_directories(fastsim_lib PUBLIC include)
target_link_libraries(fastsim_lib PUBLIC Threads::Threads ZLIB::ZLIB)
target_compile_options(fastsim_lib PRIVATE
    -Wall -Wextra -Wpedantic -Wconversion -Wshadow
    $<$<CONFIG:Release>:-O3>
)
if(FASTSIM_ENABLE_NATIVE)
    target_compile_options(fastsim_lib PRIVATE $<$<CONFIG:Release>:-march=native>)
endif()

add_executable(fastsim_fst_stream_hash tools/fst_stream_hash.cpp)
target_compile_options(fastsim_fst_stream_hash PRIVATE -O3)
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    target_link_options(fastsim_fst_stream_hash PRIVATE -static-libstdc++ -static-libgcc)
endif()

add_executable(fastsim src/main.cpp)
target_link_libraries(fastsim PRIVATE fastsim_lib)
add_executable(fastsim_audit_retired_frontend tools/audit_fst_retired_frontend.cpp)
target_link_libraries(fastsim_audit_retired_frontend PRIVATE fastsim_lib)

add_executable(fastsim_replay_projected_dram tools/replay_projected_dram.cpp)
target_link_libraries(fastsim_replay_projected_dram PRIVATE fastsim_lib)

if(FASTSIM_BUILD_PYTHON)
    # A static fastsim_lib is linked into the Python extension on the default
    # build, so all of its objects must be position independent.
    set_property(TARGET fastsim_lib PROPERTY POSITION_INDEPENDENT_CODE ON)

    # CMake 3.13 cannot reliably join a virtualenv interpreter with the base
    # installation's development files. Query sys.base_prefix ourselves when
    # the caller selects an interpreter, while retaining that interpreter for
    # pybind11 discovery and tests.
    if(FASTSIM_PYTHON_EXECUTABLE)
        if(NOT EXISTS "${FASTSIM_PYTHON_EXECUTABLE}")
            message(FATAL_ERROR
                    "FASTSIM_PYTHON_EXECUTABLE does not exist: "
                    "${FASTSIM_PYTHON_EXECUTABLE}")
        endif()
        execute_process(
            COMMAND "${FASTSIM_PYTHON_EXECUTABLE}" -c
                    "import sys; print(sys.base_prefix)"
            RESULT_VARIABLE FASTSIM_PYTHON_PREFIX_STATUS
            OUTPUT_VARIABLE FASTSIM_PYTHON_BASE_PREFIX
            ERROR_VARIABLE FASTSIM_PYTHON_PREFIX_ERROR
            OUTPUT_STRIP_TRAILING_WHITESPACE)
        if(NOT FASTSIM_PYTHON_PREFIX_STATUS EQUAL 0 OR
           NOT IS_DIRECTORY "${FASTSIM_PYTHON_BASE_PREFIX}")
            message(FATAL_ERROR
                    "cannot query sys.base_prefix from "
                    "FASTSIM_PYTHON_EXECUTABLE: "
                    "${FASTSIM_PYTHON_PREFIX_ERROR}")
        endif()
        if(NOT Python3_ROOT_DIR)
            set(Python3_ROOT_DIR "${FASTSIM_PYTHON_BASE_PREFIX}")
        endif()
        set(FASTSIM_PYBIND11_INTERPRETER
            "${FASTSIM_PYTHON_EXECUTABLE}")
        set(FASTSIM_PYTHON_TEST_INTERPRETER
            "${FASTSIM_PYTHON_EXECUTABLE}")
    endif()

    set(PYBIND11_FINDPYTHON ON)
    find_package(Python3 3.8 COMPONENTS Interpreter Development REQUIRED)
    if(NOT FASTSIM_PYBIND11_INTERPRETER)
        set(FASTSIM_PYBIND11_INTERPRETER "${Python3_EXECUTABLE}")
    endif()
    if(NOT FASTSIM_PYTHON_TEST_INTERPRETER)
        set(FASTSIM_PYTHON_TEST_INTERPRETER "${Python3_EXECUTABLE}")
    endif()

    # A pip/venv installation is not normally on CMake's prefix path. Ask the
    # selected interpreter for its pybind11 package before falling back to a
    # system CMake package.
    execute_process(
        COMMAND "${FASTSIM_PYBIND11_INTERPRETER}"
                -m pybind11 --cmakedir
        RESULT_VARIABLE FASTSIM_PYBIND11_CMAKEDIR_STATUS
        OUTPUT_VARIABLE FASTSIM_PYBIND11_CMAKEDIR
        ERROR_QUIET
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(FASTSIM_PYBIND11_CMAKEDIR_STATUS EQUAL 0 AND
       IS_DIRECTORY "${FASTSIM_PYBIND11_CMAKEDIR}")
        list(APPEND CMAKE_PREFIX_PATH "${FASTSIM_PYBIND11_CMAKEDIR}")
    endif()
    find_package(pybind11 CONFIG REQUIRED)

    # FastSim already owns the project-wide IPO decision below. Avoid
    # pybind11's independent LTO/strip extras so the module follows the same
    # build policy as the C++ targets.
    pybind11_add_module(
        fastsim_py MODULE NO_EXTRAS python/bindings.cpp)
    target_link_libraries(fastsim_py PRIVATE fastsim_lib)
    target_compile_definitions(
        fastsim_py PRIVATE FASTSIM_PY_VERSION="${PROJECT_VERSION}")
    target_compile_options(fastsim_py PRIVATE
        -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
    set_target_properties(fastsim_py PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python")
endif()

if(FASTSIM_ENABLE_IPO)
    include(CheckIPOSupported)
    check_ipo_supported(RESULT FASTSIM_IPO_SUPPORTED
                        OUTPUT FASTSIM_IPO_ERROR
                        LANGUAGES CXX)
    if(FASTSIM_IPO_SUPPORTED)
        if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
            # Make -j controls build targets, not GCC's LTRANS workers.
            # Propagate the link-time limit to every consumer of fastsim_lib.
            target_link_options(fastsim_lib INTERFACE
                "$<$<CONFIG:Release>:-flto=${FASTSIM_LTO_JOBS}>")
        endif()
        set_property(TARGET fastsim_lib PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        set_property(TARGET fastsim PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        if(TARGET fastsim_py)
            set_property(TARGET fastsim_py PROPERTY
                         INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        endif()
    else()
        message(WARNING
                "FASTSIM_ENABLE_IPO=ON, but IPO is unavailable: "
                "${FASTSIM_IPO_ERROR}")
    endif()
endif()

if(FASTSIM_BUILD_TESTS)
    add_executable(fastsim_spr_rename_elimination_tests tests/test_spr_rename_elimination.cpp)
    target_compile_definitions(fastsim_spr_rename_elimination_tests PRIVATE FASTSIM_RENAME_STANDALONE)
    target_link_libraries(fastsim_spr_rename_elimination_tests PRIVATE fastsim_lib)
    add_executable(fastsim_parent_service_tests tests/test_parent_service.cpp)
    target_link_libraries(fastsim_parent_service_tests PRIVATE fastsim_lib)
    add_executable(fastsim_legacy_frfcfs_single_pass_tests tests/test_legacy_frfcfs_single_pass.cpp)
    target_link_libraries(fastsim_legacy_frfcfs_single_pass_tests PRIVATE fastsim_lib)
    add_executable(fastsim_spr_l2_shared_prefetch_runtime_tests tests/test_spr_l2_shared_prefetch_runtime.cpp)
    target_link_libraries(fastsim_spr_l2_shared_prefetch_runtime_tests PRIVATE fastsim_lib)
    add_executable(fastsim_spr_l2_llc_resident_runtime_tests tests/test_spr_l2_llc_resident_runtime.cpp)
    target_link_libraries(fastsim_spr_l2_llc_resident_runtime_tests PRIVATE fastsim_lib)
    add_executable(fastsim_spr_store_group_runtime_tests tests/test_spr_store_group_runtime.cpp)
    target_compile_definitions(fastsim_spr_store_group_runtime_tests PRIVATE FASTSIM_SPR_STORE_GROUP_STANDALONE)
    target_link_libraries(fastsim_spr_store_group_runtime_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_runtime_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_warmup_tests tests/test_spr_store_group_warmup.cpp)
    target_link_libraries(fastsim_spr_store_group_warmup_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_warmup_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_c4_tests tests/test_spr_store_group_c4.cpp)
    target_link_libraries(fastsim_spr_store_group_c4_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_c4_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_frfcfs_tests tests/test_spr_store_group_frfcfs.cpp)
    add_executable(fastsim_spr_ifetch_sequencer_tests tests/test_spr_ifetch_sequencer_runtime.cpp)
    target_link_libraries(fastsim_spr_ifetch_sequencer_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_ifetch_sequencer_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_id_stlb_tests tests/test_spr_id_stlb_runtime.cpp)
    target_link_libraries(fastsim_id_stlb_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_id_stlb_tests PRIVATE -flto=16)
    endif()
    target_link_libraries(fastsim_spr_store_group_frfcfs_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_frfcfs_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_bounded_neighbors_tests tests/test_spr_store_group_bounded_neighbors.cpp)
    target_link_libraries(fastsim_spr_store_group_bounded_neighbors_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_bounded_neighbors_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_exception_boundaries_tests tests/test_spr_store_group_exception_boundaries.cpp)
    target_link_libraries(fastsim_spr_store_group_exception_boundaries_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_exception_boundaries_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_rejection_counters_tests tests/test_spr_store_group_rejection_counters.cpp)
    target_link_libraries(fastsim_spr_store_group_rejection_counters_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_rejection_counters_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_line_shape_tests tests/test_spr_store_group_line_shape.cpp)
    target_link_libraries(fastsim_spr_store_group_line_shape_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_line_shape_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_spr_store_group_up_to_four_tests tests/test_spr_store_group_up_to_four.cpp)
    target_link_libraries(fastsim_spr_store_group_up_to_four_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
        target_link_options(fastsim_spr_store_group_up_to_four_tests PRIVATE -flto=16)
    endif()
    add_executable(fastsim_parallel_trace_open_tests tests/test_parallel_trace_open.cpp)
    target_link_libraries(fastsim_parallel_trace_open_tests PRIVATE fastsim_lib)
    add_executable(fastsim_architectural_boundary_bundle_integration_tests tests/test_architectural_boundary_bundle_integration.cpp)
    target_include_directories(fastsim_architectural_boundary_bundle_integration_tests PRIVATE gem5-spr/src gem5-spr/taogen/shared)
    target_link_libraries(fastsim_architectural_boundary_bundle_integration_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_architectural_boundary_bundle_integration_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_architectural_boundary_observer_tests tests/test_architectural_boundary_observer.cpp)
    target_include_directories(fastsim_architectural_boundary_observer_tests PRIVATE include gem5-spr/src gem5-spr/taogen/shared)
    target_link_libraries(fastsim_architectural_boundary_observer_tests PRIVATE ZLIB::ZLIB)
    target_compile_options(fastsim_architectural_boundary_observer_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_fst_architectural_boundary_reader_tests tests/test_fst_architectural_boundary_reader.cpp)
    target_include_directories(fastsim_fst_architectural_boundary_reader_tests PRIVATE src)
    target_link_libraries(fastsim_fst_architectural_boundary_reader_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_fst_architectural_boundary_reader_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_fst_architectural_bundle_codecs_tests tests/test_fst_architectural_bundle_codecs.cpp)
    target_include_directories(fastsim_fst_architectural_bundle_codecs_tests PRIVATE include)
    target_link_libraries(fastsim_fst_architectural_bundle_codecs_tests PRIVATE ZLIB::ZLIB)
    target_compile_options(fastsim_fst_architectural_bundle_codecs_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_mmu_controls_tests tests/test_fst_mmu_controls.cpp)
    target_link_libraries(fastsim_mmu_controls_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_mmu_controls_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_mmu_control_observer_tests tests/test_mmu_control_observer.cpp)
    target_link_libraries(fastsim_mmu_control_observer_tests PRIVATE fastsim_lib)
    target_include_directories(fastsim_mmu_control_observer_tests PRIVATE
        gem5-spr/src gem5-spr/taogen/shared)
    target_compile_options(fastsim_mmu_control_observer_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_data_origin_tests tests/test_fst_data_origins.cpp)
    target_link_libraries(fastsim_data_origin_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_data_origin_tests PRIVATE -UNDEBUG)
    foreach(SPR_COMPONENT fst_core_functional_facts fst_instruction_translations fst_instruction_fetch spr_mechanism_profile fst_store_facts fst_paged_request fst_static_uops)
        add_executable(fastsim_${SPR_COMPONENT}_tests tests/test_${SPR_COMPONENT}.cpp)
        target_link_libraries(fastsim_${SPR_COMPONENT}_tests PRIVATE fastsim_lib)
        target_compile_options(fastsim_${SPR_COMPONENT}_tests PRIVATE -UNDEBUG)
    endforeach()
    add_executable(fastsim_spr_frontend_tests tests/test_spr_frontend.cpp)
    target_link_libraries(fastsim_spr_frontend_tests PRIVATE fastsim_lib)
    add_executable(fastsim_retired_frontend_facts_tests tests/test_retired_frontend_facts.cpp)
    target_link_libraries(fastsim_retired_frontend_facts_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_retired_frontend_facts_tests PRIVATE -UNDEBUG)
    add_executable(fastsim_spr_frontend_integration_tests tests/test_spr_frontend_integration.cpp)
    target_link_libraries(fastsim_spr_frontend_integration_tests PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_spr_frontend_integration_tests PRIVATE FASTSIM_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
    add_executable(fastsim_spr_fu_branch_rotation_tests tests/test_spr_fu_branch_rotation_regression.cpp)
    target_link_libraries(fastsim_spr_fu_branch_rotation_tests PRIVATE fastsim_lib)
    add_executable(fastsim_spr_integer_family_interval_tests tests/test_spr_integer_family_interval.cpp)
    target_link_libraries(fastsim_spr_integer_family_interval_tests PRIVATE fastsim_lib)
    add_executable(fastsim_replay_dram_probe tools/replay_dram_probe.cpp)
    target_link_libraries(fastsim_replay_dram_probe PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_replay_dram_probe PRIVATE FASTSIM_ENABLE_TEST_HOOKS)
    add_executable(fastsim_probe_dram_mechanisms tools/probe_dram_mechanisms.cpp)
    target_link_libraries(fastsim_probe_dram_mechanisms PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_probe_dram_mechanisms PRIVATE FASTSIM_ENABLE_TEST_HOOKS)
    enable_testing()
    target_compile_definitions(
        fastsim_lib PRIVATE FASTSIM_ENABLE_TEST_HOOKS)
    add_executable(
        fastsim_tests
        tests/test_main.cpp
        tests/test_memory_request_roles.cpp
        tests/test_functional_sync_runtime.cpp
        tests/test_thread_state_runtime.cpp
        tests/test_recorded_scheduler.cpp
        tests/test_recorded_scheduler_integration.cpp
    tests/test_task_pmu_runtime.cpp
        tests/test_interval_dependencies.cpp
        tests/test_response_completion.cpp
        tests/test_spr_rename_elimination.cpp
        tests/test_mixed_dram.cpp
        tests/test_dram_address.cpp
        tests/test_dram_refresh.cpp
        tests/test_cache_probe_fill.cpp
        tests/test_spr_l1_prefetch_runtime.cpp
        tests/test_spr_legacy_store_perf.cpp
        tests/test_spr_store_group_runtime.cpp
        tests/test_cross_q_config.cpp
        tests/test_cross_q_runtime.cpp
        tests/test_shared_pending_fill.cpp
        tests/test_shared_mixed_service.cpp
        tests/test_pending_resources.cpp
        tests/test_store_engine_noaccess_chain.cpp
        tests/test_bounded_analytic_integration.cpp
        tests/test_bounded_store_timing.cpp
        tests/test_store_engine_integration.cpp
        tests/test_store_request_engine.cpp
        tests/test_line_generation_coordinator.cpp
        tests/test_load_component_preflight.cpp)
    target_link_libraries(fastsim_tests PRIVATE fastsim_lib)
    add_executable(fastsim_bounded_analytic_integration_tests tests/test_bounded_analytic_integration.cpp)
    target_compile_definitions(fastsim_bounded_analytic_integration_tests PRIVATE FASTSIM_BOUNDED_INTEGRATION_STANDALONE)
    target_link_libraries(fastsim_bounded_analytic_integration_tests PRIVATE fastsim_lib)
    add_executable(fastsim_bounded_store_timing_tests tests/test_bounded_store_timing.cpp)
    target_compile_definitions(fastsim_bounded_store_timing_tests PRIVATE FASTSIM_BOUNDED_COMPONENT_STANDALONE)
    target_link_libraries(fastsim_bounded_store_timing_tests PRIVATE fastsim_lib)
    add_executable(fastsim_bounded_store_resource_lifecycle_tests tests/test_bounded_store_resource_lifecycle.cpp)
    target_link_libraries(fastsim_bounded_store_resource_lifecycle_tests PRIVATE fastsim_lib)
    add_executable(fastsim_legacy_prepare_timing_tests tests/test_legacy_prepare_timing.cpp)
    target_link_libraries(fastsim_legacy_prepare_timing_tests PRIVATE fastsim_lib)
    add_executable(fastsim_legacy_prepare_runtime_tests tests/test_legacy_prepare_runtime.cpp)
    target_link_libraries(fastsim_legacy_prepare_runtime_tests PRIVATE fastsim_lib)
    add_executable(fastsim_spr_store_window_audit_tests tests/test_spr_store_window_audit.cpp)
    target_link_libraries(fastsim_spr_store_window_audit_tests PRIVATE fastsim_lib)
    add_executable(fastsim_bounded_store_shared_transaction_tests tests/test_bounded_store_shared_transaction.cpp)
    target_compile_definitions(fastsim_bounded_store_shared_transaction_tests PRIVATE FASTSIM_ENABLE_TEST_HOOKS)
    target_link_libraries(fastsim_bounded_store_shared_transaction_tests PRIVATE fastsim_lib)
    add_executable(fastsim_store_engine_noaccess_tests tests/test_store_engine_noaccess_chain.cpp)
    target_compile_definitions(fastsim_store_engine_noaccess_tests PRIVATE FASTSIM_STORE_ENGINE_NOACCESS_STANDALONE)
    target_link_libraries(fastsim_store_engine_noaccess_tests PRIVATE fastsim_lib)
    add_executable(fastsim_store_engine_integration_tests tests/test_store_engine_integration.cpp)
    target_compile_definitions(fastsim_store_engine_integration_tests PRIVATE FASTSIM_STORE_ENGINE_INTEGRATION_STANDALONE)
    target_link_libraries(fastsim_store_engine_integration_tests PRIVATE fastsim_lib)
    add_executable(fastsim_store_request_allocation_tests tests/test_store_request_engine_allocation.cpp)
    target_link_libraries(fastsim_store_request_allocation_tests PRIVATE fastsim_lib)
    add_test(NAME fastsim_store_request_allocation_tests COMMAND fastsim_store_request_allocation_tests)
    add_executable(fastsim_store_request_engine_tests tests/test_store_request_engine.cpp)
    target_compile_definitions(fastsim_store_request_engine_tests PRIVATE FASTSIM_STORE_REQUEST_ENGINE_STANDALONE)
    target_link_libraries(fastsim_store_request_engine_tests PRIVATE fastsim_lib)
    add_test(NAME fastsim_store_request_engine_tests COMMAND fastsim_store_request_engine_tests)
    add_executable(fastsim_perf_cache_integration_tests tests/test_perf_cache_runtime_integration.cpp)
    target_link_libraries(fastsim_perf_cache_integration_tests PRIVATE fastsim_lib)
    add_test(NAME fastsim_perf_cache_integration_tests COMMAND fastsim_perf_cache_integration_tests)
    add_executable(fastsim_perf_cache_runtime_tests tests/test_perf_cache_runtime.cpp)
    target_link_libraries(fastsim_perf_cache_runtime_tests PRIVATE fastsim_lib)
    add_test(NAME fastsim_perf_cache_runtime_tests COMMAND fastsim_perf_cache_runtime_tests)
    add_executable(fastsim_perf_retirement_tests tests/test_perf_retirement.cpp)
    target_link_libraries(fastsim_perf_retirement_tests PRIVATE fastsim_lib)
    add_test(NAME fastsim_perf_retirement_tests COMMAND fastsim_perf_retirement_tests)
    add_executable(fastsim_task_clock_tests tests/test_task_clock.cpp)
    target_link_libraries(fastsim_task_clock_tests PRIVATE fastsim_lib)
    target_compile_options(fastsim_task_clock_tests PRIVATE -UNDEBUG)
    add_test(NAME fastsim_task_clock_tests COMMAND fastsim_task_clock_tests)
    set_tests_properties(fastsim_task_clock_tests PROPERTIES
        WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    if(FASTSIM_ENABLE_IPO AND FASTSIM_IPO_SUPPORTED)
        set_property(TARGET fastsim_tests PROPERTY
                     INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
    endif()
    target_compile_definitions(
        fastsim_tests PRIVATE
        FASTSIM_ENABLE_TEST_HOOKS
        FASTSIM_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
    add_test(NAME fastsim_tests COMMAND fastsim_tests)
    add_executable(fastsim_cross_q_runtime_tests tests/test_cross_q_runtime.cpp)
    add_executable(fastsim_shared_mixed_service_tests tests/test_shared_mixed_service.cpp)
    target_link_libraries(fastsim_shared_mixed_service_tests PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_shared_mixed_service_tests PRIVATE
        FASTSIM_ENABLE_TEST_HOOKS FASTSIM_SHARED_MIXED_SERVICE_STANDALONE)
    target_link_libraries(fastsim_cross_q_runtime_tests PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_cross_q_runtime_tests PRIVATE
        FASTSIM_CROSS_Q_RUNTIME_STANDALONE
        FASTSIM_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
    add_executable(
        fastsim_cross_q_config_tests
        tests/test_cross_q_config.cpp)
    target_link_libraries(fastsim_cross_q_config_tests PRIVATE fastsim_lib)
    target_compile_definitions(
        fastsim_cross_q_config_tests PRIVATE
        FASTSIM_CROSS_Q_CONFIG_STANDALONE
        FASTSIM_PROJECT_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
    add_test(
        NAME fastsim_cross_q_config_tests
        COMMAND fastsim_cross_q_config_tests)
    if(TARGET fastsim_py)
        add_test(
            NAME fastsim_python_tests
            COMMAND "${CMAKE_COMMAND}" -E env
                    "PYTHONPATH=$<TARGET_FILE_DIR:fastsim_py>"
                    "${FASTSIM_PYTHON_TEST_INTERPRETER}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_python_bindings.py")
    endif()
endif()

if(FASTSIM_BUILD_TESTS)
    add_executable(fastsim_dynamic_translation_tests tests/test_dynamic_translation_reader.cpp)
    target_link_libraries(fastsim_dynamic_translation_tests PRIVATE fastsim_lib)
    add_executable(fastsim_dynamic_dtlb_tests tests/test_dynamic_dtlb.cpp)
    target_link_libraries(fastsim_dynamic_dtlb_tests PRIVATE fastsim_lib)
    add_executable(fastsim_validated_cache_tests tests/test_validated_input_cache.cpp)
    target_link_libraries(fastsim_validated_cache_tests PRIVATE fastsim_lib)
    add_executable(fastsim_dynamic_hierarchy_tests tests/test_dynamic_hierarchy.cpp)
    target_link_libraries(fastsim_dynamic_hierarchy_tests PRIVATE fastsim_lib)
    add_executable(fastsim_dram_controller_observation_tests tests/test_dram_controller_observation.cpp)
    add_executable(fastsim_dram_refresh_tests tests/test_dram_refresh.cpp)
    target_link_libraries(fastsim_dram_refresh_tests PRIVATE fastsim_lib)
    target_compile_definitions(fastsim_dram_refresh_tests PRIVATE FASTSIM_ENABLE_TEST_HOOKS FASTSIM_DRAM_REFRESH_STANDALONE)
    target_include_directories(fastsim_dram_controller_observation_tests PRIVATE include)
    target_compile_definitions(fastsim_dram_controller_observation_tests PRIVATE FASTSIM_DRAM_OBSERVATION_STANDALONE)
    add_executable(fastsim_spr_multi_store_group_tests tests/test_spr_multi_store_group.cpp)
    target_link_libraries(fastsim_spr_multi_store_group_tests PRIVATE fastsim_lib)
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
      target_link_options(fastsim_spr_multi_store_group_tests PRIVATE -flto=16)
    endif()
endif()
