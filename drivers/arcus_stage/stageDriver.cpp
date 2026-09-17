/*
* File:   stageDriver.cpp
* Author: Patrick Schwendimann
* Driver: Arcus Perfomax Stage
*
* The main driver class that maintaines the UDP server
* as well as the USB socket connecting to the stage itself.
*
*/

#include "stageDriver.hh"

#include <iostream>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <thread>
#include <cstring>

bool isIn(char c, std::string str) {
   for (unsigned int i = 0; i < str.size(); ++i) {
      if (c == str[i]) return true;
   }
   return false;
}

bool isEqual(std::string str1, std::string str2) {
   if (str1.size() != str2.size()) {
      return false;
   }
   for (unsigned int i = 0; i < str1.size(); ++i) {
      if (tolower(str1[i]) != tolower(str2[i])) {
         return false;
      }
   }
   return true;
}

StageDriver::StageDriver() 
: fPort(5555)
, fSocket(-1)
, fRequestedX(0)
, fCurrentX(0)
, fStatus(-1)
, fMaxX(200000)
, fMinX(0)
, fEnabled(true)
, fTheHandle(nullptr)
{
    
}

StageDriver::~StageDriver()
{
    close(fSocket);
}

bool StageDriver::connect_udp()
{
    //create socket
    struct sockaddr_in sAddress;
    memset(&sAddress, 0, sizeof(sAddress));

    sAddress.sin_family = AF_INET;
    sAddress.sin_port = htons(fPort);
    sAddress.sin_addr.s_addr = htonl(INADDR_ANY);

    fSocket = socket(PF_INET, SOCK_DGRAM, 0);

    if (fSocket < 0) {
        std::cerr << "Failed to create UDP socket" << std::endl;
        return false;
    }

    if ((bind(fSocket, (struct sockaddr*)&sAddress, sizeof(sAddress))) < 0) {
        std::cerr << "Failed to bind UDP socket" << std::endl;
        return false;
    }

    return true;    
}

bool StageDriver::usb_command(const char* cmd, char* retval)
{
    static char paddedCMD[64];
    memset(paddedCMD, 0, 64);
    strcpy(paddedCMD, cmd);
	if(!fnPerformaxComSendRecv(fTheHandle, paddedCMD, 64,64, retval))
	{
		printf("Could not send\n");
		return 0;
	}
	return 1;
}

bool StageDriver::connect_usb()
{
    
	char 		lpDeviceString[PERFORMAX_MAX_DEVICE_STRLEN];
	char		out[64];
	AR_DWORD	num;
	int i;
    bool status;
	
	memset(out,0,64);

	//acquire information
	
	if(!fnPerformaxComGetNumDevices(&num))
	{
		printf("error in fnPerformaxComGetNumDevices\n");
		return false;
	}
	if(num<1)
	{
		printf( "No motor found\n");
		return false;
	} else {
        std::cout << "Num of devices: " << num << std::endl;
    }

	if( !fnPerformaxComGetProductString(0, lpDeviceString, PERFORMAX_RETURN_SERIAL_NUMBER) ||
		!fnPerformaxComGetProductString(0, lpDeviceString, PERFORMAX_RETURN_DESCRIPTION) )
	{
		printf("error acquiring product string\n");
		return false;
	}
	
	printf("device description: %s\n", lpDeviceString);
	
	//setup the connection
	
	if(!fnPerformaxComOpen(0,&fTheHandle))
	{
		printf( "Error opening device\n");
		return false;
	}
	
	if(!fnPerformaxComSetTimeouts(5000,5000))
	{
		printf("Error setting timeouts\n");
		return false;
	}
	if(!fnPerformaxComFlush(fTheHandle))
	{
		printf("Error flushing the coms\n");
		return false;
	}
	
	// setup the device
	
    usb_command("EO=2", out);
    usb_command("LSPD=300", out);
	usb_command("HSPD=3000", out);

    usb_command("POL=66", out);
    usb_command("POLX=66", out);
    usb_command("POLY=66", out);
    usb_command("ACC=300", out);

    usb_command("ID", out);
	printf("Arcus Product: %s\n",out);

    usb_command("DN", out);
	printf("Device Number: %s\n",out);

    usb_command("ABS", out);

	usb_command("MSTY", out);
	printf("MSTY: %s\n", out);
	
	usb_command("LCAY=1000", out);

    std::cout << "Doing calibration movement to limit switch" << std::endl;
	usb_command("LY-", out);
	do {
		usleep(1e5);
		usb_command("MSTY", out);
	} while (out[0] != '0');

	usb_command("PY", out);
	printf("LY- PY: %s\n",out);

    std::cout << "Stage ready" << std::endl;
    return true;
}

void StageDriver::ReadPacket(char* rawBuffer, int maxLen) {
   fBuffer.clear();

   std::string aString;
   for (char *c = rawBuffer; c < rawBuffer + maxLen; ++c) {
      if (isIn(*c, std::string(" ,;\n\r\0", 6))) {
         if (aString.size() > 0) {
            fBuffer.push_back(aString);
            aString.clear();
         }
      } else {
         aString.push_back(*c);
      }
      if (*c == 0) {
         // end of string character read.
         break;
      }
   }
}

void StageDriver::run()
{
    std::thread usb_thread(&StageDriver::run_usb, this);
    std::thread udp_thread(&StageDriver::run_udp, this);

    usb_thread.join();
    udp_thread.join();
}



void StageDriver::run_usb()
{
    // ready to start mainloop, wait for changes through UDB thread and so on
    static char buf[64];
    std::string cmd;

    while (true) {
        // read stuff from stage
        usb_command("PY", buf);
        fCurrentX = atof(buf);
        usb_command("MSTY", buf);
        fStatus = atoi(buf);

        // acion goes here:

        if (fEnabled && abs(fCurrentX - fRequestedX) > 1 && fStatus == 0) {
            cmd = "Y" + std::to_string(fRequestedX);
            usb_command(cmd.c_str(), buf);
        }

        // sleep
        usleep(1e6);
    }
}

void StageDriver::run_udp()
{
    struct sockaddr_in cAddress;
    socklen_t cAddrLen = sizeof(cAddress);
    std::string reply;
    int channel = -1;
    double newVal = 0;

    char rawBuffer [1024] = {0};
    while (true) {
        // read raw data
        memset(rawBuffer, 0, sizeof(rawBuffer));
        recvfrom(fSocket, rawBuffer, sizeof(rawBuffer), 0, (struct sockaddr*) &cAddress, &cAddrLen);

        // Update fBuffer based on raw data
        ReadPacket(rawBuffer, sizeof(rawBuffer));
        for (unsigned int index = 0; index < fBuffer.size(); ++index) {
            // Prepare a reply
            reply.clear();

            if (isEqual(fBuffer[index], "READ")) {
                reply = std::to_string(fCurrentX) + " ";
                reply += std::to_string(fRequestedX) + " ";
                reply += std::to_string(fStatus) + "\n";
            } else if (isEqual(fBuffer[index], "SET")) {
                try {
                    channel = std::stoi(fBuffer[index + 1]);
                    newVal  = std::stod(fBuffer[index + 2]);
                    if (channel == 0) {
                        std::cout << "New Requested value: " << newVal << std::endl;
                        if (newVal < fMinX) {
                            newVal = fMinX;
                        } else if (newVal > fMaxX) {
                            newVal = fMaxX;
                        }
                        fRequestedX = newVal;
                        reply = "done\n";
                    } else {
                        reply = "mimimi\n";
                    }
                    ++index;
                    ++index;
                }
                catch (std::invalid_argument&) {
                    reply = fBuffer[index] + " " + fBuffer[index + 1] + " " + fBuffer[index + 2] + ": ";
                    reply += "No conversion to number possible";

                }
            } else if (isEqual(fBuffer[index], "CMD")) {
                char cmd[64];
                char out[64];
                strcpy(cmd, fBuffer[index + 1].c_str());
                usb_command(cmd, out);
                reply = fBuffer[index + 1] + " > " + out + "\n";
                ++index;
            } else if (isEqual(fBuffer[index], "MAX")) {
                newVal = std::stod(fBuffer[index + 1]);
                fMaxX = newVal;
                reply = "done\n";
                ++index;
            } else if (isEqual(fBuffer[index], "MIN")) {
                newVal = std::stod(fBuffer[index + 1]);
                fMinX = newVal;
                reply = "done\n";
                ++index;
            } else if (isEqual(fBuffer[index], "ENABLE")) {
                fEnabled = true;
                reply = "done\n";
            } else if (isEqual(fBuffer[index], "DISABLE")) {
                fEnabled = false;
                reply = "done\n";
            } else {
                std::cerr << "bad request " << fBuffer[index] << std::endl;
                reply = "bad request\n";
            }
            sendto(fSocket, reply.c_str(), reply.size(), 0, (struct sockaddr*) &cAddress, sizeof(cAddress));
        }
    }
}
