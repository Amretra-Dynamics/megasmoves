cmake_minimum_required(VERSION 3.25)
project(megas_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(megas_native SHARED
    src/megas_runtime.cpp
)

target_include_directories(megas_native
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_compile_definitions(megas_native PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

if(WIN32)
    target_link_libraries(megas_native PRIVATE advapi32)
endif()

if(MSVC)
    target_compile_options(megas_native PRIVATE /W4 /permissive- /EHsc)
else()
    target_compile_options(megas_native PRIVATE -Wall -Wextra -Wpedantic)
endif()

include(CTest)
if(BUILD_TESTING)
    add_executable(megas_native_abi_smoke tests/abi_smoke.cpp)
    target_link_libraries(megas_native_abi_smoke PRIVATE megas_native)
    add_test(NAME megas_native_abi_smoke COMMAND megas_native_abi_smoke)
endif()
