cmake_minimum_required(VERSION 3.37)
project(PSM2026_SCFE)


set(MIDASSYS $ENV{MIDASSYS})
set(CMAKE_CXX_STANDARD 20)

find_package(Threads REQUIRED)

add_executable(scfe
                pi_generic.cxx
                scfe.cxx
                arcus_stage_fe.cxx
                isel_fe.cxx
                caen_hv_fe.cxx
                hv_alarm.cxx
                ${MIDASSYS}/drivers/class/hv.cxx
            )

# ${MIDASSYS}/include/mscb holds mstrlcpy.h in this MIDAS install; hv.cxx includes it
target_include_directories(scfe PRIVATE ${MIDASSYS}/include ${MIDASSYS}/include/mscb ${MIDASSYS}/drivers/class)
target_link_libraries(scfe $ENV{MIDASSYS}/lib/libmfe.a $ENV{MIDASSYS}/lib/libmidas.a Threads::Threads libutil.so)

install(TARGETS scfe)
