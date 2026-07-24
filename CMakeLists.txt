cmake_minimum_required(VERSION 3.18)

project(tg-client-stdio
    VERSION 0.1.0
    DESCRIPTION "Telegram user-client stdio worker and C++ protocol helpers"
    LANGUAGES CXX
)

option(TG_CLIENT_STDIO_BUILD_TESTS "Build tg-client-stdio tests" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(tg_client_stdio INTERFACE)
add_library(tg-client-stdio::tg_client_stdio ALIAS tg_client_stdio)

target_include_directories(tg_client_stdio
    INTERFACE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)

target_compile_features(tg_client_stdio INTERFACE cxx_std_17)

if(TG_CLIENT_STDIO_BUILD_TESTS)
    enable_testing()

    add_executable(tg_protocol_envelope_test
        tests/cpp/protocol_envelope_test.cpp
    )
    target_link_libraries(tg_protocol_envelope_test
        PRIVATE
            tg-client-stdio::tg_client_stdio
    )
    add_test(NAME tg_protocol_envelope_test COMMAND tg_protocol_envelope_test)
endif()
