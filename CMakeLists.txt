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
    if(TARGET fastsim_py)
        add_test(
            NAME fastsim_python_tests
            COMMAND "${CMAKE_COMMAND}" -E env
                    "PYTHONPATH=$<TARGET_FILE_DIR:fastsim_py>"
                    "${FASTSIM_PYTHON_TEST_INTERPRETER}"
                    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_python_bindings.py")
    endif()
endif()
