cmake_minimum_required(VERSION 3.16)
project(arthera_quant_kernel LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_library(arthera_quant_kernel STATIC quant_kernel.cpp)
target_compile_options(arthera_quant_kernel PRIVATE -O3 -Wall -Wextra)

enable_testing()
add_executable(quant_kernel_test quant_kernel_test.cpp)
target_link_libraries(quant_kernel_test PRIVATE arthera_quant_kernel)
add_test(NAME quant_kernel_test COMMAND quant_kernel_test)
