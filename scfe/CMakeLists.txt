cmake_minimum_required(VERSION 3.37)
project(PSM2026_SCFE)


set(MIDASSYS $ENV{MIDASSYS})
set(CMAKE_CXX_STANDARD 20)

find_package(Threads REQUIRED)

add_executable(scfe
                pi_generic.cxx
                scfe.cxx
                arcus_stage_fe.cxx
            )

target_include_directories(scfe PRIVATE ${MIDASSYS}/include)
target_link_libraries(scfe $ENV{MIDASSYS}/lib/libmfe.a $ENV{MIDASSYS}/lib/libmidas.a Threads::Threads libutil.so)

install(TARGETS scfe)
