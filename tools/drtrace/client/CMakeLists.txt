cmake_minimum_required(VERSION 3.13)
project(fastsim_div_capture C CXX)

if(NOT DEFINED DynamoRIO_DIR)
  message(FATAL_ERROR "DynamoRIO_DIR is required")
endif()
if(NOT DEFINED FASTSIM_DYNAMORIO_ROOT)
  message(FATAL_ERROR "FASTSIM_DYNAMORIO_ROOT is required")
endif()

find_package(DynamoRIO 11.3 REQUIRED)

set_source_files_properties(div_capture.c PROPERTIES LANGUAGE CXX)
add_library(fastsim_div_capture SHARED div_capture.c)
configure_DynamoRIO_client(fastsim_div_capture)
use_DynamoRIO_extension(fastsim_div_capture drmgr)
use_DynamoRIO_extension(fastsim_div_capture drreg)
use_DynamoRIO_extension(fastsim_div_capture drutil)
target_include_directories(fastsim_div_capture PRIVATE
  "${FASTSIM_DYNAMORIO_ROOT}/tools/include"
  "${CMAKE_CURRENT_SOURCE_DIR}/../../../include")
target_link_directories(fastsim_div_capture PRIVATE
  "${FASTSIM_DYNAMORIO_ROOT}/tools/lib64/release")
target_link_libraries(fastsim_div_capture drmemtrace)
set_target_properties(fastsim_div_capture PROPERTIES
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
  BUILD_RPATH "${FASTSIM_DYNAMORIO_ROOT}/tools/lib64/release;${FASTSIM_DYNAMORIO_ROOT}/ext/lib64/release;${FASTSIM_DYNAMORIO_ROOT}/lib64/release")
