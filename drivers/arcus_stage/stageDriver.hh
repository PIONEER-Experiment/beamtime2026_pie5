/*
* File:   stageDriver.hh
* Author: Patrick Schwendimann
* Driver: Arcus Perfomax Stage
*
* The main driver class that maintaines the UDP server
* as well as the USB socket connecting to the stage itself.
*
*/

#ifndef StageDriver_h
#define StageDriver_h 1

#include <vector>
#include <string>

#include "ArcusPerformaxDriver.h"

class StageDriver {
    public:
        StageDriver();
        ~StageDriver();

        inline void SetPort(int port) {fPort = port; };

        bool connect_udp();
        bool connect_usb();
        void run(); // Mainloop

    protected:
        void ReadPacket(char*, int);
        void run_udp();
        void run_usb();

        bool usb_command(const char*, char*);

    private:
        int fPort;
        int fSocket;
        std::vector<std::string> fBuffer;


        float fRequestedX;
        float fCurrentX;
        int fStatus;

        float fMaxX;
        float fMinX;
        bool fEnabled;

        AR_HANDLE fTheHandle; //usb handle


};


#endif
