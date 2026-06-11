cmake_minimum_required(VERSION 3.10)
project(mesi_ref_sim CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-O2 -Wall -Wextra -Wno-unused-parameter)
add_executable(mesi_ref_sim src/main.cc)
target_include_directories(mesi_ref_sim PRIVATE include ../shared)

option(BUILD_PY "Build Python pybind11 module" ON)
if(BUILD_PY)
  find_package(Python3 COMPONENTS Interpreter Development REQUIRED)
  execute_process(
    COMMAND "${Python3_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
  if(PYBIND11_CMAKE_DIR)
    set(pybind11_DIR "${PYBIND11_CMAKE_DIR}")
  endif()
  find_package(pybind11 CONFIG REQUIRED)
  pybind11_add_module(ref_sim_py src/python_module.cc src/quantum.cc)
  target_include_directories(ref_sim_py PRIVATE include ../shared)
endif()
