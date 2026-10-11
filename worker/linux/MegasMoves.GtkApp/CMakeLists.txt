cmake_minimum_required(VERSION 3.25)
project(megasmoves_gtk LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
pkg_check_modules(GTK4 REQUIRED gtk4)
pkg_check_modules(ADW REQUIRED libadwaita-1)

add_executable(megasmoves_gtk
    src/main.cpp
    src/native_bridge.cpp
    src/core_process_manager.cpp
    ../../windows/native/src/megas_runtime.cpp
)

target_include_directories(megasmoves_gtk
    PRIVATE
        src
        ../../windows/native/include
        ${GTK4_INCLUDE_DIRS}
        ${ADW_INCLUDE_DIRS}
)

target_link_libraries(megasmoves_gtk
    PRIVATE
        ${GTK4_LIBRARIES}
        ${ADW_LIBRARIES}
        Threads::Threads
)

target_compile_options(megasmoves_gtk
    PRIVATE
        ${GTK4_CFLAGS_OTHER}
        ${ADW_CFLAGS_OTHER}
        -Wall -Wextra
)
