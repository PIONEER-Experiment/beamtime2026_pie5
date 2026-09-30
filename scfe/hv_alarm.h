#ifndef HV_ALARM_H
#define HV_ALARM_H

#include <string>

#include <midas.h>

/// @brief Frontend-side alarms and polarity publishing for cd_hv equipments.
///
/// Nothing in MIDAS evaluates AT_INTERNAL alarms for us, so the frontend has to
/// compare the values cd_hv put in ODB against per-channel thresholds itself
/// and call al_trigger_alarm()/al_reset_alarm(). All three functions run on the
/// mfe **main thread only** (frontend_init / frontend_loop / frontend_exit) and
/// never touch the device: they only read ODB plus the driver's cached polarity
/// through hv_alarm_driver_t::polarity, which must be safe while the poll
/// thread runs.
///
/// One instance per equipment: call hv_alarm_init() once for each cd_hv
/// equipment, each with its own driver descriptor. Everything that depends on
/// the device (status bits, their names, defaults) comes from that descriptor,
/// so this module includes no driver header.
///
/// Two global gates silence all of this, independently of Settings/Alarm/Enabled:
/// @c /Runinfo/Online @c Mode and @c /Alarms/Alarm @c system @c active
/// (alarm.cxx:296-300, 350-356).

/// @brief What the generic alarm code needs to know about one device driver.
///
/// Plain data plus function pointers, so a driver can describe itself with a
/// constant (captureless lambdas convert to the pointers). hv_alarm_init()
/// copies it, the caller's object need not outlive the call. The strings must
/// be string literals or otherwise live for the whole frontend run.
struct hv_alarm_driver_t {
   /// device name for the Comm alarm text "no readings from <label> for N s",
   /// e.g. "CAEN HV"
   const char *device_label;

   /// appended verbatim to the "ChState ON but board says off (STAT ...)"
   /// message, e.g. " - check front switch KILL/OFF/ON"; "" for nothing
   const char *off_hint;

   /// driver-private "status could not be read" flag in Variables/ChStatus
   /// (not a device condition: masked out of the alarm comparison, drives the
   /// Comm alarm)
   DWORD stale_bit;

   /// default of Settings/Alarm/Status Mask: status bits that raise an alarm
   DWORD default_status_mask;

   /// default of Settings/Alarm/Voltage Max[i] [V]
   float default_voltage_max;

   /// default of Settings/Alarm/Current Max[i] [uA]
   float default_current_max;

   /// TRUE when the status word says the output is on
   bool (*is_on)(DWORD stat);

   /// decode a status word for a message, e.g. "ON|OVC"; must return "none"
   /// when no known bit is set
   std::string (*stat_text)(DWORD stat);

   /// polarity of channel @p ch from the driver's cache: +1, -1, or 0 if
   /// unknown; @p dd_info is DEVICE_DRIVER::dd_info. nullptr: no
   /// Variables/Polarity is published.
   int (*polarity)(void *dd_info, int ch);

   /// TRUE: create Settings/Alarm/Deviation Max[i] and Deviation Hold s, and
   /// alarm when |Variables/Demand - Variables/Measured| has stayed above
   /// Deviation Max for Deviation Hold s without a break, while ChState is 1,
   /// the status word is known, not stale, is_on() and not is_ramping(). Before
   /// the alarm is raised, any second in which one of these conditions fails
   /// restarts the hold timer. Once raised, the hold no longer applies: every
   /// qualifying second keeps the alarm up, and it clears the usual way, after
   /// Clear After s without one (so a single noisy reading cannot make it
   /// flap). A "deviation" alarm adopted from a previous instance counts as
   /// raised. FALSE: no such keys are created and nothing is checked.
   bool deviation_check;

   /// default of Settings/Alarm/Deviation Max[i] [V]; unused unless
   /// deviation_check
   float default_deviation_max;

   /// TRUE while the status word says the output is ramping; required when
   /// deviation_check, may be nullptr otherwise
   bool (*is_ramping)(DWORD stat);

   /// default of Settings/Alarm/Deviation Hold s [s]; unused unless
   /// deviation_check. Exists because a unit can report "on, not ramping"
   /// while its read-back still trails the set point (the NHQ lags by up to
   /// ~25 V for a while after a ramp).
   INT default_deviation_hold_s;

   /// TRUE: alarm "on but ChState OFF" when Variables/ChState is 0 but the
   /// status word (known, not stale) says is_on() and not is_ramping(), for
   /// 5 s in a row - voltage on the output that MIDAS believes is off (set
   /// from the front panel or the CLI, or a switch-off that did not take).
   /// Ramping is excluded so a normal ramp-down after OFF is quiet; requires
   /// is_ramping. Latched like the deviation check. No ODB key. FALSE: not
   /// checked.
   bool on_while_off_check;

   /// optional: the status text for the "ChState ON but board says off
   /// (STAT ...)" message, where a device can say more than stat_text (the
   /// iseg reports "ON" at a 0 V set point, which reads as a contradiction
   /// there). nullptr: stat_text is used.
   std::string (*off_stat_text)(DWORD stat);
};

/// @brief Create the ODB records for one equipment and register it.
///
/// Creates @c /Equipment/<eq>/Settings/Alarm/* and makes sure the alarm class
/// @c /Alarms/Classes/HV @c Alarm exists with the complete key set that
/// al_trigger_class() reads (alarm.cxx:449-505). Alarms are called
/// @c "<eq> Ch<i>" and @c "<eq> Comm". Calling it again for an equipment that
/// is already registered replaces that instance (names compared
/// case-insensitively, as MIDAS does); a call with bad arguments disables it.
///
/// @param equipment_name equipment name as in the EQUIPMENT table, e.g. "CaenHV"
/// @param n_channels     number of channels, as in the DEVICE_DRIVER table
/// @param drv            the device descriptor, copied
/// @return always CM_SUCCESS: a failure to create the records is reported with
///         cm_msg(MERROR) and disables the alarm checks of this equipment, it
///         must never stop the frontend from monitoring the supply.
INT hv_alarm_init(const char *equipment_name, int n_channels, const hv_alarm_driver_t &drv);

/// @brief One alarm check of every registered equipment. Call from
///        frontend_loop() as often as you like: each equipment's check runs
///        at most once per second (steady_clock).
void hv_alarm_loop();

/// @brief Release every equipment's ODB handles. Does not reset pending
///        alarms (an alarm must survive a frontend restart).
void hv_alarm_exit();

#endif // HV_ALARM_H
