#include <stdio.h>
#include <iostream>
#include <stdlib.h>
#include <stdarg.h>
#include <string>
#include <vector>
#include <midas.h>
#include <msystem.h>

#include "arcus_stage_fe.h"
#include "pi_generic.h"

#define ARCUS_STAGE_SETTINGS_STRING "\
Host = STRING : [32] 127.0.0.1\n\
Port = INT32 : 5555\n\
"

typedef struct {
    char host[32];
    int port;
} ARCUS_STAGE_SETTINGS;

static const std::string label[] = {
  "X Position"
};

typedef struct {
    ARCUS_STAGE_SETTINGS stage_settings;
    HNDLE hKey;
    struct sockaddr_in server_addr;
    int sock;
} ARCUS_STAGE_FE_INFO;

INT arcus_stage_fe_init(HNDLE hKey, void **pinfo, INT channels, INT(*bd) (INT cmd, ...))
{
    std::cout << "arcus_stage init" << std::endl;

    int status, size;
    char str[80];
    HNDLE hDB;
    ARCUS_STAGE_FE_INFO *info = new ARCUS_STAGE_FE_INFO;

    struct sockaddr_in server_addr;
    struct hostent *phe;

    *pinfo = info;

    info->hKey = hKey;
    cm_get_experiment_database(&hDB, NULL);

    status = db_create_record(hDB, hKey, "./", ARCUS_STAGE_SETTINGS_STRING);
    if (status != DB_SUCCESS) {
        std::cerr << "Failed to create record" << std::endl;
        return FE_ERR_ODB;
    }

    size = sizeof(info->stage_settings.host);
    db_get_value(hDB, hKey, "Host", &info->stage_settings.host, &size, TID_STRING, FALSE);
    size = sizeof(info->stage_settings.port);
    db_get_value(hDB, hKey, "Port", &info->stage_settings.port, &size, TID_INT, FALSE);

    std::cout << "host " << info->stage_settings.host << " port " << info->stage_settings.port << std::endl;

    info->sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (info->sock < 0) {
        return FE_ERR_HW;
    }
    phe = gethostbyname(info->stage_settings.host);
    if (not phe) {
        cm_msg(MERROR, "STAGE FE INIT", "cannot find host %s", info->stage_settings.host);
        return FE_ERR_HW;
    }

    memset(&info->server_addr, 0, sizeof(info->server_addr));
    info->server_addr.sin_family = AF_INET;
    info->server_addr.sin_port = htons(info->stage_settings.port);
    memcpy((char*) &(info->server_addr.sin_addr), phe->h_addr, phe->h_length);
    
    sendto(info->sock, "read\n", 5, 0, (struct sockaddr *) &info->server_addr, sizeof(info->server_addr));

    return FE_SUCCESS;
}

INT arcus_stage_fe_exit(ARCUS_STAGE_FE_INFO *info)
{
    if (info) {
        close(info->sock);
        free(info);
    }

    std::cout << "arcus_stage exit" << std::endl;
    return FE_SUCCESS;
}

INT arcus_stage_fe_get(ARCUS_STAGE_FE_INFO *info, INT channel, float *pvalue)
{
    // values = {requested, measured, status}
    static float values[3] = {0, 0, 0};
    char str[1024];
    unsigned int size;
    fd_set readfds;
    struct timeval timeout;

    if (channel == 0) {
        sendto(info->sock, "read\n", 5, 0, (struct sockaddr *) &info->server_addr, sizeof(info->server_addr));

        FD_ZERO(&readfds);
        FD_SET(info->sock, &readfds);

        timeout.tv_sec = 1;
        timeout.tv_usec = 0;

        select(FD_SETSIZE, &readfds, NULL, NULL, &timeout);

        if (FD_ISSET(info->sock, &readfds)) {
            memset(str, 0, sizeof(str));
            recv(info->sock, str, sizeof(str), 0);
            int i = 0;
            char *p = strtok(str, " ");
            while (p && i < 3) {
                if (i < 2) {
                    values[i++] = atof(p);
                } else {
                    int status = atoi(p);
                    // Status bits are as follows according to manual:
                    // 0 - motor running at constant speed
                    // 1 - motor accelerating
                    // 2 - motor decelerating
                    // 3 - home input switch status (we don't care here)
                    // 4 - minus limit switch (shouldn't be there)
                    // 5 - plus limit switch (shouldn't be there either)
                    // 6 - minus limit error
                    // 7 - plus limit error
                    // 8 - Latch input status (we don't care)
                    // 9 - Z-index status (we don't care)
                    // 10 - TOC time-out status (we don't care)
                    if ((status & 0xf7) == 0) {
                        values[i++] = as_float(pi_gen_status_t::kOK);
                    } else if (status & 0x07) {
                        values[i++] = as_float(pi_gen_status_t::kTRANSITION);
                    } else if (status & 0xf0) {
                        values[i++] = as_float(pi_gen_status_t::kERROR);
                    }
                }
                p = strtok(NULL, " ");
            }
        }
    }

    if (channel < 3) {
        *pvalue = values[channel];
    } else {
        *pvalue = (float) ss_nan();
    }

    return FE_SUCCESS;
}

INT arcus_stage_fe_set(ARCUS_STAGE_FE_INFO *info, INT channel, float value)
{
    std::cout << "arcus_stage_fe_set called with channel " << channel << " and value " << value << std::endl;
    if (channel == 0) {
        std::string request = "SET " + std::to_string(channel) + " " + std::to_string(value) + "\n";
        std::cout << request;
        sendto(info->sock, request.c_str(), request.size(), 0, (struct sockaddr *) &info->server_addr, sizeof(info->server_addr));
    }

    return FE_SUCCESS;
}

INT arcus_stage_fe(INT cmd, ...)
{
    va_list argptr;
    HNDLE hKey;
    INT channel, status;
    DWORD flags;
    float *pvalue;
    float value;
    void *info;
    INT(*bd)(INT cmd, ...);
    char *name;

    va_start(argptr, cmd);
    status = FE_SUCCESS;

    switch(cmd) {
        case CMD_INIT:
            hKey = va_arg(argptr, HNDLE);
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            flags = va_arg(argptr, DWORD);
            bd = va_arg(argptr, INT(*)(INT, ...));
            status = arcus_stage_fe_init(hKey, (void**)info, channel, bd);
            break;

        case CMD_EXIT:
            info = va_arg(argptr, void *);
            status = arcus_stage_fe_exit((ARCUS_STAGE_FE_INFO*)info);
            break;

        case CMD_GET:
            // Get measured value
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            pvalue = va_arg(argptr, float *);
            status = arcus_stage_fe_get((ARCUS_STAGE_FE_INFO*)info, 1, pvalue);
            break;

        case CMD_GET_DEMAND:
            // Get requested value
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            pvalue = va_arg(argptr, float *);
            status = arcus_stage_fe_get((ARCUS_STAGE_FE_INFO*)info, 0, pvalue);
            break;

        case CMD_GET_STATUS:
            // Get status of stage
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            pvalue = va_arg(argptr, float *);
            status = arcus_stage_fe_get((ARCUS_STAGE_FE_INFO*)info, 2, pvalue);
            break;

        case CMD_SET:
            info = va_arg(argptr, void*);
            channel = va_arg(argptr, INT);
            value = (float) va_arg(argptr, double);
            status = arcus_stage_fe_set((ARCUS_STAGE_FE_INFO*)info, channel, value);
            break;

        case CMD_GET_LABEL:
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            name = va_arg(argptr, char *);
            if (channel < 3)
                strcpy(name, label[channel].c_str());
            else
                name[0] = 0;
            status = FE_SUCCESS;
            break;

        default:
            // We are ignoring a bunch of possible commands, such as read temperature etc.
            // This is not an error but a dedicated choice that the stage does not provide
            // these fields.
            break;

    }

    va_end(argptr);
    return status;
}