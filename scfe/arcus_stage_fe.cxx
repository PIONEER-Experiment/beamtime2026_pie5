#include <stdio.h>
#include <iostream>
#include <stdlib.h>
#include <stdarg.h>
#include <string>
#include <vector>
#include <memory>
#include <chrono>

#include <midas.h>
#include <msystem.h>

#include "arcus_stage_fe.h"
#include "pi_generic.h"

#define ARCUS_STAGE_SETTINGS_STRING "\
Host = STRING : [32] 127.0.0.1\n\
Port = INT32 : 5555\n\
Steps per mm = INT32 : 800\n\
"

typedef struct {
    char host[32];
    int port;
    int steps_per_mm;
} ARCUS_STAGE_SETTINGS;

constexpr size_t kNumChannels = 1;

static const std::string label[kNumChannels] = {
  "X Position"
};

struct ARCUS_STAGE_VALUES {
    float measured[kNumChannels]{};
    float demand[kNumChannels]{};
    float status[kNumChannels]{};
};
struct ARCUS_STAGE_FE_INFO {
    ARCUS_STAGE_SETTINGS settings{};
    ARCUS_STAGE_VALUES values{};
    HNDLE hKey{};
    struct sockaddr_in server_addr{};
    int sock{-1};
    std::chrono::steady_clock::time_point last_read{};

    ~ARCUS_STAGE_FE_INFO() {
        if (sock != -1) {
            close(sock);
        }
    }
};

INT arcus_stage_fe_init(HNDLE hKey, void **pinfo, INT channels, INT(*bd) (INT cmd, ...))
{
    std::cout << "arcus_stage init" << std::endl;

    int status, size;
    char str[80];
    HNDLE hDB;
    std::unique_ptr<ARCUS_STAGE_FE_INFO> info = std::make_unique<ARCUS_STAGE_FE_INFO>();

    info->hKey = hKey;
    cm_get_experiment_database(&hDB, NULL);

    status = db_create_record(hDB, hKey, "./", ARCUS_STAGE_SETTINGS_STRING);
    if (status != DB_SUCCESS) {
        std::cerr << "Failed to create record" << std::endl;
        return FE_ERR_ODB;
    }

    size = sizeof(info->settings.host);
    db_get_value(hDB, hKey, "Host", &info->settings.host, &size, TID_STRING, FALSE);
    size = sizeof(int);
    db_get_value(hDB, hKey, "Port", &info->settings.port, &size, TID_INT32, FALSE);
    db_get_value(hDB, hKey, "Steps per mm", &info->settings.steps_per_mm, &size, TID_INT32, FALSE);

    info->sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (info->sock < 0) {
        return FE_ERR_HW;
    }
    auto phe = gethostbyname(info->settings.host);
    if (not phe) {
        cm_msg(MERROR, "STAGE FE INIT", "cannot find host %s", info->settings.host);
        return FE_ERR_HW;
    }

    memset(&info->server_addr, 0, sizeof(info->server_addr));
    info->server_addr.sin_family = AF_INET;
    info->server_addr.sin_port = htons(info->settings.port);
    memcpy((char*) &(info->server_addr.sin_addr), phe->h_addr, phe->h_length);
    
    *pinfo = info.release();
    return FE_SUCCESS;
}

INT arcus_stage_fe_exit(ARCUS_STAGE_FE_INFO *info)
{
    if (info) {
        delete info;
    }

    std::cout << "arcus_stage exit" << std::endl;
    return FE_SUCCESS;
}

INT arcus_read(ARCUS_STAGE_FE_INFO* info)
{
    auto now = std::chrono::steady_clock::now();
    if (now - info->last_read < std::chrono::seconds(1)) {
        // read values at most once every second
        return FE_SUCCESS;
    }

    // drain socket first of stray data that may have arrived.
    for (;;) {
        char str[1024]{};
        ssize_t n = recv(info->sock, str, sizeof(str) - 1, MSG_DONTWAIT);
        if (n > 0) {
            std::cout << "received " << str << "while draining (n = " << n << ")" << std::endl;
            // discard a DGRAM
            continue;
        }
        if (n == 0 || errno == EAGAIN || errno == EWOULDBLOCK) {
            // socket fully drained
            break;
        }
        // Socket error?
        std::cerr << "Encountered socket error while draining: errno = " << errno << std::endl;
        for (size_t i = 0; i < kNumChannels; ++i) {
            info->values.measured[i] = (float)ss_nan();
            info->values.demand[i]   = (float)ss_nan();
            info->values.status[i]   = as_float(pi_gen_status_t::kERROR);
        }
        return FE_ERR_HW;
    }

    // send "read" to device
    sendto(info->sock, "read\n", 5, 0, (struct sockaddr *) &info->server_addr, sizeof(info->server_addr));

    // Wait for data to arrive (no more than 1 second)
    fd_set readfds;
    FD_ZERO(&readfds);
    FD_SET(info->sock, &readfds);
    timeval timeout{1, 0};
    select(FD_SETSIZE, &readfds, NULL, NULL, &timeout);

    if (FD_ISSET(info->sock, &readfds)) {
        char str[1024]{};
        recv(info->sock, str, sizeof(str), 0);
        int i = 0;
        constexpr char sep[] = " ";
        char *p = strtok(str, sep);
        char *parts[3];

        while (p && i < 3) {
            parts[i++] = p;
            p = strtok(NULL, sep);
        }

        if (i == 3) {
            // We might not have received 3 parts (measured/demand/status)
            // That's ok, the gnome in the cable might be overworked and
            // delivered the wrong package. Let's hope he does better
            // on the next read.
            info->values.measured[0] = atof(parts[0]) / info->settings.steps_per_mm;
            info->values.demand[0]   = atof(parts[1]) / info->settings.steps_per_mm;
            int status = atoi(parts[2]);
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
                info->values.status[0] = as_float(pi_gen_status_t::kOK);
            } else if (status & 0x07) {
                info->values.status[0] = as_float(pi_gen_status_t::kTRANSITION);
            } else if (status & 0xf0) {
                info->values.status[0] = as_float(pi_gen_status_t::kERROR);
            }
            info->last_read = now;
        }
    }

    if (now - info->last_read >= std::chrono::seconds(60)) {
        for (size_t i = 0; i < kNumChannels; ++i) {
            info->values.measured[i] = (float)ss_nan();
            info->values.demand[i]   = (float)ss_nan();
            info->values.status[i]   = as_float(pi_gen_status_t::kDISCONNECT);
        }
    }
    return FE_SUCCESS;
}

INT arcus_stage_fe_get(ARCUS_STAGE_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
    INT status = arcus_read(info);
    if (channel < 0 || channel >= kNumChannels) {
        *pvalue = (float)ss_nan();
        return status;
    }

    switch (cmd)
    {
        case CMD_GET:
            *pvalue = info->values.measured[channel];
            break;
        case CMD_GET_DEMAND:
            *pvalue = info->values.demand[channel];
            break;
        case CMD_GET_STATUS:
            *pvalue = info->values.status[channel];
            break;
        default:
            *pvalue = (float)ss_nan();
    }

    return status;
}

INT arcus_stage_fe_set(ARCUS_STAGE_FE_INFO *info, INT channel, float value)
{
    std::cout << "arcus_stage_fe_set called with channel " << channel << " and value " << value << std::endl;
    if (channel >= 0 && channel < kNumChannels) {
        std::string request = "SET " + std::to_string(channel) + " " + std::to_string(int(value * info->settings.steps_per_mm)) + "\n";
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
        case CMD_GET_DEMAND:
        case CMD_GET_STATUS:
            // Get measured value
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            pvalue = va_arg(argptr, float *);
            status = arcus_stage_fe_get((ARCUS_STAGE_FE_INFO*)info, channel, pvalue, cmd);
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
            if (channel < kNumChannels)
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