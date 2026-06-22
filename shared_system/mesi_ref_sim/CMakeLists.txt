cmake_minimum_required(VERSION 3.10)
project(llmsim_shared_system CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-O2 -Wall -Wextra -Wno-unused-parameter)

add_executable(llmsim_shared_system
  src/shared_system_main.cc
  src/quantum.cc
)
target_include_directories(llmsim_shared_system PRIVATE include ../shared)
