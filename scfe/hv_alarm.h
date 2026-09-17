#ifndef HV_ALARM_H
#define HV_ALARM_H

#include <midas.h>

/// @brief Frontend-side alarms and polarity publishing for a cd_hv equipment.
///
/// Nothing in MIDAS evaluates AT_INTERNAL alarms for us, so the frontend has to
/// compare the values cd_hv put in ODB against per-channel thresholds itself
/// and call al_trigger_alarm()/al_reset_alarm(). All three functions run on the
/// mfe **main thread only** (frontend_init / frontend_loop / frontend_exit) and
/// never touch the device: they only read ODB plus the driver's cached polarity
/// through caen_hv_polarity(), which is safe while the poll thread runs.
///
/// Two global gates silence all of this, independently of Settings/Alarm/Enabled:
/// @c /Runinfo/Online @c Mode and @c /Alarms/Alarm @c system @c active
/// (alarm.cxx:296-300, 350-356).

/// @brief Create the ODB records and remember the equipment.
///
/// Creates @c /Equipment/<eq>/Settings/Alarm/* and makes sure the alarm class
/// @c /Alarms/Classes/HV @c Alarm exists with the complete key set that
/// al_trigger_class() reads (alarm.cxx:449-505).
///
/// @param equipment_name equipment name as in the EQUIPMENT table, e.g. "CaenHV"
/// @param n_channels     number of channels, as in the DEVICE_DRIVER table
/// @return always CM_SUCCESS: a failure to create the records is reported with
///         cm_msg(MERROR) and disables the alarm checks, it must never stop the
///         frontend from monitoring the supply.
INT hv_alarm_init(const char *equipment_name, int n_channels);

/// @brief One alarm check. Call from frontend_loop() as often as you like:
///        the body runs at most once per second (steady_clock).
void hv_alarm_loop();

/// @brief Release ODB handles. Does not reset pending alarms (an alarm must
///        survive a frontend restart).
void hv_alarm_exit();

#endif // HV_ALARM_H
