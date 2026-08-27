cmake_minimum_required(VERSION 3.37)
project(StageDriver_UDP)


set(CMAKE_CXX_STANDARD 20)

find_package(Threads REQUIRED)

add_executable(stageDriver
                main.cpp
                stageDriver.cpp
                ArcusPerformaxDriver.cpp
                )

target_link_libraries(stageDriver usb-1.0 Threads::Threads)


install(TARGETS stageDriver)
