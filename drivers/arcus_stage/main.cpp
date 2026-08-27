/*
* File: main.cpp
* Author: Patrick Schwendimann
* Driver: Arcus Perfomax Stage
*
* This code should be compiled on the computer to which the
* stage is connected. It will listen on port 5555 and wait
* for MIDAS to connect.
*
* Alternatively, one can use nc -u XXX.XXX.XXX.XXX 5555
* to connect a terminal for simple instructions.
*/



#include "stageDriver.hh"
#include <iostream>



int main(int argc, char **argv) {

    StageDriver *aDriver = new StageDriver();

    aDriver->SetPort(5555);

    bool connected = aDriver->connect_usb();

    if (!connected) {
        std::cerr << "Failed to connect to the stage" << std::endl;
        return 1;
    }
    connected = aDriver->connect_udp();

    if (!connected) {
        std::cerr << "Failed to connect to UDP socket" << std::endl;
        return 1;
    }

    std::cout << "All connected, enter mainloop" << std::endl;
    aDriver->run();


    return 0;
}
