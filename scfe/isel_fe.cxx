#include <stdio.h>
#include <iostream>
#include <stdlib.h>
#include <stdarg.h>
#include <string>
#include <vector>
#include <memory>
#include <chrono>
#include <errno.h>
#include <format>


#include <midas.h>
#include <msystem.h>


#include "isel_fe.h"
#include "pi_generic.h"


/// @todo adjust this to match actual settings in the experiment
#define ISEL_SETTINGS_STRING "\
Host = STRING : [32] 127.0.0.1\n\
Port = INT32 : 4001\n\
Speed = INT32 : 600\n\
Steps per mm = INT32 : 160\n\
Centre X = FLOAT : 0\n\
Centre Y = FLOAT : 0\n\
"

/// @brief device settings stored in ODB
/// @note keep as fixed lenght struct as it maps the ODB layout
struct ISEL_SETTINGS{
    char host[32];
    int port;
    int speed;
    int steps_per_mm;
    float centre_x;
    float centre_y;
};

constexpr size_t kNumChannels = 2;

/// @brief labels for the variables measured (.../Settings/Names)
static const std::string label[kNumChannels] = {
  "X Position",
  "Y Position"
};

/// @brief values obtained from reading the FE
struct ISEL_VALUES {
    float measured[kNumChannels]{};
    float demand[kNumChannels]{};
    float status[kNumChannels]{};
};

/// @brief the internal information to run the FE
struct ISEL_FE_INFO {
    /// @brief The settings related to the FE
    ISEL_SETTINGS settings{};

    /// @brief Last values read
    ISEL_VALUES values{};

    /// @brief DB handle
    HNDLE hKey{};

    /// @brief Address where the device is reached.
    struct sockaddr_in server_addr{};

    /// @brief UDP socket owned by this FE
    int sock{-1};

    /// @brief last time the device was read and values populated
    std::chrono::steady_clock::time_point last_read{};

    ~ISEL_FE_INFO() {
        if (sock != -1) {
            close(sock);
        }
    }
};

INT isel_fe_init(HNDLE hKey, void **pinfo, INT channels, INT(*bd) (INT cmd, ...))
{
    std::cout << "isel init" << std::endl;

    if (channels != kNumChannels) {
        std::cerr << "Requested number of channels (" << channels << ") does not match expected number of " << kNumChannels << " channels" << std::endl;
        /// @todo Is this recoverable? Should we report failure?
    }

    int status, size;
    char str[80];
    HNDLE hDB;

    // allocate and initialise information object
    std::unique_ptr<ISEL_FE_INFO> info = std::make_unique<ISEL_FE_INFO>();
    info->hKey = hKey;

    // Synchronise with ODB
    cm_get_experiment_database(&hDB, NULL);

    status = db_create_record(hDB, hKey, "./", ISEL_SETTINGS_STRING);
    if (status != DB_SUCCESS) {
        std::cerr << "Failed to create record" << std::endl;
        return FE_ERR_ODB;
    }

    size = sizeof(info->settings.host);
    db_get_value(hDB, hKey, "Host", &info->settings.host, &size, TID_STRING, FALSE);
    size = sizeof(int);
    db_get_value(hDB, hKey, "Port", &info->settings.port, &size, TID_INT32, FALSE);
    db_get_value(hDB, hKey, "Speed", &info->settings.speed, &size, TID_INT32, FALSE);
    db_get_value(hDB, hKey, "Steps per mm", &info->settings.steps_per_mm, &size, TID_INT32, FALSE);
    size = sizeof(float);
    db_get_value(hDB, hKey, "Centre X", &info->settings.centre_x, &size, TID_FLOAT32, FALSE);
    db_get_value(hDB, hKey, "Centre Y", &info->settings.centre_y, &size, TID_FLOAT32, FALSE);

    // Setup communication protocol.
    info->sock = socket(AF_INET, SOCK_STREAM, 0);
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

    // Establish TCP connection
    if (connect(info->sock,
            reinterpret_cast<struct sockaddr*>(&info->server_addr),
            sizeof(info->server_addr)) < 0) {
        cm_msg(MERROR, "STAGE FE INIT",
            "cannot connect to %s:%d: errno=%d",
            info->settings.host,
            info->settings.port,
            errno);
        close(info->sock);
        info->sock = -1;
        return FE_ERR_HW;
    }

    // Transfer ownership to MIDAS now that all has succeeded.
    *pinfo = info.release();
    return FE_SUCCESS;
}

INT isel_fe_exit(ISEL_FE_INFO *info)
{
    if (info) {
        delete info;
    }

    std::cout << "isel exit" << std::endl;
    return FE_SUCCESS;
}

/// @brief Communicate with the device to extract all values
/// @param info contains full state of the FE
/// @return FE_SUCCESS on success, FE_ERR_HW otherwise.
INT isel_fe_read(ISEL_FE_INFO* info)
{
    auto now = std::chrono::steady_clock::now();
    if (now - info->last_read < std::chrono::seconds(1)) {
        // read values at most once every second
        return FE_SUCCESS;
    }

    // drain socket first of stray data that may have arrived.
    for (;;) {
        char str[1024]{};
        ssize_t n = recv(info->sock, str, sizeof(str), MSG_DONTWAIT);
        if (n > 0) {
            // discard
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            // socket fully drained
            break;
        }
        if (n == 0) {
            // peer closed connection
            std::cerr << "Peer closed connection" << std::endl;
            for (size_t i = 0; i < kNumChannels; ++i) {
                info->values.measured[i] = (float)ss_nan();
                info->values.demand[i]   = (float)ss_nan();
                info->values.status[i]   = as_float(pi_gen_status_t::kDISCONNECT);
            }
        } else {
            // Socket error?
            std::cerr << "Encountered socket error while draining: errno = " << errno << std::endl;
            for (size_t i = 0; i < kNumChannels; ++i) {
                info->values.measured[i] = (float)ss_nan();
                info->values.demand[i]   = (float)ss_nan();
                info->values.status[i]   = as_float(pi_gen_status_t::kERROR);
            }
        }
        return FE_ERR_HW;
    }

    // send "read" to device
    constexpr char read_cmd[] = "@0p\r";
    send(info->sock, read_cmd, sizeof(read_cmd) - 1 , 0);

    // Wait for data to arrive (no more than 1 second)
    fd_set readfds;
    FD_ZERO(&readfds);
    FD_SET(info->sock, &readfds);
    timeval timeout{1, 0};
    select(FD_SETSIZE, &readfds, NULL, NULL, &timeout);
    if (FD_ISSET(info->sock, &readfds)) {
        // We did receive new data
        char str[1024]{};
        size_t nBytes = recv(info->sock, str, sizeof(str), 0);
        // format of str should be
        // 0XXXXXXYYYYYYZZZZZZ
        if (nBytes == 19 && str[0] == '0') {
            auto convert = [](const char* c) {
                int val = std::stoi(std::string(c, c + 6), nullptr, 16);
                // dealing with 24 bit int, make it wrap around neatly
                // from -8388608 -> 8388607 rather than 0 -> 16777215
                if (val & (1 << 23)) val -= (1 << 24);
                return static_cast<float>(val);
            };
            float xval = convert(str + 1);
            float yval = convert(str + 7);
            info->values.measured[0] = info->settings.centre_x + xval / info->settings.steps_per_mm;
            info->values.measured[1] = info->settings.centre_y + yval / info->settings.steps_per_mm;
            /// @todo: Deal with proper status updates here
            info->values.status[0]   = as_float(pi_gen_status_t::kOK);
            info->values.status[1]   = as_float(pi_gen_status_t::kOK);
            info->last_read = now;
        }
    }

    if (now - info->last_read >= std::chrono::seconds(60)) {
        for (size_t i = 0; i < kNumChannels; ++i) {
            info->values.measured[i] = (float)ss_nan();
            info->values.status[i]   = as_float(pi_gen_status_t::kDISCONNECT);
        }
    }
    return FE_SUCCESS;
}

INT isel_fe_get(ISEL_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
    INT status = isel_fe_read(info);
    if (channel < 0 || channel >= kNumChannels) {
        *pvalue = (float)ss_nan();
        return status;
    }

    switch (cmd) {
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

INT isel_fe_set(ISEL_FE_INFO *info, INT channel, float value)
{
    if (channel >= 0 && channel < kNumChannels) {
        info->values.demand[channel] = value;
        long xreq = (info->values.demand[0] + info->settings.centre_x) * info->settings.steps_per_mm;
        long yreq = (info->values.demand[1] + info->settings.centre_y) * info->settings.steps_per_mm;
        std::string request = "@0M " + std::to_string(xreq) + ","
                                     + std::to_string(info->settings.speed) + ","
                                     + std::to_string(yreq) + ","
                                     + std::to_string(info->settings.speed) + "\r";
        send(info->sock, request.c_str(), request.size(), 0);
    }

    return FE_SUCCESS;
}

INT isel_fe(INT cmd, ...)
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
            status = isel_fe_init(hKey, (void**)info, channel, bd);
            break;

        case CMD_EXIT:
            info = va_arg(argptr, void *);
            status = isel_fe_exit((ISEL_FE_INFO*)info);
            break;

        case CMD_GET:
        case CMD_GET_DEMAND:
        case CMD_GET_STATUS:
            info = va_arg(argptr, void *);
            channel = va_arg(argptr, INT);
            pvalue = va_arg(argptr, float *);
            status = isel_fe_get((ISEL_FE_INFO*)info, channel, pvalue, cmd);
            break;

        case CMD_SET:
            info = va_arg(argptr, void*);
            channel = va_arg(argptr, INT);
            value = (float) va_arg(argptr, double);
            status = isel_fe_set((ISEL_FE_INFO*)info, channel, value);
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
            // This is not an error but a dedicated choice that this FE does not provide
            // these fields.
            break;

    }

    va_end(argptr);
    return status;
}