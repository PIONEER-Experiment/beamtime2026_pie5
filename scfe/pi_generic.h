
#ifndef pi_generic_h
#define pi_generic_h
#include <midas.h>

#include <unordered_map>


// Command int to string map, extracted from midas.h
// For easier understanding of debug messages
inline std::unordered_map<int, std::string> commands = {
        {1,  "CMD_INIT"},
        {2,  "CMD_EXIT"},
        {3,  "CMD_START"},
        {4,  "CMD_STOP"},
        {5,  "CMD_IDLE"},
        {6,  "CMD_GET_THRESHOLD"},
        {7,  "CMD_GET_THRESHOLD_CURRENT"},
        {8,  "CMD_GET_THRESHOLD_ZERO"},
        {9,  "CMD_SET_LABEL"},
        {10, "CMD_GET_LABEL"},
        {11, "CMD_OPEN"},
        {12, "CMD_CLOSE"},
        {13, "CMD_SET"},
        {14, "CMD_SET_VOLTAGE_LIMIT"},
        {15, "CMD_SET_CURRENT_LIMIT"},
        {16, "CMD_SET_RAMPUP"},
        {17, "CMD_SET_RAMPDOWN"},
        {18, "CMD_SET_TRIP_TIME"},
        {19, "CMD_SET_CHSTATE"},
        {20, "CMD_GET"},
        {21, "CMD_GET_CURRENT"},
        {22, "CMD_GET_TRIP"},
        {23, "CMD_GET_STATUS"},
        {24, "CMD_GET_TEMPERATURE"},
        {25, "CMD_GET_DEMAND"},
        {26, "CMD_GET_VOLTAGE_LIMIT"},
        {28, "CMD_GET_CURRENT_LIMIT"},
        {29, "CMD_GET_RAMPUP"},
        {30, "CMD_GET_RAMPDOWN"},
        {31, "CMD_GET_TRIP_TIME"},
        {32, "CMD_GET_CHSTATE"},
        {33, "CMD_GET_CRATEMAP"},
        {34, "CMD_GET_DEMAND_DIRECT"},
    };

// Possible status the pi_generic class driver supports while displaying the FE status
// Note: The severety of the status has to be increasing - the frontend status will be
// displayed as the largest status encountered in any of the associated channels.
enum class pi_gen_status_t : uint32_t {
    kOK         = 0,
    kTRANSITION = 20,
    kTIMEOUT    = 40,
    kDISCONNECT = 60,
    kERROR      = 80,
    kMALFORM    = 81,
};

inline float as_float(pi_gen_status_t t) {return static_cast<float>(t); }
pi_gen_status_t status_from_float(float);

// Forward declarations
INT cd_pi_gen_read(char *pevent, int offset);
INT cd_pi_gen(INT cmd, EQUIPMENT * pequipment);
#endif


