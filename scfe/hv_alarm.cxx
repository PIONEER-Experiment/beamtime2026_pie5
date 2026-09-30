/********************************************************************\

  Name:         hv_alarm.cxx

  Contents:     Frontend side alarms for cd_hv equipments (CaenHV, ...) plus
                publication of Variables/Polarity.

  Threading:    main thread only. hv_alarm_init() runs from frontend_init(),
                hv_alarm_loop() from frontend_loop(), hv_alarm_exit() from
                frontend_exit(). No serial I/O happens here; the only driver
                call is hv_alarm_driver_t::polarity, which must read a cache
                that is safe from the main thread (caen_hv_polarity() reads an
                std::atomic).

  One instance per equipment: every hv_alarm_init() call registers one
                equipment with its own hv_alarm_driver_t, and hv_alarm_loop()
                checks all of them. Nothing in this file knows a device: bit
                numbers, bit names and defaults all come from the descriptor,
                which the driver's own header is the source of.

  Why frontend side: cd_hv has no alarm support and nothing in MIDAS evaluates
                AT_INTERNAL alarms, so the comparison against the per-channel
                thresholds has to happen here.

  Settings are read with db_get_value() once per second rather than through a
  db_open_record() hotlink struct: the record mixes BOOL / FLOAT[4] / DWORD /
  INT32, and a hand-written struct for it would depend on the compiler's
  padding. Re-reading eight small keys per second is free at this rate and
  edits take effect just as "live" as a hotlink would.

  KEEP EVERY MESSAGE SHORT: MIDAS mfe.cxx message_print() overflows a 160-byte
                stack buffer on long cm_msg text. mfe.cxx:1348-1357 strips
                everything up to the first "] " - that is only the
                "[SlowControl,INFO] " client prefix - and then does
                "char str[160]; memcpy(str, msg, strlen(msg))" with no bound,
                so 159 characters or more after that prefix abort the whole
                frontend with glibc's "*** buffer overflow detected ***". This
                has already crashed scfe on hardware once. What counts:
                  - MINFO/MTALK: the body only -> kMaxMsgLen (120).
                  - MERROR: cm_msg adds a second prefix
                    "[hv_alarm.cxx:NNN:routine,ERROR] " (midas.cxx cm_msg_format)
                    which message_print keeps, ~40 characters -> kMaxErrMsgLen
                    (110) for the body.
                  - an alarm body goes out twice from inside this process,
                    through al_trigger_class() (alarm.cxx:449-490): as MTALK
                    "<class>: <body>", and, when the class has an Execute
                    command, as MINFO "Execute: <command with %s replaced by
                    '<class>: <body>'>". On pinky that command is the Slack
                    script, ~50 characters, so the body has much less than 100
                    left: alarm_msg_limit() re-reads the command on every
                    check and clips each alarm body to what fits.
                A channel name is up to 31 characters, an equipment name up to
                31 and a fully decoded STAT word about 40, so no message here
                may interpolate all of them raw: the limits above,
                stat_brief() and clip() are the guards, and clip() is the last
                one - put it on every cm_msg or al_trigger_alarm body that
                contains anything variable.

  One system message per burst: /Alarms/Classes/HV Alarm/System message
                interval gates the MTALK line per *alarm class*, not per alarm
                (al_trigger_class(), alarm.cxx:449-465). With the default of
                60 s, four channels tripping together produce one
                "[SlowControl,TALK] HV Alarm: ..." line in midas.log naming
                only the first of them, and a "CaenHV Comm" alarm arriving
                inside the same minute produces none at all. All equipments
                share the class, so this holds across equipments too. Every
                alarm is always visible in /Alarms/Alarms/<name> and on the
                mhttpd banner regardless. Shifters who want one line per alarm
                should lower "System message interval" on the class.

\********************************************************************/

#include <stdio.h>
#include <string.h>
#include <math.h>

#include <chrono>
#include <string>
#include <vector>

#include <midas.h>
#include <mfe.h>

#include "hv_alarm.h"

/*---- constants ---------------------------------------------------*/

namespace {

/* Everything about the status word - which bit means "on", which is the
   driver's "stale" flag, what the bits are called, which ones are faults by
   default - comes from the hv_alarm_driver_t the equipment was registered
   with. The stale bit is not a device condition, so it is masked out before
   the alarm comparison: the "<eq> Comm" alarm is what covers a dead link. */

/// @brief TRUE for a ChStatus word that means "never read yet", not a device
///        status.
///
/// sc_thread pre-fills its per-channel float buffer with ss_nan() before the
/// first poll and hv_read() copies that buffer bit for bit into
/// Variables/ChStatus (device_driver.cxx:186-189 and hv.cxx:1031-1037), so a
/// fresh frontend shows a float NaN there. Which NaN depends on how libmidas
/// was compiled: ss_nan() is 0/0 (system.cxx:8025-8032), which x86 evaluates
/// at run time to the *negative* quiet NaN 0xFFC00000, while a compiler that
/// folds it gets 0x7FC00000. The negative one has bit 31 set, so testing for
/// 0x7FC00000 alone made it look "stale", and with the iseg fault mask its
/// bits 22-25 read as T_ERR|AUTOSTART|TOT - a false alarm at every start. So
/// any NaN pattern (exponent bits 23-30 all set, nonzero mantissa) counts as
/// unknown: no alarm, no reason text, and not towards the comm alarm. No real
/// status word collides with it: the CAEN word uses bits 0-13 and 31, the
/// iseg word bits 0-26 and 31, so bits 27-30 are never all set.
/// This comes from cd_hv, not from a device, so it holds for every driver.
bool stat_is_unknown(DWORD stat)
{
   return (stat & 0x7F800000u) == 0x7F800000u && (stat & 0x007FFFFFu) != 0;
}

/// @brief hard ceiling for an MINFO/MTALK cm_msg body, see the note in the
///        file header. 120 leaves a wide margin below mfe.cxx's 160-byte
///        stack buffer.
constexpr size_t kMaxMsgLen = 120;

/// @brief ceiling for an MERROR body: its "[hv_alarm.cxx:NNN:routine,ERROR] "
///        prefix (~40 characters) stays in the 160 bytes, see the file header
constexpr size_t kMaxErrMsgLen = 110;

/// @brief ceiling for an al_trigger_alarm() body. Lower than kMaxMsgLen
///        because al_trigger_class() prepends "<alarm class>: " before
///        handing the text to cm_msg(MTALK) (alarm.cxx:455-462), and that
///        prefix counts towards the same 160 bytes.
constexpr size_t kMaxAlarmMsgLen = 100;

/// @brief alarm_msg_limit() never clips an alarm body below this, however
///        long the class's Execute command is: an alarm must still say which
///        channel and why. A command so long that even this overflows the
///        160 bytes cannot be protected from here - keep it short.
constexpr size_t kMinAlarmMsgLen = 40;

/// @brief safety margin in alarm_msg_limit() [characters]
constexpr size_t kExecMsgMargin = 8;

/// @brief reason text of the on_while_off check; also how an adopted alarm
///        is recognised as one of those after a restart
constexpr const char *kOnWhileOffReason = "on but ChState OFF";

/// @brief the same for the deviation check (its reason is "deviation N V")
constexpr const char *kDeviationReason = "deviation";

/// @brief above this many characters a decoded STAT word is replaced by its
///        hex value. caen_hv::stat_text() can reach ~40 characters when many
///        bits are set, which alone would eat a third of the budget.
constexpr size_t kMaxStatTextLen = 24;

/// @brief how long "ODB says on, board says off" has to last before it is
///        reported [s]. Long enough to ride out a ramp-up or a poll in flight.
constexpr DWORD kOffMismatchSeconds = 5;

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

/// @brief a condition that must hold for a while before it raises an alarm,
///        and then stays raised until it has been gone for Clear After s.
///        See held().
struct held_t {
   DWORD since = 0;       ///< ss_time() the current unbroken episode began, 0 = none
   DWORD last = 0;        ///< ss_time() the condition last counted while latched
   bool latched = false;  ///< TRUE once it has counted: no hold until it clears
};

/// @brief everything this module keeps about one registered equipment
struct instance_t {
   /// @brief FALSE when init failed; the instance then does nothing
   bool active = false;

   hv_alarm_driver_t drv{};         ///< copy of the caller's descriptor

   HNDLE hDB = 0;
   /// @brief handle of /Equipment/<eq>
   HNDLE hKeyEq = 0;

   std::string eq_name;             ///< equipment name, e.g. "CaenHV"
   std::string alarm_class;         ///< "HV Alarm"
   int n_ch = 0;

   /// @brief equipment[] index, -1 until found
   int eq_index = -1;
   /// @brief cached DEVICE_DRIVER::dd_info of the device driver, NULL until
   ///        the class driver's init has run (which is after frontend_init())
   void *dd_info = nullptr;

   /// @brief FALSE until Settings/Editable has been inspected once this start
   bool editable_checked = false;

   /// @brief last value written to Variables/Polarity, so it is written only
   ///        on change (once at start, then only when a rear switch is flipped)
   std::vector<INT> pol_published;
   bool pol_ever_published = false;

   /// @brief ss_time() when the current "ChState on but board off" episode
   ///        started, 0 while there is no mismatch
   std::vector<DWORD> off_mismatch_since;
   /// @brief TRUE once the episode has been reported, so it is reported once
   std::vector<bool> off_mismatch_logged;

   /// @brief per channel "|D-M| above Deviation Max" (deviation_check only)
   std::vector<held_t> deviation;
   /// @brief per channel "ChState 0 but unit on" (on_while_off_check only)
   std::vector<held_t> on_while_off;

   /// @brief ss_time() of the last second in which the channel's condition
   ///        was true; only meaningful while triggered[i]
   std::vector<DWORD> last_bad_time;
   /// @brief TRUE while we have an outstanding al_trigger_alarm() for a channel
   std::vector<bool> triggered;

   /// @brief ss_time() when the readings first went all-NaN, 0 while good
   DWORD comm_bad_since = 0;
   bool comm_triggered = false;

   std::chrono::steady_clock::time_point last_check;
};

/// @brief one entry per hv_alarm_init() call, in call order
std::vector<instance_t> s_instances;

/*---- helpers -----------------------------------------------------*/

/// @brief truncate a message body in place and return it, ready for cm_msg.
///
/// Last line of defence against the mfe.cxx overflow described in the file
/// header. Every message here is also *built* to fit, but a 31-character
/// channel name plus a long STAT text can still add up, and a truncated
/// message is infinitely better than a dead frontend.
const char *clip(std::string &body, size_t max_len = kMaxMsgLen)
{
   if (body.size() > max_len) {
      body.resize(max_len - 3);
      body += "...";
   }
   return body.c_str();
}

/// @brief does a held condition count as an alarm reason this second?
///
/// Raising needs @p cond to hold for @p hold seconds without a break; any
/// second without it restarts that. Once it has counted, it is latched: every
/// second with @p cond counts at once, without the hold, and the latch only
/// drops after @p clear_after seconds without @p cond - the same rule that
/// resets the channel's alarm, so the two end together. Without the latch
/// one bad reading in the middle of a real deviation would restart the hold
/// and let the alarm reset and re-raise (flap).
bool held(held_t &h, bool cond, DWORD t_now, DWORD hold, DWORD clear_after)
{
   if (cond) {
      if (h.since == 0)
         h.since = t_now;
      if (h.latched || t_now - h.since >= hold) {
         h.latched = true;
         h.last = t_now;
         return true;
      }
      return false;
   }
   h.since = 0;
   if (h.latched && t_now - h.last >= clear_after)
      h.latched = false;
   return false;
}

/// @brief the longest alarm body that fits the mfe.cxx 160-byte buffer,
///        given the class's current Execute command (see the file header).
///
/// al_trigger_class() logs MINFO "Execute: " + the command with its "%s"
/// replaced by "<class>: <body>". What must fit in the 159 usable characters
/// after message_print() has stripped "[SlowControl,INFO] ":
///   9 ("Execute: ") + strlen(cmd) - 2 ("%s") + strlen(class) + 2 (": ")
///   + body + kExecMsgMargin.
/// Without a command (or with Execute interval <= 0, when MIDAS does not run
/// it) only the MTALK line counts and kMaxAlarmMsgLen applies. Read every
/// check, so an edited command takes effect at once.
size_t alarm_msg_limit(const instance_t &in)
{
   std::string base = "/Alarms/Classes/" + in.alarm_class;
   char cmd[256] = {0};
   INT size = sizeof(cmd);
   if (db_get_value(in.hDB, 0, (base + "/Execute command").c_str(), cmd, &size,
                    TID_STRING, FALSE) != DB_SUCCESS || cmd[0] == 0)
      return kMaxAlarmMsgLen;
   INT interval = 0;
   size = sizeof(interval);
   db_get_value(in.hDB, 0, (base + "/Execute interval").c_str(), &interval, &size, TID_INT32, FALSE);
   if (interval <= 0)
      return kMaxAlarmMsgLen;

   const long used = 9 + ((long) strlen(cmd) - 2) + ((long) in.alarm_class.size() + 2)
                     + (long) kExecMsgMargin;
   const long room = 159 - used;
   if (room < (long) kMinAlarmMsgLen)
      return kMinAlarmMsgLen;
   return room < (long) kMaxAlarmMsgLen ? (size_t) room : kMaxAlarmMsgLen;
}

/// @brief STAT as "ON|OVC" text, or as hex when the text would be too long.
std::string stat_brief(const instance_t &in, DWORD stat)
{
   std::string text = in.drv.stat_text(stat);
   if (text.size() > kMaxStatTextLen)
      return msprintf("0x%x", (unsigned) stat);
   return text;
}

/// @brief one "<key> = FLOAT[<n>] :" block with the same default everywhere
///
/// A single channel is written as a plain "<key> = FLOAT : <value>": the
/// ODB text parser does not take "FLOAT[1] :" plus a "[0] <value>" line as a
/// one element array - it made the key 0 and put every following key into a
/// stray subdirectory called "0", so a 1-channel equipment had Voltage Max 0
/// and an over-voltage alarm the moment it was on.
void append_float_array(std::string &s, const char *key, int n_ch, float value)
{
   char line[128];
   if (n_ch == 1) {
      sprintf(line, "%s = FLOAT : %g\n", key, value);
      s += line;
      return;
   }
   sprintf(line, "%s = FLOAT[%d] :\n", key, n_ch);
   s += line;
   for (int i = 0; i < n_ch; i++) {
      sprintf(line, "[%d] %g\n", i, value);
      s += line;
   }
}

/// @brief build the db_create_record() string for Settings/Alarm.
/// The array defaults use the ODB text format "<key> = <type>[<n>] :" followed
/// by one "[i] <value>" line per element. "Deviation Max" and "Deviation
/// Hold s" only exist for a driver that asked for the deviation check.
std::string alarm_settings_string(const instance_t &in)
{
   std::string s = "Enabled = BOOL : y\n";

   append_float_array(s, "Voltage Max", in.n_ch, in.drv.default_voltage_max);
   append_float_array(s, "Current Max", in.n_ch, in.drv.default_current_max);
   if (in.drv.deviation_check) {
      append_float_array(s, "Deviation Max", in.n_ch, in.drv.default_deviation_max);
      char hold[64];
      sprintf(hold, "Deviation Hold s = INT32 : %d\n", (int) in.drv.default_deviation_hold_s);
      s += hold;
   }

   char line[128];
   sprintf(line, "Status Mask = DWORD : %u\n", (unsigned) in.drv.default_status_mask);
   s += line;
   s += "Clear After s = INT32 : 10\n";
   s += "Comm Timeout s = INT32 : 60\n";

   return s;
}

/// @brief make sure /Alarms/Classes/<alarm_class> has every key
///        al_trigger_class() needs.
/// db_create_record() with the full layout is used unconditionally instead of
/// db_copy()ing /Alarms/Classes/Alarm: it adds only the missing keys, keeps
/// whatever the operator has already edited, and also repairs a class that an
/// earlier partial version of this frontend may have left behind. Copying
/// "Alarm" would gain nothing, its MIDAS defaults are the ones below. Running
/// it once per equipment is harmless for the same reason.
INT ensure_alarm_class(const instance_t &in)
{
   std::string path = "/Alarms/Classes/" + in.alarm_class;

   INT status = db_create_record(in.hDB, 0, path.c_str(), HV_ALARM_CLASS_STRING);
   if (status != DB_SUCCESS) {
      std::string m = msprintf("cannot create alarm class %s (%d)", path.c_str(), status);
      cm_msg(MERROR, "hv_alarm_init", "%s", clip(m, kMaxErrMsgLen));
      return status;
   }
   return DB_SUCCESS;
}

/// @brief is the equipment switched on in ODB?
bool equipment_enabled(const instance_t &in)
{
   BOOL enabled = TRUE;
   INT size = sizeof(enabled);
   /* create = FALSE: cd_hv/mfe own this key. If it is not there yet we simply
      assume enabled - one spurious check is harmless. */
   db_get_value(in.hDB, in.hKeyEq, "Common/Enabled", &enabled, &size, TID_BOOL, FALSE);
   return enabled ? true : false;
}

/// @brief is /Alarms/Alarms/<name>/Triggered present and nonzero?
///
/// Used to adopt an alarm a *previous* scfe instance raised. The alarm records
/// live in ODB and survive a frontend restart, but triggered[] does not, and
/// al_reset_alarm() is only ever called for an alarm this process considers its
/// own - so without this a channel that tripped before the restart would stay
/// red forever even after the condition cleared. Adopting is deliberately the
/// only thing done here: resetting at init instead would hide a condition that
/// is still present, which is exactly what the operator needs to see.
/// @brief /Alarms/Alarms/<name>/Alarm Message, "" when there is none
std::string alarm_message(HNDLE hDB, const std::string &alarm_name)
{
   char text[256] = {0};
   INT size = sizeof(text);
   std::string path = "/Alarms/Alarms/" + alarm_name + "/Alarm Message";
   if (db_get_value(hDB, 0, path.c_str(), text, &size, TID_STRING, FALSE) != DB_SUCCESS)
      return "";
   return text;
}

bool alarm_is_triggered(HNDLE hDB, const std::string &alarm_name)
{
   INT triggered = 0;
   INT size = sizeof(triggered);
   std::string path = "/Alarms/Alarms/" + alarm_name + "/Triggered";
   /* create = FALSE: a missing record simply means "never triggered", and
      al_trigger_alarm() is what creates it (alarm.cxx:307-322) */
   if (db_get_value(hDB, 0, path.c_str(), &triggered, &size, TID_INT32, FALSE) != DB_SUCCESS) {
      return false;
   }
   return triggered != 0;
}

/// @brief Collapse Settings/Editable to one comma separated string.
///
/// WORKAROUND for a MIDAS mismatch, remove once MIDAS is fixed:
/// $MIDASSYS/resources/eqtable.js:598-600 does
/// "eq.settings.editable.toLowerCase()", but cd_hv writes
/// /Equipment/<eq>/Settings/Editable as a 2 element STRING *array*
/// ("Demand", "ChState"), hv.cxx:790-803. Calling toLowerCase() on an array
/// throws a TypeError, which leaves mhttpd's ?cmd=eqtable&eq=CaenHV page
/// completely blank - the one page a shifter uses to set a voltage.
/// eqtable.js's other use of the key (line 447, .includes()) is happy with
/// "Demand,ChState", and cd_hv only ever writes the key, never reads it back,
/// so rewriting it as a single string costs nothing. cd_hv recreates the array
/// in every hv_init(), so this has to run on every frontend start, and it runs
/// from hv_alarm_loop() rather than hv_alarm_init() because the key does not
/// exist until the class driver's init has run.
void fix_editable(instance_t &in)
{
   HNDLE hKey;
   if (db_find_key(in.hDB, in.hKeyEq, "Settings/Editable", &hKey) != DB_SUCCESS)
      return;                  /* cd_hv has not created it yet, retry next pass */

   KEY key;
   if (db_get_key(in.hDB, hKey, &key) != DB_SUCCESS)
      return;

   in.editable_checked = true;
   if (key.type != TID_STRING || key.num_values <= 1 || key.item_size <= 0)
      return;                  /* already a single string, or not ours to touch */

   std::vector<char> buf((size_t) key.total_size, 0);
   INT size = key.total_size;
   if (db_get_data(in.hDB, hKey, buf.data(), &size, TID_STRING) != DB_SUCCESS)
      return;

   /* the array is item_size (NAME_LENGTH) bytes per entry, NUL terminated */
   std::string joined;
   for (int i = 0; i < key.num_values; i++) {
      const char *value = &buf[(size_t) key.item_size * i];
      if (value[0] == 0)
         continue;
      if (!joined.empty())
         joined += ",";
      joined += value;
   }
   if (joined.empty())
      return;

   /* One db_set_value is enough: db_set_data_wlocked() reallocates the data,
      sets num_values = 1 and, for TID_STRING, item_size = data_size
      (odb.cxx:7533-7565), so no separate db_set_num_values() is needed. */
   if (db_set_value(in.hDB, in.hKeyEq, "Settings/Editable", joined.c_str(),
                    (INT) joined.size() + 1, 1, TID_STRING) != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm", "cannot collapse Settings/Editable for mhttpd");
      return;
   }

   cm_msg(MINFO, "hv_alarm", "Settings/Editable collapsed to one string for mhttpd "
          "(eqtable.js:600 vs hv.cxx:802)");
}

/// @brief Variables/Polarity[n] from the driver's cache, written on change only.
void publish_polarity(instance_t &in)
{
   if (in.drv.polarity == nullptr)
      return;                  /* the driver does not know its polarity */

   if (in.dd_info == nullptr) {
      /* the class driver fills dd_info in its own CMD_INIT, which mfe runs
         after frontend_init(), so this can only be resolved from the loop */
      if (in.eq_index < 0)
         return;
      if (equipment[in.eq_index].driver == nullptr)
         return;
      in.dd_info = equipment[in.eq_index].driver[0].dd_info;
      if (in.dd_info == nullptr)
         return;
   }

   std::vector<INT> pol(in.n_ch, 0);
   bool changed = !in.pol_ever_published;
   for (int i = 0; i < in.n_ch; i++) {
      pol[i] = in.drv.polarity(in.dd_info, i);
      if (pol[i] != in.pol_published[i])
         changed = true;
   }
   if (!changed)
      return;

   INT status = db_set_value(in.hDB, in.hKeyEq, "Variables/Polarity", pol.data(),
                             sizeof(INT) * in.n_ch, in.n_ch, TID_INT32);
   if (status != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm", "cannot write Variables/Polarity (%d)", status);
      return;
   }
   in.pol_published = pol;
   in.pol_ever_published = true;
}

/// @brief set up one instance; on any failure it stays inactive (the caller
///        has already reported why)
void init_instance(instance_t &in, const char *equipment_name, int n_channels,
                   const hv_alarm_driver_t &drv)
{
   in.active = false;

   in.drv = drv;
   in.eq_name = equipment_name;
   in.alarm_class = "HV Alarm";
   in.n_ch = n_channels;

   INT status = cm_get_experiment_database(&in.hDB, NULL);
   if (status != CM_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "no ODB handle, HV alarms off");
      return;
   }

   std::string eq_path = "/Equipment/" + in.eq_name;
   status = db_create_key(in.hDB, 0, eq_path.c_str(), TID_KEY);
   if (status != DB_SUCCESS && status != DB_KEY_EXIST) {
      std::string m = msprintf("cannot create %s (%d), HV alarms off", eq_path.c_str(), status);
      cm_msg(MERROR, "hv_alarm_init", "%s", clip(m, kMaxErrMsgLen));
      return;
   }
   status = db_find_key(in.hDB, 0, eq_path.c_str(), &in.hKeyEq);
   if (status != DB_SUCCESS) {
      std::string m = msprintf("cannot find %s (%d), HV alarms off", eq_path.c_str(), status);
      cm_msg(MERROR, "hv_alarm_init", "%s", clip(m, kMaxErrMsgLen));
      return;
   }

   std::string set_path = eq_path + "/Settings/Alarm";

   /* A 1-channel equipment run by an earlier build of this file got a broken
      Settings/Alarm: "FLOAT[1] :" + "[0] v" made Voltage Max 0 and put every
      later key into a subdirectory "0" (see append_float_array()). Because
      db_create_record() merges by path, the zero would stay forever, so a
      record with that subdirectory is deleted and rebuilt from the defaults. */
   HNDLE hStray;
   if (db_find_key(in.hDB, 0, (set_path + "/0").c_str(), &hStray) == DB_SUCCESS) {
      HNDLE hSet;
      if (db_find_key(in.hDB, 0, set_path.c_str(), &hSet) == DB_SUCCESS &&
          db_delete_key(in.hDB, hSet) == DB_SUCCESS) {
         std::string m = msprintf("%s: corrupt Settings/Alarm from an older build replaced by defaults",
                                  in.eq_name.c_str());
         cm_msg(MINFO, "hv_alarm_init", "%s", clip(m));
      }
   }

   status = db_create_record(in.hDB, 0, set_path.c_str(), alarm_settings_string(in).c_str());
   if (status != DB_SUCCESS) {
      std::string m = msprintf("cannot create %s (%d), HV alarms off", set_path.c_str(), status);
      cm_msg(MERROR, "hv_alarm_init", "%s", clip(m, kMaxErrMsgLen));
      return;
   }

   if (ensure_alarm_class(in) != DB_SUCCESS) {
      cm_msg(MERROR, "hv_alarm_init", "HV alarms off");
      return;
   }

   /* find our own equipment slot once, for DEVICE_DRIVER::dd_info later */
   in.eq_index = -1;
   for (int k = 0; equipment[k].name[0] != 0; k++) {
      if (equal_ustring(equipment[k].name, in.eq_name.c_str())) {
         in.eq_index = k;
         break;
      }
   }
   if (in.eq_index < 0) {
      std::string m = msprintf("%s not in the equipment table, no Polarity", in.eq_name.c_str());
      cm_msg(MERROR, "hv_alarm_init", "%s", clip(m, kMaxErrMsgLen));
   }

   in.dd_info = nullptr;
   in.editable_checked = false;
   in.pol_published.assign(in.n_ch, 0);
   in.pol_ever_published = false;
   in.last_bad_time.assign(in.n_ch, 0);
   in.triggered.assign(in.n_ch, false);
   in.off_mismatch_since.assign(in.n_ch, 0);
   in.off_mismatch_logged.assign(in.n_ch, false);
   in.deviation.assign(in.n_ch, held_t{});
   in.on_while_off.assign(in.n_ch, held_t{});
   in.comm_bad_since = 0;
   in.comm_triggered = false;
   in.last_check = std::chrono::steady_clock::now();

   /* Adopt whatever a previous instance of this frontend left triggered, so the
      normal "Clear After s" path can reset it once the condition is gone. The
      clock starts now: a restart is not evidence that the condition has been
      clear for any length of time. */
   DWORD t_init = ss_time();
   int adopted = 0;
   for (int i = 0; i < in.n_ch; i++) {
      std::string name = in.eq_name + " Ch" + std::to_string(i);
      if (alarm_is_triggered(in.hDB, name)) {
         in.triggered[i] = true;
         in.last_bad_time[i] = t_init;
         adopted++;
         /* an adopted deviation / on-while-off alarm is already raised: latch
            it, so a condition that is still there keeps it up at once rather
            than waiting out the hold after a reset (reset + re-raise) */
         if (in.drv.deviation_check || in.drv.on_while_off_check) {
            std::string text = alarm_message(in.hDB, name);
            if (in.drv.deviation_check && text.find(kDeviationReason) != std::string::npos)
               in.deviation[i] = held_t{0, t_init, true};
            if (in.drv.on_while_off_check && text.find(kOnWhileOffReason) != std::string::npos)
               in.on_while_off[i] = held_t{0, t_init, true};
         }
      }
   }
   if (alarm_is_triggered(in.hDB, in.eq_name + " Comm")) {
      in.comm_triggered = true;
      adopted++;
   }
   if (adopted > 0)
      cm_msg(MINFO, "hv_alarm_init", "adopted %d triggered alarm(s) from a previous instance",
             adopted);

   in.active = true;
   std::string m = msprintf("HV alarms active for %s, %d channels, class %s",
                            in.eq_name.c_str(), in.n_ch, in.alarm_class.c_str());
   cm_msg(MINFO, "hv_alarm_init", "%s", clip(m));
}

/// @brief the once-per-second check of one equipment
void check_instance(instance_t &in)
{
   if (!in.active)
      return;

   auto now = std::chrono::steady_clock::now();
   if (now - in.last_check < std::chrono::seconds(1))
      return;
   in.last_check = now;

   if (!equipment_enabled(in))
      return;

   /* first pass after the class driver's init, same place as the dd_info
      pickup: make mhttpd's equipment page usable again, see fix_editable() */
   if (!in.editable_checked)
      fix_editable(in);

   publish_polarity(in);

   const int n_ch = in.n_ch;
   const HNDLE hDB = in.hDB;
   const HNDLE hKeyEq = in.hKeyEq;

   /*---- settings, re-read every second so edits apply live ----*/
   BOOL al_enabled = TRUE;
   INT size = sizeof(al_enabled);
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Enabled", &al_enabled, &size, TID_BOOL, FALSE);
   if (!al_enabled)
      return;

   std::vector<float> vmax(n_ch, in.drv.default_voltage_max);
   size = sizeof(float) * n_ch;
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Voltage Max", vmax.data(), &size, TID_FLOAT, FALSE);

   std::vector<float> imax(n_ch, in.drv.default_current_max);
   size = sizeof(float) * n_ch;
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Current Max", imax.data(), &size, TID_FLOAT, FALSE);

   DWORD status_mask = in.drv.default_status_mask;
   size = sizeof(status_mask);
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Status Mask", &status_mask, &size, TID_DWORD, FALSE);
   /* the driver's own "STAT is stale" flag, never a device alarm */
   status_mask &= ~in.drv.stale_bit;

   INT clear_after = 10;
   size = sizeof(clear_after);
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Clear After s", &clear_after, &size, TID_INT32, FALSE);

   INT comm_timeout = 60;
   size = sizeof(comm_timeout);
   db_get_value(hDB, hKeyEq, "Settings/Alarm/Comm Timeout s", &comm_timeout, &size, TID_INT32, FALSE);

   /* only for a driver that asked for it: nothing else reads or creates the
      key, so an equipment without the check never sees it in ODB */
   std::vector<float> dmax;
   INT dev_hold = in.drv.default_deviation_hold_s;
   if (in.drv.deviation_check) {
      dmax.assign(n_ch, in.drv.default_deviation_max);
      size = sizeof(float) * n_ch;
      db_get_value(hDB, hKeyEq, "Settings/Alarm/Deviation Max", dmax.data(), &size, TID_FLOAT, FALSE);
      size = sizeof(dev_hold);
      db_get_value(hDB, hKeyEq, "Settings/Alarm/Deviation Hold s", &dev_hold, &size, TID_INT32, FALSE);
   }

   /*---- readings ----*/
   std::vector<float> meas(n_ch, (float) ss_nan());
   size = sizeof(float) * n_ch;
   db_get_value(hDB, hKeyEq, "Variables/Measured", meas.data(), &size, TID_FLOAT, FALSE);

   std::vector<float> curr(n_ch, (float) ss_nan());
   size = sizeof(float) * n_ch;
   db_get_value(hDB, hKeyEq, "Variables/Current", curr.data(), &size, TID_FLOAT, FALSE);

   std::vector<DWORD> stat(n_ch, 0);
   size = sizeof(DWORD) * n_ch;
   db_get_value(hDB, hKeyEq, "Variables/ChStatus", stat.data(), &size, TID_DWORD, FALSE);

   std::vector<DWORD> chstate(n_ch, 0);
   size = sizeof(DWORD) * n_ch;
   db_get_value(hDB, hKeyEq, "Variables/ChState", chstate.data(), &size, TID_DWORD, FALSE);

   std::vector<float> demand;
   if (in.drv.deviation_check) {
      demand.assign(n_ch, (float) ss_nan());
      size = sizeof(float) * n_ch;
      db_get_value(hDB, hKeyEq, "Variables/Demand", demand.data(), &size, TID_FLOAT, FALSE);
   }

   std::vector<char> names(NAME_LENGTH * n_ch, 0);
   size = NAME_LENGTH * n_ch;
   db_get_value(hDB, hKeyEq, "Settings/Names", names.data(), &size, TID_STRING, FALSE);

   DWORD t_now = ss_time();
   const DWORD clear_s = (DWORD) (clear_after < 0 ? 0 : clear_after);
   const size_t alarm_len = alarm_msg_limit(in);

   /*---- per channel ----*/
   for (int i = 0; i < n_ch; i++) {
      std::string reason;

      /* NaN means "no reading", not "over limit": every comparison with NaN is
         false anyway, this is only here to say so out loud. */
      if (!std::isnan(meas[i]) && meas[i] > vmax[i])
         reason += reason.empty() ? "over voltage" : ", over voltage";
      /* a "never read" current sentinel of -1 (caen_hv::kCurrentNeverRead)
         needs no test of its own: Current Max is a positive limit, so -1 can
         never satisfy ">" */
      if (!std::isnan(curr[i]) && curr[i] > imax[i])
         reason += reason.empty() ? "over current" : ", over current";

      /* "never polled yet" is not a status word, see stat_is_unknown() */
      const bool stat_known = !stat_is_unknown(stat[i]);
      const bool stat_usable = stat_known && !(stat[i] & in.drv.stale_bit);

      /* |Demand - Measured| only means something while the output is meant
         to be on, is on, and has arrived: not while off (the demand of a
         switched-off channel is kept, the output is 0 V), not while ramping,
         not on a stale or unknown status word. And it has to last: a unit
         can say "on, not ramping" while its read-back still trails the set
         point, so the excess must hold for Deviation Hold s without a break
         before it is raised; see held() for why it is latched after that. */
      if (in.drv.deviation_check) {
         float dev = 0.f;
         const bool over =
            chstate[i] == 1 && stat_usable &&
            in.drv.is_on(stat[i]) && !in.drv.is_ramping(stat[i]) &&
            !std::isnan(meas[i]) && !std::isnan(demand[i]) &&
            (dev = fabsf(demand[i] - meas[i])) > dmax[i];
         if (held(in.deviation[i], over, t_now, (DWORD) (dev_hold < 0 ? 0 : dev_hold), clear_s)) {
            std::string d = msprintf("%s %.0f V", kDeviationReason, dev);
            reason += reason.empty() ? d : ", " + d;
         }
      }

      /* the reverse of the "board says off" message below, and an alarm:
         voltage on an output MIDAS believes is off. Ramping is left out so a
         normal ramp-down after OFF stays quiet. */
      if (in.drv.on_while_off_check) {
         const bool on_off = chstate[i] == 0 && stat_usable &&
                             in.drv.is_on(stat[i]) && !in.drv.is_ramping(stat[i]);
         if (held(in.on_while_off[i], on_off, t_now, kOffMismatchSeconds, clear_s))
            reason += reason.empty() ? kOnWhileOffReason : std::string(", ") + kOnWhileOffReason;
      }

      DWORD bad_bits = stat_known ? (stat[i] & status_mask) : 0;
      if (bad_bits) {
         /* the driver owns the bit names, see its header */
         /* stat_brief() falls back to hex when the decoded text would eat the
            message budget, see the note in the file header */
         std::string bits = stat_brief(in, bad_bits);
         if (bits == "none")   /* mask bit above the driver's table */
            bits = msprintf("0x%x", (unsigned) bad_bits);
         reason += reason.empty() ? ("status " + bits) : (", status " + bits);
      }

      const char *name = &names[NAME_LENGTH * i];
      std::string alarm_name = in.eq_name + " Ch" + std::to_string(i);

      /* "ODB says on, board says off". cd_hv reads CHSTATE only at init
         (hv.cxx:1052-1054), so once an operator switches Variables/ChState to
         1 it stays 1 even if the channel never came up - mhttpd then shows a
         channel "on" at 0 V. On the CAEN board the usual cause is the
         channel's front switch sitting in OFF (DIS) or KILL, where the board
         answers CMD:OK to PAR:ON and does nothing; the descriptor's off_hint
         says what to check on each device. This is an operator-state problem,
         not a hardware fault, so it is a message and deliberately not a MIDAS
         alarm. */
      if (chstate[i] == 1 && stat_usable && !in.drv.is_on(stat[i])) {
         if (in.off_mismatch_since[i] == 0)
            in.off_mismatch_since[i] = t_now;
         if (!in.off_mismatch_logged[i] &&
             t_now - in.off_mismatch_since[i] >= kOffMismatchSeconds) {
            in.off_mismatch_logged[i] = true;
            /* No separate "switch bits" clause: the switch bits are already
               named by the STAT text, and the message budget has no room for
               saying it twice. */
            std::string m = msprintf(
               "%s: ChState ON but board says off (STAT %s)%s",
               name[0] ? name : alarm_name.c_str(), stat_brief(in, stat[i]).c_str(),
               in.drv.off_hint);
            cm_msg(MERROR, "hv_alarm", "%s", clip(m, kMaxErrMsgLen));
         }
      } else {
         /* episode over (or not judgeable): rearm for the next one */
         in.off_mismatch_since[i] = 0;
         in.off_mismatch_logged[i] = false;
      }

      if (!reason.empty()) {
         in.last_bad_time[i] = t_now;
         in.triggered[i] = true;
         /* Called every second on purpose: al_trigger_alarm() self-gates on the
            alarm's own "Check interval" (alarm.cxx:353-358). A private latch
            would stop re-triggering after an operator acknowledges the alarm
            while the condition is still there. */
         std::string stat_str = stat_known ? msprintf("0x%x", (unsigned) stat[i]) : "unknown";
         std::string msg = msprintf("%s: %s (V=%.0f I=%.1f STAT=%s)",
                                    name[0] ? name : alarm_name.c_str(), reason.c_str(),
                                    meas[i], curr[i], stat_str.c_str());
         al_trigger_alarm(alarm_name.c_str(), clip(msg, alarm_len),
                          in.alarm_class.c_str(), "", AT_INTERNAL);
      } else if (in.triggered[i]) {
         if (t_now - in.last_bad_time[i] >= clear_s) {
            al_reset_alarm(alarm_name.c_str());
            in.triggered[i] = false;
         }
      }
   }

   /*---- communication ----*/
   /* Two independent signals say the link is dead:
      - every channel's STAT is stale (drv.stale_bit). This shows up one poll
        period after the port stopped answering, because hv_read() writes
        Variables/ChStatus whenever the word differs (hv.cxx:283-300).
      - every Measured is NaN. Correct but slow: none of hv_read()'s change
        tests fire for NaN (ABS(NaN - x) > thr and !isnan(m) && isnan(mirror)
        are both false), so the NaN only reaches ODB through the "last update
        older than a minute" fallback, hv.cxx:210-240.
      The stale word is therefore the fast signal and the NaN the backstop; a
      word that stat_is_unknown() counts as neither. Either signal starts the same
      Comm Timeout s debounce, so that setting keeps its documented meaning -
      lower it if the alarm should come faster than a minute. */
   bool all_nan = true;
   bool all_stale = true;
   for (int i = 0; i < n_ch; i++) {
      if (!std::isnan(meas[i]))
         all_nan = false;
      if (stat_is_unknown(stat[i]) || !(stat[i] & in.drv.stale_bit))
         all_stale = false;
   }

   std::string comm_name = in.eq_name + " Comm";
   if (all_stale || all_nan) {
      if (in.comm_bad_since == 0)
         in.comm_bad_since = t_now;
      DWORD dead = t_now - in.comm_bad_since;
      if (dead >= (DWORD) (comm_timeout < 0 ? 0 : comm_timeout)) {
         in.comm_triggered = true;
         /* name the signal that fired, the two mean different things to a
            shifter: a stale status word means the port stopped answering,
            all-NaN readings can also be a driver that never got going */
         std::string msg = all_stale
            ? msprintf("status stale on all channels for %u s", (unsigned) dead)
            : msprintf("no readings from %s for %u s", in.drv.device_label, (unsigned) dead);
         al_trigger_alarm(comm_name.c_str(), clip(msg, alarm_len),
                          in.alarm_class.c_str(), "", AT_INTERNAL);
      }
   } else {
      in.comm_bad_since = 0;
      if (in.comm_triggered) {
         al_reset_alarm(comm_name.c_str());
         in.comm_triggered = false;
      }
   }
}

}  // namespace

/*---- entry points ------------------------------------------------*/

INT hv_alarm_init(const char *equipment_name, int n_channels, const hv_alarm_driver_t &drv)
{
   /* the instance of this equipment, if it was registered before; names
      compare case-insensitively, as MIDAS compares equipment names */
   instance_t *in = nullptr;
   if (equipment_name != nullptr) {
      for (auto &x : s_instances) {
         if (equal_ustring(x.eq_name.c_str(), equipment_name)) {
            in = &x;
            break;
         }
      }
   }

   if (equipment_name == nullptr || n_channels <= 0 ||
       drv.device_label == nullptr || drv.off_hint == nullptr ||
       drv.is_on == nullptr || drv.stat_text == nullptr ||
       ((drv.deviation_check || drv.on_while_off_check) && drv.is_ramping == nullptr)) {
      /* as a failed re-init always did: the old instance stops checking */
      if (in != nullptr)
         in->active = false;
      cm_msg(MERROR, "hv_alarm_init", "bad arguments, HV alarms off");
      return CM_SUCCESS;
   }

   if (in == nullptr) {
      s_instances.emplace_back();
      in = &s_instances.back();
   }
   *in = instance_t{};

   init_instance(*in, equipment_name, n_channels, drv);

   return CM_SUCCESS;
}

/*------------------------------------------------------------------*/

void hv_alarm_loop()
{
   for (auto &in : s_instances)
      check_instance(in);
}

/*------------------------------------------------------------------*/

void hv_alarm_exit()
{
   /* Alarms are deliberately *not* reset here: a tripped channel must still be
      flagged after a frontend restart. */
   for (auto &in : s_instances) {
      in.active = false;
      in.hKeyEq = 0;
      in.dd_info = nullptr;
   }
   s_instances.clear();
}
