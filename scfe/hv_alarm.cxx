/********************************************************************\

  Name:         hv_alarm.cxx

  Contents:     Frontend side alarms for a cd_hv equipment (CaenHV) plus
                publication of Variables/Polarity.

  Threading:    main thread only. hv_alarm_init() runs from frontend_init(),
                hv_alarm_loop() from frontend_loop(), hv_alarm_exit() from
                frontend_exit(). No serial I/O happens here; the only driver
                call is caen_hv_polarity(), which reads an std::atomic cache
                and is documented as main-thread safe.

  Why frontend side: cd_hv has no alarm support and nothing in MIDAS evaluates
                AT_INTERNAL alarms, so the comparison against the per-channel
                thresholds has to happen here.

  Settings are read with db_get_value() once per second rather than through a
  db_open_record() hotlink struct: the record mixes BOOL / FLOAT[4] / DWORD /
  INT32, and a hand-written struct for it would depend on the compiler's
  padding. Re-reading eight small keys per second is free at this rate and
  edits take effect just as "live" as a hotlink would.

  One system message per burst: /Alarms/Classes/HV Alarm/System message
                interval gates the MTALK line per *alarm class*, not per alarm
                (al_trigger_class(), alarm.cxx:449-465). With the default of
                60 s, four channels tripping together produce one
                "[SlowControl,TALK] HV Alarm: ..." line in midas.log naming
                only the first of them, and a "CaenHV Comm" alarm arriving
                inside the same minute produces none at all. Every alarm is
                always visible in /Alarms/Alarms/<name> and on the mhttpd
                banner regardless. Shifters who want one line per alarm should
                lower "System message interval" on the class.

\********************************************************************/

#include <stdio.h>
#include <string.h>
#include <math.h>

#include <chrono>
#include <string>
#include <vector>

#include <midas.h>
#include <mfe.h>

#include "caen_hv_fe.h"
#include "hv_alarm.h"

/*---- constants ---------------------------------------------------*/

namespace {

/* The STAT bit numbers, their names and the "stale" flag all come from
   caen_hv_fe.h, which is their single source of truth - nothing about the
   status word is duplicated here, so a renumbering after the hardware run
   cannot silently diverge from the driver. caen_hv::kStatStale is not a device
   condition, so it is masked out before the alarm comparison: the CaenHV Comm
   alarm is what covers a dead link. */

/// @brief ChStatus word that means "never read yet", not a device status.
///
/// sc_thread pre-fills its per-channel float buffer with ss_nan() before the
/// first poll and hv_read() copies that buffer bit for bit into
/// Variables/ChStatus (device_driver.cxx:186-189 and hv.cxx:1031-1037), so a
/// fresh frontend shows the IEEE quiet-NaN pattern 0x7FC00000 there. Bit 30 of
/// it would look like an unknown high status bit and bit 31 is *not* set, so it
/// is neither an alarm nor "stale": it is simply unknown. Treated as such here
/// - no alarm, no reason text, and it does not count towards the comm alarm.
constexpr DWORD kStatUnknown = 0x7FC00000u;

/// @brief default Status Mask: the conditions that should raise an alarm.
///        Built from the driver's enum, never from literal bit numbers, so it
///        follows a renumbering of stat_bit_t automatically.
///        Today that is OVC | OVV | TRIP | OVP | OVT | ILK = 0x1398.
constexpr DWORD kDefaultStatusMask =
   (1u << caen_hv::kStatOvC)  | (1u << caen_hv::kStatOvV) |
   (1u << caen_hv::kStatTrip) | (1u << caen_hv::kStatOvP) |
   (1u << caen_hv::kStatOvT)  | (1u << caen_hv::kStatIlk);

/// @brief Voltage Max default [V]. It deliberately is *not* the device MAXV:
///        hv_alarm_init() runs in frontend_init(), i.e. before cd_hv's
///        hv_init() has read MAXV from the board, so there is nothing to copy
///        yet. 8000 V is the DT1470ET full scale, i.e. "no software alarm
///        limit" until the operator sets one per detector.
constexpr float kDefaultVoltageMax = 8000.f;

/// @brief Current Max default [uA] - full scale, same reasoning.
constexpr float kDefaultCurrentMax = 3000.f;

/// @brief full key set of an alarm class, in ALARM_CLASS order
///        (midas.h:1459-1471). al_trigger_class() reads every one of
///        "Write system message", "System message interval", "System message
///        last", "Write Elog message", "Execute command", "Execute interval",
///        "Execute last" and "Stop run" through midas::odb, which throws on a
///        missing subkey, so a partial class breaks every alarm of that class.
///        Values are MIDAS's own defaults for /Alarms/Classes/Alarm
///        (alarm.cxx:682-695), except that nothing here stops a run.
#define HV_ALARM_CLASS_STRING "\
Write system message = BOOL : y\n\
Write Elog message = BOOL : n\n\
System message interval = INT32 : 60\n\
System message last = DWORD : 0\n\
Execute command = STRING : [256] \n\
Execute interval = INT32 : 0\n\
Execute last = DWORD : 0\n\
Stop run = BOOL : n\n\
Display BGColor = STRING : [32] red\n\
Display FGColor = STRING : [32] black\n\
Alarm sound = BOOL : y\n\
"

}  // namespace

/*---- module state ------------------------------------------------*/

namespace {

/// @brief FALSE when init failed; every entry point then does nothing
bool s_active = false;

HNDLE s_hDB = 0;
/// @brief handle of /Equipment/<eq>
HNDLE s_hKeyEq = 0;

std::string s_eq_name;            ///< equipment name, e.g. "CaenHV"
std::string s_alarm_class;        ///< "HV Alarm"
int s_n_ch = 0;

/// @brief equipment[] index, -1 until found
int s_eq_index = -1;
/// @brief cached DEVICE_DRIVER::dd_info of the CAEN HV driver, NULL until the
///        class driver's init has run (which is after frontend_init())
void *s_dd_info = nullptr;

/// @brief last value written to Variables/Polarity, so it is written only on
///        change (once at start, then only when a rear switch is flipped)
std::vector<INT> s_pol_published;
bool s_pol_ever_published = false;

/// @brief ss_time() of the last second in which the channel's condition was
///        true; only meaningful while s_triggered[i]
std::vector<DWORD> s_last_bad_time;
/// @brief TRUE while we have an outstanding al_trigger_alarm() for a channel
std::vector<bool> s_triggered;

/// @brief ss_time() when the readings first went all-NaN, 0 while they are good
DWORD s_comm_bad_since = 0;
bool s_comm_triggered = false;

std::chrono::steady_clock::time_point s_last_check;

/*---- helpers -----------------------------------------------------*/

/// @brief build the db_create_record() string for Settings/Alarm.
/// The array defaults use the ODB text format "<key> = <type>[<n>] :" followed
/// by one "[i] <value>" line per element.
std::string alarm_settings_string(int n_ch)
{
   std::string s = "Enabled = BOOL : y\n";

   char line[128];
   sprintf(line, "Voltage Max = FLOAT[%d] :\n", n_ch);
   s += line;
   for (int i = 0; i < n_ch; i++) {
      sprintf(line, "[%d] %g\n", i, kDefaultVoltageMax);
      s += line;
   }

   sprintf(line, "Current Max = FLOAT[%d] :\n", n_ch);
   s += line;
   for (int i = 0; i < n_ch; i++) {
      sprintf(line, "[%d] %g\n", i, kDefaultCurrentMax);
      s += line;
   }

   sprintf(line, "Status Mask = DWORD : %u\n", (unsigned) kDefaultStatusMask);
   s += line;
   s += "Clear After s = INT32 : 10\n";
   s += "Comm Timeout s = INT32 : 60\n";

   return s;
}

/// @brief make sure /Alarms/Classes/<s_alarm_class> has every key
///        al_trigger_class() needs.
/// db_create_record() with the full layout is used unconditionally instead of
/// db_copy()ing /Alarms/Classes/Alarm: it adds only the missing keys, keeps
/// whatever the operator has already edited, and also repairs a class that an
/// earlier partial version of this frontend may have left behind. Copying
/// "Alarm" would gain nothing, its MIDAS defaults are the ones below.
INT ensure_alarm_class()
{
   std::string path = "/Alarms/Classes/" + s_alarm_class;

   INT status = db_create_record(s_hDB, 0, path.c_str(), HV_ALARM_CLASS_STRING);
   if (status != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "Cannot create alarm class \"%s\", db_create_record() returned %d",
             path.c_str(), status);
      return status;
   }
   return DB_SUCCESS;
}

/// @brief is the equipment switched on in ODB?
bool equipment_enabled()
{
   BOOL enabled = TRUE;
   INT size = sizeof(enabled);
   /* create = FALSE: cd_hv/mfe own this key. If it is not there yet we simply
      assume enabled - one spurious check is harmless. */
   db_get_value(s_hDB, s_hKeyEq, "Common/Enabled", &enabled, &size, TID_BOOL, FALSE);
   return enabled ? true : false;
}

/// @brief Variables/Polarity[n] from the driver's cache, written on change only.
void publish_polarity()
{
   if (s_dd_info == nullptr) {
      /* the class driver fills dd_info in its own CMD_INIT, which mfe runs
         after frontend_init(), so this can only be resolved from the loop */
      if (s_eq_index < 0)
         return;
      if (equipment[s_eq_index].driver == nullptr)
         return;
      s_dd_info = equipment[s_eq_index].driver[0].dd_info;
      if (s_dd_info == nullptr)
         return;
   }

   std::vector<INT> pol(s_n_ch, 0);
   bool changed = !s_pol_ever_published;
   for (int i = 0; i < s_n_ch; i++) {
      pol[i] = caen_hv_polarity(s_dd_info, i);
      if (pol[i] != s_pol_published[i])
         changed = true;
   }
   if (!changed)
      return;

   INT status = db_set_value(s_hDB, s_hKeyEq, "Variables/Polarity", pol.data(),
                             sizeof(INT) * s_n_ch, s_n_ch, TID_INT32);
   if (status != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm", "Cannot write Variables/Polarity, db_set_value() returned %d", status);
      return;
   }
   s_pol_published = pol;
   s_pol_ever_published = true;
}

}  // namespace

/*---- entry points ------------------------------------------------*/

INT hv_alarm_init(const char *equipment_name, int n_channels)
{
   s_active = false;

   if (equipment_name == nullptr || n_channels <= 0) {
      cm_msg(MERROR, "hv_alarm_init", "Bad arguments, HV alarms disabled");
      return CM_SUCCESS;
   }

   s_eq_name = equipment_name;
   s_alarm_class = "HV Alarm";
   s_n_ch = n_channels;

   INT status = cm_get_experiment_database(&s_hDB, NULL);
   if (status != CM_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "No ODB handle, HV alarms disabled");
      return CM_SUCCESS;
   }

   std::string eq_path = "/Equipment/" + s_eq_name;
   status = db_create_key(s_hDB, 0, eq_path.c_str(), TID_KEY);
   if (status != DB_SUCCESS && status != DB_KEY_EXIST) {
      cm_msg(MERROR, "hv_alarm_init", "Cannot create \"%s\" (%d), HV alarms disabled", eq_path.c_str(), status);
      return CM_SUCCESS;
   }
   status = db_find_key(s_hDB, 0, eq_path.c_str(), &s_hKeyEq);
   if (status != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "Cannot find \"%s\" (%d), HV alarms disabled", eq_path.c_str(), status);
      return CM_SUCCESS;
   }

   std::string set_path = eq_path + "/Settings/Alarm";
   status = db_create_record(s_hDB, 0, set_path.c_str(), alarm_settings_string(s_n_ch).c_str());
   if (status != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "Cannot create \"%s\" (%d), HV alarms disabled", set_path.c_str(), status);
      return CM_SUCCESS;
   }

   if (ensure_alarm_class() != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "HV alarms disabled");
      return CM_SUCCESS;
   }

   /* find our own equipment slot once, for DEVICE_DRIVER::dd_info later */
   s_eq_index = -1;
   for (int k = 0; equipment[k].name[0] != 0; k++) {
      if (equal_ustring(equipment[k].name, s_eq_name.c_str())) {
         s_eq_index = k;
         break;
      }
   }
   if (s_eq_index < 0)
      cm_msg(MERROR, "hv_alarm_init", "Equipment \"%s\" not in the equipment table, "
             "Variables/Polarity will not be published", s_eq_name.c_str());

   s_pol_published.assign(s_n_ch, 0);
   s_pol_ever_published = false;
   s_last_bad_time.assign(s_n_ch, 0);
   s_triggered.assign(s_n_ch, false);
   s_comm_bad_since = 0;
   s_comm_triggered = false;
   s_last_check = std::chrono::steady_clock::now();

   s_active = true;
   cm_msg(MINFO, "hv_alarm_init", "HV alarms active for \"%s\", %d channels, class \"%s\"",
          s_eq_name.c_str(), s_n_ch, s_alarm_class.c_str());

   return CM_SUCCESS;
}

/*------------------------------------------------------------------*/

void hv_alarm_loop()
{
   if (!s_active)
      return;

   auto now = std::chrono::steady_clock::now();
   if (now - s_last_check < std::chrono::seconds(1))
      return;
   s_last_check = now;

   if (!equipment_enabled())
      return;

   publish_polarity();

   /*---- settings, re-read every second so edits apply live ----*/
   BOOL al_enabled = TRUE;
   INT size = sizeof(al_enabled);
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Enabled", &al_enabled, &size, TID_BOOL, FALSE);
   if (!al_enabled)
      return;

   std::vector<float> vmax(s_n_ch, kDefaultVoltageMax);
   size = sizeof(float) * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Voltage Max", vmax.data(), &size, TID_FLOAT, FALSE);

   std::vector<float> imax(s_n_ch, kDefaultCurrentMax);
   size = sizeof(float) * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Current Max", imax.data(), &size, TID_FLOAT, FALSE);

   DWORD status_mask = kDefaultStatusMask;
   size = sizeof(status_mask);
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Status Mask", &status_mask, &size, TID_DWORD, FALSE);
   /* bit 31 is the driver's own "STAT is stale" flag, never a device alarm */
   status_mask &= ~caen_hv::kStatStale;

   INT clear_after = 10;
   size = sizeof(clear_after);
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Clear After s", &clear_after, &size, TID_INT32, FALSE);

   INT comm_timeout = 60;
   size = sizeof(comm_timeout);
   db_get_value(s_hDB, s_hKeyEq, "Settings/Alarm/Comm Timeout s", &comm_timeout, &size, TID_INT32, FALSE);

   /*---- readings ----*/
   std::vector<float> meas(s_n_ch, (float) ss_nan());
   size = sizeof(float) * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Variables/Measured", meas.data(), &size, TID_FLOAT, FALSE);

   std::vector<float> curr(s_n_ch, (float) ss_nan());
   size = sizeof(float) * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Variables/Current", curr.data(), &size, TID_FLOAT, FALSE);

   std::vector<DWORD> stat(s_n_ch, 0);
   size = sizeof(DWORD) * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Variables/ChStatus", stat.data(), &size, TID_DWORD, FALSE);

   std::vector<char> names(NAME_LENGTH * s_n_ch, 0);
   size = NAME_LENGTH * s_n_ch;
   db_get_value(s_hDB, s_hKeyEq, "Settings/Names", names.data(), &size, TID_STRING, FALSE);

   DWORD t_now = ss_time();

   /*---- per channel ----*/
   for (int i = 0; i < s_n_ch; i++) {
      std::string reason;

      /* NaN means "no reading", not "over limit": every comparison with NaN is
         false anyway, this is only here to say so out loud. */
      if (!std::isnan(meas[i]) && meas[i] > vmax[i])
         reason += reason.empty() ? "over voltage" : ", over voltage";
      /* caen_hv::kCurrentNeverRead (-1) needs no test of its own: Current Max
         is a positive limit, so a sentinel of -1 can never satisfy ">" */
      if (!std::isnan(curr[i]) && curr[i] > imax[i])
         reason += reason.empty() ? "over current" : ", over current";

      /* "never polled yet" is not a status word, see kStatUnknown */
      const bool stat_known = (stat[i] != kStatUnknown);

      DWORD bad_bits = stat_known ? (stat[i] & status_mask) : 0;
      if (bad_bits) {
         /* the driver owns the bit names, see caen_hv_fe.h */
         std::string bits = caen_hv::stat_text(bad_bits);
         if (bits == "none")   /* mask bit above the driver's table */
            bits = msprintf("0x%x", (unsigned) bad_bits);
         reason += reason.empty() ? ("status " + bits) : (", status " + bits);
      }

      const char *name = &names[NAME_LENGTH * i];
      std::string alarm_name = s_eq_name + " Ch" + std::to_string(i);

      if (!reason.empty()) {
         s_last_bad_time[i] = t_now;
         s_triggered[i] = true;
         /* Called every second on purpose: al_trigger_alarm() self-gates on the
            alarm's own "Check interval" (alarm.cxx:353-358). A private latch
            would stop re-triggering after an operator acknowledges the alarm
            while the condition is still there. */
         std::string stat_str = stat_known ? msprintf("0x%x", (unsigned) stat[i]) : "unknown";
         std::string msg = msprintf("%s: %s (V=%.1f I=%.2f STAT=%s)",
                                    name[0] ? name : alarm_name.c_str(), reason.c_str(),
                                    meas[i], curr[i], stat_str.c_str());
         al_trigger_alarm(alarm_name.c_str(), msg.c_str(),
                          s_alarm_class.c_str(), "", AT_INTERNAL);
      } else if (s_triggered[i]) {
         if (t_now - s_last_bad_time[i] >= (DWORD) (clear_after < 0 ? 0 : clear_after)) {
            al_reset_alarm(alarm_name.c_str());
            s_triggered[i] = false;
         }
      }
   }

   /*---- communication ----*/
   /* Two independent signals say the link is dead:
      - every channel's STAT is stale (caen_hv::kStatStale). This shows up one
        poll period after the port stopped answering, because hv_read() writes
        Variables/ChStatus whenever the word differs (hv.cxx:283-300).
      - every Measured is NaN. Correct but slow: none of hv_read()'s change
        tests fire for NaN (ABS(NaN - x) > thr and !isnan(m) && isnan(mirror)
        are both false), so the NaN only reaches ODB through the "last update
        older than a minute" fallback, hv.cxx:210-240.
      The stale word is therefore the fast signal and the NaN the backstop; a
      word of kStatUnknown counts as neither. Either signal starts the same
      Comm Timeout s debounce, so that setting keeps its documented meaning -
      lower it if the alarm should come faster than a minute. */
   bool all_nan = true;
   bool all_stale = true;
   for (int i = 0; i < s_n_ch; i++) {
      if (!std::isnan(meas[i]))
         all_nan = false;
      if (stat[i] == kStatUnknown || !(stat[i] & caen_hv::kStatStale))
         all_stale = false;
   }

   std::string comm_name = s_eq_name + " Comm";
   if (all_stale || all_nan) {
      if (s_comm_bad_since == 0)
         s_comm_bad_since = t_now;
      DWORD dead = t_now - s_comm_bad_since;
      if (dead >= (DWORD) (comm_timeout < 0 ? 0 : comm_timeout)) {
         s_comm_triggered = true;
         /* name the signal that fired, the two mean different things to a
            shifter: a stale status word means the port stopped answering,
            all-NaN readings can also be a driver that never got going */
         std::string msg = all_stale
            ? msprintf("status stale on all channels for %u s", (unsigned) dead)
            : msprintf("no readings from CAEN HV for %u s", (unsigned) dead);
         al_trigger_alarm(comm_name.c_str(), msg.c_str(),
                          s_alarm_class.c_str(), "", AT_INTERNAL);
      }
   } else {
      s_comm_bad_since = 0;
      if (s_comm_triggered) {
         al_reset_alarm(comm_name.c_str());
         s_comm_triggered = false;
      }
   }
}

/*------------------------------------------------------------------*/

void hv_alarm_exit()
{
   /* Alarms are deliberately *not* reset here: a tripped channel must still be
      flagged after a frontend restart. */
   s_active = false;
   s_hKeyEq = 0;
   s_dd_info = nullptr;
}
