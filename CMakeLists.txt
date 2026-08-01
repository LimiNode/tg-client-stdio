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

add_subdirectory(external/cpp/tiny-process-library EXCLUDE_FROM_ALL)
add_subdirectory(external/cpp/nlohmann-json EXCLUDE_FROM_ALL)

add_library(tg_client_stdio INTERFACE)
add_library(tg-client-stdio::tg_client_stdio ALIAS tg_client_stdio)

target_include_directories(tg_client_stdio
    INTERFACE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
)

target_compile_features(tg_client_stdio INTERFACE cxx_std_17)
target_link_libraries(tg_client_stdio
    INTERFACE
        tiny-process-library::tiny-process-library
        nlohmann_json::nlohmann_json
)

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

    add_executable(tg_client_stdio_client_test
        tests/cpp/client_test.cpp
    )
    target_link_libraries(tg_client_stdio_client_test
        PRIVATE
            tg-client-stdio::tg_client_stdio
    )
    add_test(NAME tg_client_stdio_client_test COMMAND tg_client_stdio_client_test)

    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_Interpreter_FOUND)
        add_executable(tg_worker_client_test
            tests/cpp/worker_client_test.cpp
        )
        target_compile_definitions(tg_worker_client_test
            PRIVATE
                TG_CLIENT_STDIO_TEST_PYTHON_EXECUTABLE="${Python3_EXECUTABLE}"
        )
        target_link_libraries(tg_worker_client_test
            PRIVATE
                tg-client-stdio::tg_client_stdio
        )
        add_test(NAME tg_worker_client_test COMMAND tg_worker_client_test)
    endif()
endif()
