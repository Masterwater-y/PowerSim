cmake_minimum_required(VERSION 3.10)
project(mesi_ref_sim CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-O2 -Wall -Wextra -Wno-unused-parameter)

# µarch oracle 共享头 (lru_banked.hh / uarch_profile.hh) 的唯一信源已迁入 FastSim
# 仓的 gem5-collection/overlay/taogen/shared (taogen 分支自 2026-06 冻结)。默认指向
# 同级 FastSim checkout, 可用 -DFASTSIM_SHARED=<path> 覆盖。保留旧 ../shared 作为
# 回退, 二者取先存在者, 以免脱离 FastSim 时无法构建。
set(FASTSIM_SHARED "${CMAKE_SOURCE_DIR}/../../FastSim/gem5-collection/overlay/taogen/shared"
    CACHE PATH "FastSim single-source path for shared uarch oracle headers")
if(EXISTS "${FASTSIM_SHARED}/uarch_profile.hh")
  set(SHARED_INC "${FASTSIM_SHARED}")
else()
  set(SHARED_INC "${CMAKE_SOURCE_DIR}/../shared")
endif()
message(STATUS "mesi_ref_sim shared uarch headers: ${SHARED_INC}")

add_executable(mesi_ref_sim src/main.cc)
target_include_directories(mesi_ref_sim PRIVATE include "${SHARED_INC}")
