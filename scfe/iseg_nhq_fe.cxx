/********************************************************************\

  Name:         iseg_nhq_fe.cxx

  Contents:     MIDAS device driver for an iseg NHQ 208L NIM high voltage
                supply (RS232, per-character echo handshake), serving the
                stock "cd_hv" class driver with one MIDAS channel.

                Same shape as scfe/caen_hv_fe.cxx (private info struct,
                db_create_record settings string, va_arg dispatcher, rate
                limited messages, O_NONBLOCK open with a 5 s reopen backoff,
                last-good values, stale bit). The wire protocol is the one
                documented in drivers/iseg_nhq/iseg_nhq_protocol.py and
                implemented in drivers/iseg_nhq/iseg_nhq_probe.py; the parsing
                here follows those two files line by line, see the comments
                that name the Python function each piece mirrors.

  Threading:    Identical contract to caen_hv_fe.cxx. The equipment uses
                  DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP |
                  DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND
                so every command in [CMD_GET_FIRST, CMD_GET_LAST] and
                [CMD_SET_FIRST, CMD_SET_LAST] runs on MIDAS's sc_thread, and
                everything else (CMD_INIT, labels, thresholds, the direct gets
                that hv_init() issues) runs on the main thread *before*
                CMD_START creates that thread. So no two threads ever touch
                the serial port and there is no mutex. The whole info struct
                except ISEG_NHQ_FE_INFO::pol is main-thread-only until
                CMD_START and sc_thread-only afterwards; iseg_nhq_polarity()
                only loads that std::atomic and is the one entry point that is
                safe at run time.

  Channel state emulation (the NHQ has no software on/off):
                ChState ON  = D<n>=<cached demand>, read D<n> back, G<n>
                ChState OFF = D<n>=0, G<n> (the unit ramps to 0 V)
                At start and after every reconnect ChState = (D<n> != 0).
                While OFF a demand is only cached; CMD_GET_DEMAND hands out
                the unit's D<n> while ON and the cached demand while OFF, so
                Variables/Demand survives a switch-off. A frontend restart
                writes nothing: every setting cd_hv echoes back at startup
                equals what was just read and is dropped.

  Safety:       * every nonzero D write is checked against
                  min(ODB Voltage Limit, M<n>% x Vmax, "Max Voltage"), with M
                  read fresh right before the write;
                * a "? UMAX" reply (the unit clamps D to its hardware limit
                  and STORES it) is answered at once with D<n>=0;
                * D is read back after every write; a set point that did not
                  take is undone before any G goes out;
                * A<n> (autostart) is read at every connect; if it is nonzero
                  the driver stops reading S<n> (a read of S restores a
                  shut-off voltage by itself when autostart is armed) and
                  refuses every write until a reconnect reads A<n> = 0;
                * the port is flock()ed like the CLI does, so the two can
                  never interleave echo handshakes on one line.

\********************************************************************/

// cfmakeraw()/CRTSCTS live behind __USE_MISC in glibc, which -std=c++20
// (__STRICT_ANSI__) would otherwise switch off.
#define _DEFAULT_SOURCE 1

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <errno.h>

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>
#include <sys/file.h>
#include <sys/select.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <regex>
#include <string>

#include <midas.h>
#include <msystem.h>

#include "iseg_nhq_fe.h"

/*---- protocol constants ------------------------------------------*/

namespace iseg_nhq {

   /// @brief the two channel numbers an NHQ accepts (protocol CHANNELS)
   constexpr int kMinHwChannel = 1;
   constexpr int kMaxHwChannel = 2;

   /// @brief ramp speed range of V<n>, V/s (COMMANDS["V"].lo/.hi)
   constexpr int kMinRamp = 2;
   constexpr int kMaxRamp = 255;

   /// @brief T<n> bits kept in the status word: everything but DISPLAY (1)
   constexpr unsigned kTMask = 0xFEu;
   /// @brief T<n> POL bit: set = positive
   constexpr unsigned kTPolPos = 4u;

   /// @brief the A<n> bit that means "autostart"; the driver treats *any*
   ///        nonzero A<n> as armed (decision 12), this one only names it
   constexpr int kAutostartBit = 8;

   /// @brief human readable bit names, indexed by stat_bit_t; nullptr = unused
   static const char *const kStatBitName[kStatNumBits] = {
      "ON", "OFF", "MAN", "ERR", "INH", "QUA", "L2H", "H2L", "LAS", "TRP",
      "S_UNKNOWN",
      nullptr, nullptr, nullptr, nullptr, nullptr,
      nullptr,                                   // 16: T DISPLAY, dropped
      "T_MAN", "T_POS", "T_HVOFF", "T_KILL", "T_INH", "T_ERR", "T_QUA",
      "AUTOSTART", "TOT", "DSET"
   };

   /// @brief the S<n> status texts, in stat_bit_t order (bits 0..9)
   static const char *const kStatusWords[] = {
      "ON", "OFF", "MAN", "ERR", "INH", "QUA", "L2H", "H2L", "LAS", "TRP"
   };

   /// @brief ODB update thresholds handed to cd_hv. The standard series
   ///        reports whole volts and whole microamps, and cd_hv compares with
   ///        '>' (hv.cxx:216-218), so a 1 V threshold would hide every 1 V
   ///        step; cd_hv's own 20 V zero threshold would hide everything
   ///        below 20 V.
   constexpr float kThresholdVoltage = 0.5f;   ///< [V]
   constexpr float kThresholdCurrent = 0.5f;   ///< [uA]
   /// Zero threshold -1 V, i.e. off: cd_hv only counts a Measured change
   /// when ABS(measured) > zero_threshold (hv.cxx:215-218), so with any
   /// threshold >= 0 a trip to 0 V is not a change and ODB Measured keeps the
   /// old voltage until the 60 s periodic update. Only used when
   /// Settings/Zero Threshold does not exist yet (validate_odb_array).
   constexpr float kThresholdZero    = -1.0f;  ///< [V]

   /// @brief how often at most any one kind of repetitive cm_msg is emitted
   constexpr auto kLogInterval = std::chrono::seconds(30);

   /// @brief how often at most open() and the connect sequence are retried
   constexpr auto kReopenInterval = std::chrono::seconds(5);

   /// @brief echo timeout fallback when the ODB setting is nonsense [ms]
   constexpr int kDefaultEchoTimeoutMs = 300;

   /// @brief inter-character deadline for an answer line [ms], the probe's
   ///        IsegNHQ.timeout default; widened to 4 x W + 200 ms after W
   constexpr int kAnswerTimeoutMs = 1000;

   /// @brief the longest break time the unit can be set to (W), [ms]
   ///        (IsegNHQ.MAX_BREAK_S)
   constexpr int kMaxBreakMs = 255;

   /// @brief longest message body this driver hands to cm_msg
   ///
   /// MIDAS's frontend printer memcpy()s the body into a char[160] without a
   /// bound check (mfe.cxx:1357), so anything from ~159 characters up kills
   /// the frontend; same limit as caen_hv::kMaxMsgLen.
   constexpr size_t kMaxMsgLen = 120;

   /// @brief the one MIDAS channel's default name
   constexpr const char *kChannelLabel = "S5";

}  // namespace iseg_nhq

/*---- ODB settings ------------------------------------------------*/

#define ISEG_NHQ_SETTINGS_STRING "\
Port = STRING : [64] /dev/ttyUSB0\n\
Hardware Channel = INT32 : 2\n\
Max Voltage = FLOAT : 1300\n\
Echo Timeout ms = INT32 : 300\n\
"

/// @brief device settings stored in ODB
/// @note keep as fixed length struct as it maps the ODB layout
struct ISEG_NHQ_SETTINGS {
   char port[64];
   /// NHQ output the one MIDAS channel is mapped to, 1 (A) or 2 (B)
   int hw_channel;
   /// software ceiling on every set point [V], like the CLI's --max-v; <= 0
   /// switches it off (the CLI's --max-v 0)
   float max_voltage;
   /// how long to wait for the echo of each byte [ms]
   int echo_timeout_ms;
};

/*---- driver state ------------------------------------------------*/

/// @brief outcome of one command line
enum class IsegRc {
   Ok,        ///< answer (or empty write acknowledgement) in text
   NoLink,    ///< port closed / unit not connected, nothing was sent
   Echo,      ///< echo mismatch (the link was resynchronised)
   Timeout,   ///< no echo or no answer (the link was resynchronised)
   Syntax,    ///< "????"
   Wcn,       ///< "?WCN" wrong channel number
   Tot,       ///< "?TOT" the unit re-initialises itself
   Umax,      ///< "? UMAX=nnnn": the unit CLAMPED and STORED the set point
   OtherErr,  ///< any other line starting with '?'
   Parse,     ///< an answer this driver could not make sense of
   Refused    ///< not sent: S, G or a write while autostart is armed
};

struct IsegReply {
   IsegRc rc{IsegRc::NoLink};
   std::string text;     ///< stripped answer, or the error line
   float umax{0.f};      ///< the limit out of a UMAX reply
};

/// @brief the number format of the unit, learnt from the shape of D<n>
enum class IsegSeries { Unknown, Precision, Standard };

/// @brief the internal information to run the FE
struct ISEG_NHQ_FE_INFO {
   ISEG_NHQ_SETTINGS settings{};
   HNDLE hKey{};
   HNDLE hDB{};
   /// @brief "/Equipment/<eq>", from the settings key's path; "" if unknown
   std::string eq_path;
   int num_channels{0};

   // --- serial link ---
   int fd{-1};                 ///< serial port, -1 when closed
   bool connected{false};      ///< fd open and locked
   bool linked{false};         ///< connect sequence (sync, W, #, A, D ...) done
   bool ever_linked{false};    ///< linked at least once this session
   bool announce_recovery{false};
   int zero_reads{0};
   std::string rxbuf;          ///< received but not yet consumed
   int break_ms{3};            ///< the unit's W

   std::chrono::steady_clock::time_point last_open{};
   bool have_last_open{false};
   int last_open_errno{0};
   std::chrono::steady_clock::time_point last_link_try{};
   bool have_last_link_try{false};

   // --- identity ---
   bool ident_valid{false};
   float vmax{0.f};            ///< Vout max from '#' [V]
   float imax_ua{0.f};         ///< Iout max from '#' [uA]
   std::string unit_no, software;
   IsegSeries series{IsegSeries::Unknown};

   // --- channel state ---
   bool autostart{false};      ///< A<n> read nonzero at the last connect
   int auto_flags{0};

   bool d_valid{false};        ///< D<n> read at least once
   float d_unit{0.f};          ///< last D<n> read [V]

   bool on{false};             ///< emulated ChState
   float demand{0.f};          ///< cached operator demand [V]
   bool demand_valid{false};

   bool m_valid{false};
   int m_pct{0};               ///< Vmax rotary switch [%]
   float odb_limit{0.f};       ///< ODB Voltage Limit, <= 0 = none
   /// @brief what Settings/Voltage Limit[0] shows in ODB as far as the driver
   ///        knows (reported at init, received or written back since);
   ///        0 until hv_init() has asked, which is when the key exists
   float odb_shown{0.f};

   bool v_valid{false};
   int v_ramp{0};              ///< V<n> [V/s]
   bool l_valid{false};
   float l_ua{0.f};            ///< L<n> [uA], 0 = no trip

   bool imon_valid{false};
   float imon{iseg_nhq::kCurrentNeverRead};

   DWORD s_bits{0};            ///< last S text as a bit, 0 = none / not polled
   bool s_valid{false};
   unsigned t_byte{0};         ///< last T<n> byte
   bool t_valid{false};
   bool tot_seen{false};       ///< ?TOT since the last status poll

   /// @brief one Settings value that hv_init() writes back to ODB at start
   ///
   /// With DF_PRIO_DEVICE, hv_init() fills Settings/* from the direct gets and
   /// then db_set_record()s them (hv.cxx:1040-1057); each of those writes
   /// fires the hotlink, so the value comes straight back as a CMD_SET_*.
   /// The direct get arms the guard with exactly the value hv_init will write
   /// back, and the first CMD_SET_* equal to it is dropped: a start-up echo
   /// never reaches the unit, whether or not the register could be read.
   struct Echo {
      bool pending{false};
      float value{0.f};
   };
   Echo echo_vlimit, echo_ilimit, echo_rampup, echo_rampdown, echo_chstate;

   /// @brief the last ChState received (start-up echo included), i.e. what
   ///        ODB shows; -1 until one arrives
   int last_chstate{-1};
   /// @brief ChState 0 arrived while the link was down after the unit had
   ///        been linked before: executed (D=0, G) as soon as it answers
   bool pending_off{false};

   /// @brief +1 / -1 / 0 unknown; written by the poll thread, read by the
   ///        main thread through iseg_nhq_polarity()
   std::atomic<int> pol{0};

   std::map<std::string, std::chrono::steady_clock::time_point> last_log{};

   ~ISEG_NHQ_FE_INFO() {
      if (fd >= 0) {
         close(fd);   // also releases the flock
      }
   }
};

/*---- small helpers -----------------------------------------------*/

/// @brief rate limiter: TRUE at most once per kLogInterval per @p kind
static bool iseg_may_log(ISEG_NHQ_FE_INFO *info, const char *kind)
{
   auto now = std::chrono::steady_clock::now();
   auto it = info->last_log.find(kind);
   if (it != info->last_log.end() && now - it->second < iseg_nhq::kLogInterval) {
      return false;
   }
   info->last_log[kind] = now;
   return true;
}

static void iseg_arm_echo(ISEG_NHQ_FE_INFO::Echo &e, float value)
{
   e.pending = true;
   e.value = value;
}

/// @brief TRUE (once) when @p value is hv_init()'s write-back of what the
///        direct get reported; see ISEG_NHQ_FE_INFO::Echo
static bool iseg_startup_echo(ISEG_NHQ_FE_INFO::Echo &e, float value)
{
   if (!e.pending) {
      return false;
   }
   e.pending = false;
   return value == e.value;
}

/// @brief cm_msg whose body can never overrun MIDAS's 160-byte printer
///        buffer (mfe.cxx:1357); see CAEN_HV_MSG. Every format below also
///        carries explicit precisions on its runtime strings.
#define ISEG_MSG(type, fmt, ...)                                               \
   do {                                                                        \
      char iseg_body_[iseg_nhq::kMaxMsgLen + 1];                               \
      snprintf(iseg_body_, sizeof(iseg_body_), fmt __VA_OPT__(,) __VA_ARGS__); \
      cm_msg(type, "iseg_nhq_fe", "%s", iseg_body_);                           \
   } while (0)

static int iseg_echo_timeout_ms(const ISEG_NHQ_FE_INFO *info)
{
   int ms = info->settings.echo_timeout_ms > 0 ? info->settings.echo_timeout_ms
                                               : iseg_nhq::kDefaultEchoTimeoutMs;
   // IsegNHQ._adopt_break_time: never shorter than one break time + 200 ms
   return std::max(ms, info->break_ms + 200);
}

static int iseg_answer_timeout_ms(const ISEG_NHQ_FE_INFO *info)
{
   return std::max(iseg_nhq::kAnswerTimeoutMs, 4 * info->break_ms + 200);
}

static std::string iseg_strip(const std::string &s)
{
   size_t b = 0, e = s.size();
   while (b < e && (unsigned char) s[b] <= ' ') {
      ++b;
   }
   while (e > b && (unsigned char) s[e - 1] <= ' ') {
      --e;
   }
   return s.substr(b, e - b);
}

static int iseg_hwch(const ISEG_NHQ_FE_INFO *info)
{
   return info->settings.hw_channel;
}

// --- public status helpers, declared in iseg_nhq_fe.h ---

const char *iseg_nhq::stat_bit_name(int bit)
{
   if (bit < 0 || bit >= iseg_nhq::kStatNumBits || !iseg_nhq::kStatBitName[bit]) {
      return "?";
   }
   return iseg_nhq::kStatBitName[bit];
}

std::string iseg_nhq::stat_text(DWORD stat)
{
   std::string out;
   for (int bit = 0; bit < iseg_nhq::kStatNumBits; bit++) {
      if ((stat & (1u << bit)) && iseg_nhq::kStatBitName[bit]) {
         if (!out.empty()) {
            out += "|";
         }
         out += iseg_nhq::kStatBitName[bit];
      }
   }
   if (stat & iseg_nhq::kStatStale) {
      out += out.empty() ? "STALE" : "|STALE";
   }
   return out.empty() ? std::string("none") : out;
}

bool iseg_nhq::stat_is_ramping(DWORD stat)
{
   return (stat & ((1u << kStatL2H) | (1u << kStatH2L))) != 0;
}

bool iseg_nhq::stat_is_on(DWORD stat)
{
   if (stat_is_ramping(stat)) {
      return true;
   }
   return (stat & (1u << kStatOn)) && (stat & (1u << kStatDSet));
}

int iseg_nhq_polarity(void *info, int ch)
{
   ISEG_NHQ_FE_INFO *hv = (ISEG_NHQ_FE_INFO *) info;
   if (!hv || ch < 0 || ch >= hv->num_channels) {
      return 0;
   }
   return hv->pol.load();
}

/// @brief the "no value" answer for the command's slot
///
/// @warning CMD_GET_STATUS and CMD_GET_CHSTATE hand us a @c float* that really
///          points at a @c DWORD (hv.cxx:159-170, 1031-1037), so those two
///          must never receive a NaN; see caen_hv_write_invalid().
static void iseg_write_invalid(float *pvalue, INT cmd)
{
   if (cmd == CMD_GET_STATUS) {
      *reinterpret_cast<DWORD *>(pvalue) = iseg_nhq::kStatStale;
   } else if (cmd == CMD_GET_CHSTATE) {
      *reinterpret_cast<DWORD *>(pvalue) = 0u;
   } else {
      *pvalue = (float) ss_nan();
   }
}

/*---- reply parsing (mirrors iseg_nhq_protocol.py) ----------------*/

/// @brief parse_number(): "+12345-01" = 1234.5, "+1234" = 1234, "0050" = 50
/// @param has_exp optional; TRUE when the exponent group was present, which
///        is how series_of_reply() tells the precision series
static bool iseg_parse_number(const std::string &text, double *out, bool *has_exp = nullptr)
{
   static const std::regex re(R"(^([+-]?)(\d+(?:\.\d*)?)([+-]\d+)?$)");
   std::smatch m;
   const std::string s = iseg_strip(text);
   if (!std::regex_match(s, m, re)) {
      return false;
   }
   double value = strtod(m[2].str().c_str(), nullptr);
   if (m[3].matched) {
      value *= std::pow(10.0, atoi(m[3].str().c_str()));
   }
   *out = (m[1].str() == "-") ? -value : value;
   if (has_exp) {
      *has_exp = m[3].matched;
   }
   return true;
}

/// @brief parse_current(): mantissa and signed exponent in amperes, "0294-6"
static bool iseg_parse_current(const std::string &text, double *amps)
{
   static const std::regex re(R"(^([+-]?)(\d+(?:\.\d*)?)([+-]\d+)$)");
   std::smatch m;
   const std::string s = iseg_strip(text);
   if (!std::regex_match(s, m, re)) {
      return false;
   }
   double value = strtod(m[2].str().c_str(), nullptr) * std::pow(10.0, atoi(m[3].str().c_str()));
   *amps = (m[1].str() == "-") ? -value : value;
   return true;
}

/// @brief int(text) as Python does it for W, A, T, M, N, V: "003" = 3
static bool iseg_parse_int(const std::string &text, int *out)
{
   const std::string s = iseg_strip(text);
   if (s.empty()) {
      return false;
   }
   char *end = nullptr;
   long v = strtol(s.c_str(), &end, 10);   // base 10: "010" is ten, not octal
   if (end == s.c_str() || *end != '\0') {
      return false;
   }
   *out = (int) v;
   return true;
}

/// @brief parse_quantity(): "8000V" / "6kV" / "1000uA" into V or A
static bool iseg_parse_quantity(const std::string &text, double *out)
{
   static const std::regex re(R"(^\s*([0-9.]+)\s*([a-zA-Z]*)\s*$)");
   std::smatch m;
   if (!std::regex_match(text, m, re)) {
      return false;
   }
   std::string unit = m[2].str();
   for (auto &c : unit) {
      c = (char) tolower((unsigned char) c);
   }
   static const std::map<std::string, double> scale = {
      {"", 1.0}, {"v", 1.0}, {"kv", 1e3}, {"mv", 1e-3},
      {"a", 1.0}, {"ma", 1e-3}, {"ua", 1e-6}, {"na", 1e-9}
   };
   auto it = scale.find(unit);
   if (it == scale.end()) {
      return false;
   }
   *out = strtod(m[1].str().c_str(), nullptr) * it->second;
   return true;
}

/// @brief parse_status_reply(): "S2=ON " (bench) or a bare "ON " (manual)
/// @return FALSE when the prefix names the other channel, i.e. the answers
///         have slipped by one command
static bool iseg_parse_status(const std::string &text, int hwch, DWORD *bits,
                              std::string *word_out = nullptr)
{
   std::string body = iseg_strip(text);
   for (int other = iseg_nhq::kMinHwChannel; other <= iseg_nhq::kMaxHwChannel; other++) {
      char prefix[8];
      snprintf(prefix, sizeof(prefix), "S%d=", other);
      if (body.rfind(prefix, 0) == 0) {
         if (other != hwch) {
            return false;
         }
         body = iseg_strip(body.substr(strlen(prefix)));
         break;
      }
   }
   *bits = 1u << iseg_nhq::kStatSUnknown;
   for (int i = 0; i < 10; i++) {
      if (body == iseg_nhq::kStatusWords[i]) {
         *bits = 1u << i;
         break;
      }
   }
   if (word_out) {
      *word_out = body;
   }
   return true;
}

/// @brief is_error() + parse_umax_limit(): classify an answer line
static IsegRc iseg_classify(const std::string &answer, float *umax)
{
   const std::string s = iseg_strip(answer);
   if (s.empty() || s[0] != '?') {
      return IsegRc::Ok;
   }
   // "? UMAX=nnnn" in the manual, "? UMAX@=5600" on the bench 208L
   static const std::regex re(R"(^\?\s*UMAX[^=]*=\s*(\d+))");
   std::smatch m;
   if (std::regex_search(s, m, re)) {
      *umax = (float) strtod(m[1].str().c_str(), nullptr);
      return IsegRc::Umax;
   }
   if (s.rfind("????", 0) == 0) {
      return IsegRc::Syntax;
   }
   if (s.rfind("?WCN", 0) == 0) {
      return IsegRc::Wcn;
   }
   if (s.rfind("?TOT", 0) == 0) {
      return IsegRc::Tot;
   }
   return IsegRc::OtherErr;
}

/*---- serial port -------------------------------------------------*/

static bool iseg_fatal_errno(int err)
{
   return err == ENXIO || err == EIO || err == ENODEV ||
          err == EBADF || err == EPIPE || err == ENOENT;
}

/// @brief close the port (and its lock) and remember that we lost it
static void iseg_port_lost(ISEG_NHQ_FE_INFO *info, const char *what, int err)
{
   bool was_connected = info->connected;
   if (info->fd >= 0) {
      close(info->fd);
      info->fd = -1;
   }
   info->connected = false;
   info->linked = false;
   info->announce_recovery = true;
   info->zero_reads = 0;
   info->rxbuf.clear();
   info->last_open = std::chrono::steady_clock::now();
   info->have_last_open = true;

   if (was_connected && iseg_may_log(info, "lost")) {
      ISEG_MSG(MERROR, "HV %.40s lost (%.20s, errno %d), retry %ds",
               info->settings.port, what, err,
               (int) iseg_nhq::kReopenInterval.count());
   }
}

/// @brief open and lock the serial port if it is not open, honouring the
///        retry delay. Does *not* talk to the unit; see iseg_link_up().
/// @param quiet suppress the "cannot open" message (CMD_INIT reports itself)
static bool iseg_port_open(ISEG_NHQ_FE_INFO *info, bool quiet = false)
{
   if (info->fd >= 0) {
      return true;
   }

   auto now = std::chrono::steady_clock::now();
   if (info->have_last_open && now - info->last_open < iseg_nhq::kReopenInterval) {
      return false;
   }
   info->last_open = now;
   info->have_last_open = true;

   int fd = open(info->settings.port, O_RDWR | O_NOCTTY | O_NONBLOCK);
   if (fd < 0) {
      info->last_open_errno = errno;
      if (!quiet && iseg_may_log(info, "open")) {
         ISEG_MSG(MERROR, "cannot open HV %.40s: %.40s",
                  info->settings.port, strerror(errno));
      }
      return false;
   }

   // The same exclusive advisory lock the CLI takes (iseg_nhq_probe.py
   // IsegNHQ.open), so the frontend and a hand-run CLI can never interleave
   // echo handshakes on one line. Released by close().
   if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      int err = errno;
      close(fd);
      info->last_open_errno = err;
      if (iseg_may_log(info, "busy")) {
         if (err == EWOULDBLOCK) {
            ISEG_MSG(MERROR, "HV %.40s in use by another program (iseg CLI?), retry %ds",
                     info->settings.port, (int) iseg_nhq::kReopenInterval.count());
         } else {
            ISEG_MSG(MERROR, "flock %.40s: %.40s", info->settings.port, strerror(err));
         }
      }
      return false;
   }

   int fl = fcntl(fd, F_GETFL, 0);
   if (fl >= 0) {
      fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);
   }

   // raw 9600 8N1, no flow control (IsegNHQ._configure)
   struct termios tio;
   memset(&tio, 0, sizeof(tio));
   if (tcgetattr(fd, &tio) != 0) {
      if (iseg_may_log(info, "termios")) {
         ISEG_MSG(MERROR, "tcgetattr %.40s: errno %d", info->settings.port, errno);
      }
      close(fd);
      return false;
   }
   cfmakeraw(&tio);
   cfsetispeed(&tio, B9600);
   cfsetospeed(&tio, B9600);
   tio.c_cflag &= ~(PARENB | PARODD | CSTOPB | CSIZE | CRTSCTS);
   tio.c_cflag |= CS8 | CLOCAL | CREAD;
   tio.c_iflag &= ~(IXON | IXOFF | IXANY);
   tio.c_cc[VMIN] = 0;
   tio.c_cc[VTIME] = 0;
   if (tcsetattr(fd, TCSANOW, &tio) != 0) {
      if (iseg_may_log(info, "termios")) {
         ISEG_MSG(MERROR, "tcsetattr %.40s: errno %d", info->settings.port, errno);
      }
      close(fd);
      return false;
   }
   tcflush(fd, TCIOFLUSH);

   info->fd = fd;
   info->connected = true;
   info->linked = false;         // a new fd may be a different unit
   info->zero_reads = 0;
   info->rxbuf.clear();
   info->have_last_link_try = false;   // run the connect sequence right away
   return true;
}

/// @brief wait up to @p ms for input and read what is there
/// @return >0 bytes appended to @p out, 0 nothing arrived, -1 the port was lost
static int iseg_read_some(ISEG_NHQ_FE_INFO *info, int ms, std::string *out)
{
   if (info->fd < 0) {
      return -1;
   }
   if (ms < 0) {
      ms = 0;
   }
   for (;;) {
      fd_set readfds;
      FD_ZERO(&readfds);
      FD_SET(info->fd, &readfds);
      timeval timeout{(time_t) (ms / 1000), (suseconds_t) ((ms % 1000) * 1000)};
      int n = select(info->fd + 1, &readfds, NULL, NULL, &timeout);
      if (n < 0) {
         if (errno == EINTR) {
            continue;
         }
         if (iseg_fatal_errno(errno)) {
            iseg_port_lost(info, "select", errno);
            return -1;
         }
         return 0;
      }
      if (n == 0) {
         return 0;
      }
      char buf[256];
      ssize_t rd = read(info->fd, buf, sizeof(buf));
      if (rd < 0) {
         if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            return 0;
         }
         if (iseg_fatal_errno(errno)) {
            iseg_port_lost(info, "read", errno);
            return -1;
         }
         return 0;
      }
      if (rd == 0) {
         // readable but empty: a hung-up tty (unplugged adapter, emulator
         // gone). Give it a couple of chances, as caen_hv_read_line() does.
         if (++info->zero_reads >= 3) {
            iseg_port_lost(info, "read returned 0 bytes", 0);
            return -1;
         }
         return 0;
      }
      info->zero_reads = 0;
      out->append(buf, (size_t) rd);
      return (int) rd;
   }
}

/// @brief write all of @p data, bounded by the echo timeout
static bool iseg_write_all(ISEG_NHQ_FE_INFO *info, const char *data, size_t len)
{
   auto deadline = std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(iseg_echo_timeout_ms(info));
   size_t done = 0;
   while (done < len) {
      if (info->fd < 0) {
         return false;
      }
      auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
         return false;
      }
      auto left = std::chrono::duration_cast<std::chrono::microseconds>(deadline - now).count();
      fd_set writefds;
      FD_ZERO(&writefds);
      FD_SET(info->fd, &writefds);
      timeval timeout{(time_t) (left / 1000000), (suseconds_t) (left % 1000000)};
      int n = select(info->fd + 1, NULL, &writefds, NULL, &timeout);
      if (n < 0) {
         if (errno == EINTR) {
            continue;
         }
         if (iseg_fatal_errno(errno)) {
            iseg_port_lost(info, "select for write", errno);
         }
         return false;
      }
      if (n == 0) {
         continue;
      }
      ssize_t w = write(info->fd, data + done, len - done);
      if (w > 0) {
         done += (size_t) w;
         continue;
      }
      if (w < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
         continue;
      }
      if (w < 0 && iseg_fatal_errno(errno)) {
         iseg_port_lost(info, "write", errno);
      }
      return false;
   }
   return true;
}

/// @brief IsegNHQ.drain(): throw away input until the line is quiet
///
/// Two patiences: in the middle of a line (something arrived, no '\n' yet)
/// wait as long as the slowest break time allows (4 x 255 ms); otherwise
/// 4 x W + 50 ms of silence ends it.
/// @param cap_ms total budget, < 0 for the default max(1.5 s, 2 x long)
static void iseg_drain(ISEG_NHQ_FE_INFO *info, int cap_ms = -1)
{
   info->rxbuf.clear();
   const int short_ms = 4 * info->break_ms + 50;
   const int long_ms = std::max(short_ms, 4 * iseg_nhq::kMaxBreakMs);
   if (cap_ms < 0) {
      cap_ms = std::max(1500, 2 * long_ms);
   }
   using clk = std::chrono::steady_clock;
   const auto cap = clk::now() + std::chrono::milliseconds(cap_ms);
   auto idle_end = clk::now() + std::chrono::milliseconds(short_ms);
   std::string seen;
   for (;;) {
      auto now = clk::now();
      auto until = std::min(cap, idle_end);
      if (now >= until) {
         return;
      }
      int ms = (int) std::chrono::duration_cast<std::chrono::milliseconds>(until - now).count();
      std::string data;
      int n = iseg_read_some(info, std::max(ms, 1), &data);
      if (n < 0) {
         return;   // port lost
      }
      if (n == 0) {
         continue;
      }
      seen += data;
      bool mid_line = seen.empty() || seen.back() != '\n';
      idle_end = clk::now() + std::chrono::milliseconds(mid_line ? long_ms : short_ms);
   }
}

/// @brief IsegNHQ.sync(): a bare "\r\n" without the handshake, then drain
///
/// The only recovery the manual offers. Note it terminates whatever partial
/// line the unit holds, so after an echo mismatch in the middle of
/// "D2=1230" the unit executes the prefix it received ("D2=12"): always a
/// smaller magnitude than the set point asked for (a prefix has fewer
/// digits). The write path reads D back after every write for that reason.
static void iseg_sync(ISEG_NHQ_FE_INFO *info, int cap_ms = -1)
{
   if (info->fd < 0) {
      return;
   }
   if (!iseg_write_all(info, "\r\n", 2)) {
      return;
   }
   iseg_drain(info, cap_ms);
}

/// @brief IsegNHQ._send_echoed(): one byte at a time, wait for each echo
static IsegRc iseg_send_echoed(ISEG_NHQ_FE_INFO *info, const std::string &data)
{
   const int echo_ms = iseg_echo_timeout_ms(info);
   for (size_t i = 0; i < data.size(); i++) {
      if (!iseg_write_all(info, &data[i], 1)) {
         return info->fd < 0 ? IsegRc::NoLink : IsegRc::Timeout;
      }
      using clk = std::chrono::steady_clock;
      auto deadline = clk::now() + std::chrono::milliseconds(echo_ms);
      std::string echo;
      while (echo.empty()) {
         auto now = clk::now();
         if (now >= deadline) {
            return IsegRc::Timeout;
         }
         int ms = (int) std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count();
         int n = iseg_read_some(info, std::max(ms, 1), &echo);
         if (n < 0) {
            return IsegRc::NoLink;
         }
      }
      if (echo[0] != data[i]) {
         return IsegRc::Echo;
      }
      if (echo.size() > 1) {
         // the unit answered faster than we read; keep the surplus
         info->rxbuf.append(echo, 1, std::string::npos);
      }
   }
   return IsegRc::Ok;
}

/// @brief IsegNHQ._take_line(): pop one '\n'-terminated line, empty ones too
static std::optional<std::string> iseg_take_line(ISEG_NHQ_FE_INFO *info)
{
   size_t pos = info->rxbuf.find('\n');
   if (pos == std::string::npos) {
      return std::nullopt;
   }
   std::string line = info->rxbuf.substr(0, pos);
   while (!line.empty() && line.back() == '\r') {
      line.pop_back();
   }
   info->rxbuf.erase(0, pos + 1);
   return line;
}

/// @brief IsegNHQ._read_line(): @p idle_ms is an inter-character deadline
/// @return the line; nullopt on silence (@p lost set if the port went away)
static std::optional<std::string> iseg_read_line(ISEG_NHQ_FE_INFO *info, int idle_ms, bool *lost)
{
   *lost = false;
   if (auto line = iseg_take_line(info)) {
      return line;
   }
   for (;;) {
      std::string data;
      int n = iseg_read_some(info, idle_ms, &data);
      if (n < 0) {
         *lost = true;
         return std::nullopt;
      }
      if (n == 0) {
         return std::nullopt;   // nothing for a whole idle period
      }
      info->rxbuf += data;
      if (auto line = iseg_take_line(info)) {
         return line;
      }
      if (info->rxbuf.size() > 1024) {
         info->rxbuf.clear();   // runaway, garbage
         return std::nullopt;
      }
   }
}

/// @brief IsegNHQ._read_answer(): the answer, skipping empty lines before it;
///        a write command answers only with empty lines, so after the first
///        empty one the search gives up after max(4 x W, 50 ms) of silence and
///        returns "" (the write acknowledgement)
static std::optional<std::string> iseg_read_answer(ISEG_NHQ_FE_INFO *info, bool *lost)
{
   auto line = iseg_read_line(info, iseg_answer_timeout_ms(info), lost);
   if (!line) {
      return std::nullopt;
   }
   if (!line->empty()) {
      return line;
   }
   const int idle = std::max(4 * info->break_ms, 50);
   for (;;) {
      auto next = iseg_read_line(info, idle, lost);
      if (!next) {
         if (*lost) {
            return std::nullopt;
         }
         return std::string();
      }
      if (!next->empty()) {
         return next;
      }
   }
}

/// @brief IsegNHQ.command(): flush, send with echo, read the answer, classify
///
/// Needs an open port; does not run the connect sequence (iseg_cmd() does).
/// On an echo error or a timeout the link is resynchronised before
/// returning, as the probe does; on ?TOT too.
static IsegReply iseg_raw_command(ISEG_NHQ_FE_INFO *info, const std::string &text)
{
   IsegReply r;
   if (info->fd < 0) {
      r.rc = IsegRc::NoLink;
      return r;
   }
   // whatever is in the port now belongs to a command that already finished
   info->rxbuf.clear();
   tcflush(info->fd, TCIFLUSH);

   IsegRc rc = iseg_send_echoed(info, text + "\r\n");
   if (rc != IsegRc::Ok) {
      if (rc != IsegRc::NoLink) {
         iseg_sync(info, 4 * info->break_ms + 150);
      }
      r.rc = info->fd < 0 ? IsegRc::NoLink : rc;
      return r;
   }

   bool lost = false;
   auto answer = iseg_read_answer(info, &lost);
   if (!answer) {
      if (!lost) {
         iseg_sync(info, 4 * info->break_ms + 150);
      }
      r.rc = info->fd < 0 ? IsegRc::NoLink : IsegRc::Timeout;
      return r;
   }

   r.text = iseg_strip(*answer);
   r.rc = iseg_classify(r.text, &r.umax);
   if (r.rc == IsegRc::Tot) {
      // the unit is re-initialising; put the link back in step
      info->tot_seen = true;
      if (iseg_may_log(info, "tot")) {
         ISEG_MSG(MERROR, "unit answered ?TOT to '%.12s' (it re-initialises itself), resyncing",
                  text.c_str());
      }
      iseg_sync(info, 4 * info->break_ms + 150);
   }
   return r;
}

static const char *iseg_rc_name(IsegRc rc)
{
   switch (rc) {
   case IsegRc::Ok:       return "ok";
   case IsegRc::NoLink:   return "no link";
   case IsegRc::Echo:     return "echo mismatch";
   case IsegRc::Timeout:  return "no answer";
   case IsegRc::Syntax:   return "????";
   case IsegRc::Wcn:      return "?WCN";
   case IsegRc::Tot:      return "?TOT";
   case IsegRc::Umax:     return "? UMAX";
   case IsegRc::OtherErr: return "error reply";
   case IsegRc::Parse:    return "unparseable";
   case IsegRc::Refused:  return "autostart";
   }
   return "?";
}

static bool iseg_connect(ISEG_NHQ_FE_INFO *info);
static void iseg_follow_ceiling(ISEG_NHQ_FE_INFO *info);

/// @brief make sure the port is open and the connect sequence has run
///
/// Rate limited: at most one open() and one connect sequence per
/// kReopenInterval, so a dead unit costs the poll loop nothing between
/// attempts.
static bool iseg_link_up(ISEG_NHQ_FE_INFO *info, bool quiet = false)
{
   if (!info->connected && !iseg_port_open(info, quiet)) {
      return false;
   }
   if (info->linked) {
      return true;
   }
   auto now = std::chrono::steady_clock::now();
   if (info->have_last_link_try && now - info->last_link_try < iseg_nhq::kReopenInterval) {
      return false;
   }
   info->last_link_try = now;
   info->have_last_link_try = true;
   return iseg_connect(info);
}

/// @brief one command through the connect logic, with error reporting
///
/// @param retry_on_echo send once more after an echo mismatch; only for
///        genuine reads (a write is never repeated: its first, partial, copy
///        may have been executed, see iseg_sync())
static IsegReply iseg_cmd(ISEG_NHQ_FE_INFO *info, const std::string &text,
                          bool retry_on_echo)
{
   IsegReply r;
   if (!iseg_link_up(info)) {
      r.rc = IsegRc::NoLink;
      return r;
   }
   // The one place every command passes after the connect sequence (which
   // may just have found A<n> armed): with autostart armed nothing but the
   // genuine reads goes out. S<n> can restore a shut-off voltage by itself,
   // G<n> starts a ramp, and every write moves the output at once.
   if (info->autostart && (text.find('=') != std::string::npos ||
                           text[0] == 'S' || text[0] == 'G')) {
      r.rc = IsegRc::Refused;
      return r;
   }
   r = iseg_raw_command(info, text);
   if (r.rc == IsegRc::Echo && retry_on_echo) {
      r = iseg_raw_command(info, text);
   }

   switch (r.rc) {
   case IsegRc::Ok:
   case IsegRc::Umax:          // the caller reports and handles it
   case IsegRc::NoLink:        // iseg_port_lost() has reported it
   case IsegRc::Refused:       // the caller reports it
      break;
   case IsegRc::Tot:           // reported by iseg_raw_command()
   case IsegRc::Echo:
   case IsegRc::Timeout:
      // the two sides may be out of step, or the unit restarted: run the
      // whole connect sequence (with its A<n> check) before the next command
      info->linked = false;
      info->announce_recovery = true;
      if (r.rc != IsegRc::Tot && iseg_may_log(info, "link")) {
         ISEG_MSG(MERROR, "HV %.40s: %.14s on '%.12s', reconnecting",
                  info->settings.port, iseg_rc_name(r.rc), text.c_str());
      }
      break;
   default:
      if (iseg_may_log(info, "reply")) {
         ISEG_MSG(MERROR, "unit answered '%.20s' (%.12s) to '%.12s'",
                  r.text.c_str(), iseg_rc_name(r.rc), text.c_str());
      }
      break;
   }
   return r;
}

/*---- typed commands ----------------------------------------------*/

static std::string iseg_letter(const ISEG_NHQ_FE_INFO *info, char letter)
{
   char buf[8];
   snprintf(buf, sizeof(buf), "%c%d", letter, iseg_hwch(info));
   return buf;
}

/// @brief report an answer that parsed as nothing
static void iseg_parse_failed(ISEG_NHQ_FE_INFO *info, const std::string &cmd,
                              const std::string &text)
{
   if (iseg_may_log(info, "parse")) {
      ISEG_MSG(MERROR, "cannot parse '%.20s' answered to %.8s", text.c_str(), cmd.c_str());
   }
}

/// @brief a read answering an integer (A, T, M, V, W)
static bool iseg_read_int(ISEG_NHQ_FE_INFO *info, const std::string &cmd, int *out,
                          bool raw = false)
{
   IsegReply r = raw ? iseg_raw_command(info, cmd) : iseg_cmd(info, cmd, true);
   if (r.rc != IsegRc::Ok) {
      return false;
   }
   if (!iseg_parse_int(r.text, out)) {
      iseg_parse_failed(info, cmd, r.text);
      return false;
   }
   return true;
}

/// @brief learn the series from the shape of a D / U / L answer
///        (IsegNHQ._note_series)
static void iseg_note_series(ISEG_NHQ_FE_INFO *info, bool has_exp)
{
   if (info->series == IsegSeries::Unknown) {
      info->series = has_exp ? IsegSeries::Precision : IsegSeries::Standard;
   }
}

/// @brief D<n>: the set point [V], magnitude; updates the cache
static bool iseg_read_d(ISEG_NHQ_FE_INFO *info, float *out, bool raw = false)
{
   const std::string cmd = iseg_letter(info, 'D');
   IsegReply r = raw ? iseg_raw_command(info, cmd) : iseg_cmd(info, cmd, true);
   if (r.rc != IsegRc::Ok) {
      return false;
   }
   double v = 0;
   bool has_exp = false;
   if (!iseg_parse_number(r.text, &v, &has_exp)) {
      iseg_parse_failed(info, cmd, r.text);
      return false;
   }
   iseg_note_series(info, has_exp);
   info->d_unit = (float) std::fabs(v);
   info->d_valid = true;
   *out = info->d_unit;
   return true;
}

/// @brief T<n>: the module status byte; updates the cache and the polarity
static bool iseg_read_t(ISEG_NHQ_FE_INFO *info, bool raw = false)
{
   int t = 0;
   if (!iseg_read_int(info, iseg_letter(info, 'T'), &t, raw)) {
      return false;
   }
   info->t_byte = (unsigned) t & 0xFFu;
   info->t_valid = true;
   info->pol.store((info->t_byte & iseg_nhq::kTPolPos) ? +1 : -1);
   return true;
}

/// @brief M<n>: the Vmax rotary switch in %
static bool iseg_read_m(ISEG_NHQ_FE_INFO *info, bool raw = false)
{
   int m = 0;
   if (!iseg_read_int(info, iseg_letter(info, 'M'), &m, raw)) {
      return false;
   }
   info->m_pct = m;
   info->m_valid = true;
   return true;
}

/// @brief V<n>: the ramp speed in V/s
static bool iseg_read_v(ISEG_NHQ_FE_INFO *info, bool raw = false)
{
   int v = 0;
   if (!iseg_read_int(info, iseg_letter(info, 'V'), &v, raw)) {
      return false;
   }
   info->v_ramp = v;
   info->v_valid = true;
   return true;
}

/// @brief L<n>: the current trip in uA
///
/// The standard series (the 208L) answers a count, 1 count = 1 uA on this
/// unit (bench log, S5 trip L2=450); the precision series answers amperes
/// (IsegNHQ.current_trip).
static bool iseg_read_l(ISEG_NHQ_FE_INFO *info, bool raw = false)
{
   const std::string cmd = iseg_letter(info, 'L');
   IsegReply r = raw ? iseg_raw_command(info, cmd) : iseg_cmd(info, cmd, true);
   if (r.rc != IsegRc::Ok) {
      return false;
   }
   double v = 0;
   bool has_exp = false;
   if (!iseg_parse_number(r.text, &v, &has_exp)) {
      iseg_parse_failed(info, cmd, r.text);
      return false;
   }
   iseg_note_series(info, has_exp);
   info->l_ua = (float) std::fabs(info->series == IsegSeries::Precision ? v * 1e6 : v);
   info->l_valid = true;
   return true;
}

/// @brief S<n>: the status text. NEVER call this while info->autostart.
static bool iseg_read_s(ISEG_NHQ_FE_INFO *info, std::string *word = nullptr)
{
   if (info->autostart) {
      return false;   // belt and braces: see kStatAutostart
   }
   const std::string cmd = iseg_letter(info, 'S');
   IsegReply r = iseg_cmd(info, cmd, true);
   if (r.rc != IsegRc::Ok) {
      return false;
   }
   DWORD bits = 0;
   if (!iseg_parse_status(r.text, iseg_hwch(info), &bits, word)) {
      // the answer belongs to the other channel: out of step
      info->linked = false;
      info->announce_recovery = true;
      if (iseg_may_log(info, "slip")) {
         ISEG_MSG(MERROR, "asked %.4s, got '%.16s': link out of step, reconnecting",
                  cmd.c_str(), r.text.c_str());
      }
      return false;
   }
   info->s_bits = bits;
   info->s_valid = true;
   return true;
}

/// @brief format a set point for D<n>= (format_value("D", ...))
///
/// Whole volts on the standard series (round half to even, like Python's
/// round()), two decimals for a fractional value on the precision series;
/// an integral value is written without a decimal point on both.
static std::string iseg_format_d(const ISEG_NHQ_FE_INFO *info, float volts, float *sent)
{
   double v = std::fabs((double) volts);
   char buf[32];
   if (info->series == IsegSeries::Standard || v == std::floor(v)) {
      double r = std::nearbyint(v);    // FE_TONEAREST: half to even
      snprintf(buf, sizeof(buf), "%lld", (long long) r);
      *sent = (float) r;
   } else {
      snprintf(buf, sizeof(buf), "%.2f", v);
      *sent = (float) strtod(buf, nullptr);
   }
   return buf;
}

/// @brief resolution of a D read-back [V] (SERIES_RESOLUTION_V)
static float iseg_d_resolution(const ISEG_NHQ_FE_INFO *info)
{
   return info->series == IsegSeries::Standard ? 1.0f : 0.1f;
}

/*---- connect sequence --------------------------------------------*/

/// @brief after a (re)connect: ChState = (D != 0)
///
/// The driver follows the unit. ODB's ChState is only written by cd_hv at
/// init, so whenever the adopted state differs from the last ChState the
/// driver received (what ODB shows), the operator is told; Demand writes are
/// refused while the unit is on and ChState shows 0 (iseg_set_demand).
static void iseg_adopt_state(ISEG_NHQ_FE_INFO *info)
{
   const int ch = iseg_hwch(info);
   const bool unit_on = info->d_unit > 0.f;
   const bool was_on = info->on;
   info->on = unit_on;
   if (unit_on) {
      info->demand = info->d_unit;
      info->demand_valid = true;
   }
   if (info->ever_linked && was_on && !unit_on) {
      ISEG_MSG(MERROR, "unit back with D%d=0 (power cycle?): now off, demand %.0f V kept",
               ch, (double) info->demand);
      return;
   }
   if (info->pending_off && unit_on) {
      return;   // the latched OFF runs next (iseg_service_pending)
   }
   if (info->last_chstate >= 0 && (info->last_chstate == 1) != unit_on) {
      if (unit_on) {
         ISEG_MSG(MERROR, "S5 is on at %.0f V but ChState shows OFF: set ChState (1 keeps it on)",
                  (double) info->d_unit);
      } else {
         ISEG_MSG(MERROR, "S5 is off (D%d=0) but ChState shows ON: set ChState", ch);
      }
   }
}

/// @brief sync, then learn everything that decides what the driver may do
///
/// IsegNHQ.open() plus what the frontend needs: W (timeouts), '#' (Vmax),
/// A<n> (autostart guard) before anything else about the channel, then T, M,
/// D (on state, series), V, L. S<n> is deliberately not read here.
/// @return TRUE when W, '#', A and D answered; otherwise the link stays down
///         and the next attempt comes kReopenInterval later
static bool iseg_connect(ISEG_NHQ_FE_INFO *info)
{
   const int ch = iseg_hwch(info);
   info->linked = false;

   // the sync's drain has to cover the slowest possible break time
   info->break_ms = 3;
   iseg_sync(info);
   if (info->fd < 0) {
      return false;
   }

   int w = 0;
   if (iseg_read_int(info, "W", &w, true) && w >= 0 && w <= iseg_nhq::kMaxBreakMs) {
      info->break_ms = w;
   } else if (info->fd < 0) {
      return false;
   }

   IsegReply id = iseg_raw_command(info, "#");
   if (id.rc != IsegRc::Ok) {
      if (info->fd >= 0 && iseg_may_log(info, "connect")) {
         ISEG_MSG(MERROR, "HV %.40s: no identity (%.14s), retry %ds",
                  info->settings.port, iseg_rc_name(id.rc),
                  (int) iseg_nhq::kReopenInterval.count());
      }
      return false;
   }
   {
      // parse_identity(): exactly four ';'-separated fields,
      // unit;software;Vmax;Imax, e.g. "481198;2.06;8000V;1000uA"
      std::string parts[4];
      int n = 0;
      size_t start = 0;
      for (;;) {
         size_t semi = id.text.find(';', start);
         std::string field = id.text.substr(start, semi == std::string::npos
                                                       ? std::string::npos : semi - start);
         if (n < 4) {
            parts[n] = iseg_strip(field);
         }
         n++;
         if (semi == std::string::npos) {
            break;
         }
         start = semi + 1;
      }
      double vmax = 0, imax = 0;
      if (n != 4 || !iseg_parse_quantity(parts[2], &vmax) ||
          !iseg_parse_quantity(parts[3], &imax) || vmax <= 0) {
         iseg_parse_failed(info, "#", id.text);
         return false;
      }
      info->unit_no = parts[0];
      info->software = parts[1];
      info->vmax = (float) vmax;
      info->imax_ua = (float) (imax * 1e6);
      info->ident_valid = true;
   }

   // A<n> before anything that could touch S<n>
   int a = 0;
   if (!iseg_read_int(info, iseg_letter(info, 'A'), &a, true)) {
      if (info->fd >= 0 && iseg_may_log(info, "connect")) {
         ISEG_MSG(MERROR, "A%d not readable: autostart unknown, not connecting", ch);
      }
      return false;
   }
   info->auto_flags = a;
   bool was_autostart = info->autostart;
   info->autostart = (a != 0);
   if (info->autostart) {
      info->s_bits = 0;
      info->s_valid = false;
      if (!was_autostart || iseg_may_log(info, "autostart")) {
         ISEG_MSG(MERROR, "A%d=%d%.12s: S%d not polled, writes refused. Stop scfe, CLI 'set A 0'",
                  ch, a, (a & iseg_nhq::kAutostartBit) ? " autostart" : " flags", ch);
      }
   } else if (was_autostart) {
      ISEG_MSG(MINFO, "A%d=0: autostart cleared, full control restored", ch);
   }

   float d = 0.f;
   if (!iseg_read_d(info, &d, true)) {
      if (info->fd >= 0 && iseg_may_log(info, "connect")) {
         ISEG_MSG(MERROR, "D%d not readable: channel state unknown, not connecting", ch);
      }
      return false;
   }

   // best effort: a missing one only disables what depends on it
   iseg_read_t(info, true);
   iseg_read_m(info, true);
   iseg_read_v(info, true);
   iseg_read_l(info, true);
   if (info->fd < 0) {
      return false;
   }

   iseg_adopt_state(info);
   iseg_follow_ceiling(info);   // no-op during CMD_INIT (odb_shown still 0)

   const bool recovered = info->announce_recovery;
   info->linked = true;
   info->ever_linked = true;
   info->announce_recovery = false;

   ISEG_MSG(MINFO, "%.9s NHQ %.8s sw %.5s %.0fV/%.0fuA ch%d D=%.0fV %.3s V=%d L=%.0fuA M=%d%% A=%d",
            recovered ? "back:" : "iseg",
            info->unit_no.c_str(), info->software.c_str(),
            (double) info->vmax, (double) info->imax_ua, ch,
            (double) info->d_unit, info->on ? "ON" : "OFF",
            info->v_valid ? info->v_ramp : -1, info->l_valid ? (double) info->l_ua : -1.0,
            info->m_valid ? info->m_pct : -1, info->auto_flags);
   return true;
}

/*---- limits ------------------------------------------------------*/

/// @brief the part of the ceiling the operator cannot raise from ODB:
///        min(M% x Vmax, Max Voltage), from the cache; -1 if neither is known
static float iseg_fixed_ceiling(const ISEG_NHQ_FE_INFO *info)
{
   float c = -1.f;
   if (info->ident_valid && info->m_valid) {
      c = info->vmax * (float) info->m_pct / 100.f;
   }
   if (info->settings.max_voltage > 0.f) {
      c = c < 0.f ? info->settings.max_voltage : std::min(c, info->settings.max_voltage);
   }
   return c;
}

/// @brief "set by Max Voltage (Vmax switch 70% = 5600 V)" for a message
static std::string iseg_ceiling_source(const ISEG_NHQ_FE_INFO *info)
{
   char buf[80];
   const bool hw = info->ident_valid && info->m_valid;
   const float hwv = hw ? info->vmax * (float) info->m_pct / 100.f : -1.f;
   const bool by_max = info->settings.max_voltage > 0.f && (!hw || info->settings.max_voltage <= hwv);
   if (by_max && hw) {
      snprintf(buf, sizeof(buf), "set by Max Voltage (Vmax switch %d%% = %.0f V)", info->m_pct,
               (double) hwv);
   } else if (by_max) {
      snprintf(buf, sizeof(buf), "set by Max Voltage (Vmax switch not read)");
   } else {
      snprintf(buf, sizeof(buf), "set by Vmax switch %d%% (Max Voltage %.0f V)", info->m_pct,
               (double) info->settings.max_voltage);
   }
   return buf;
}

/// @brief write @p volts into /Equipment/<eq>/Settings/Voltage Limit[0]
///
/// Called only on sc_thread (after hv_init() created the key). The ODB is
/// locked internally, so this is safe from here. The write fires cd_hv's
/// hotlink on the main thread, which queues CMD_SET_VOLTAGE_LIMIT(@p volts)
/// back to us; info->odb_limit is set to @p volts first, so that comes in as
/// "unchanged" and is accepted silently (no loop, no second message).
static void iseg_write_odb_limit(ISEG_NHQ_FE_INFO *info, float volts)
{
   info->odb_limit = volts;
   info->odb_shown = volts;
   if (info->eq_path.empty()) {
      return;
   }
   HNDLE hLim;
   const std::string path = info->eq_path + "/Settings/Voltage Limit";
   if (db_find_key(info->hDB, 0, path.c_str(), &hLim) != DB_SUCCESS) {
      return;
   }
   db_set_data_index(info->hDB, hLim, &volts, sizeof(volts), 0, TID_FLOAT);
}

/// @brief after an M<n> read: if the fixed ceiling dropped below what ODB
///        shows (Vmax switch turned down), lower ODB's Voltage Limit to it
///
/// Never below the D of a unit that is on: a limit under D would let cd_hv's
/// demand clamp (hv.cxx:428-436) ramp the PMT down at the next Demand write.
/// Only once hv_init() has asked (odb_shown > 0), so only on sc_thread.
static void iseg_follow_ceiling(ISEG_NHQ_FE_INFO *info)
{
   const float fixed = iseg_fixed_ceiling(info);
   if (info->odb_shown <= 0.f || fixed < 0.f || info->odb_shown <= fixed) {
      return;
   }
   float target = fixed;
   if (info->on && info->d_valid && info->d_unit > target) {
      target = info->d_unit;
   }
   if (target >= info->odb_shown) {
      return;
   }
   ISEG_MSG(MINFO, "S5: ceiling now %.0f V %.60s: Voltage Limit %.0f -> %.0f V",
            (double) fixed, iseg_ceiling_source(info).c_str(), (double) info->odb_shown,
            (double) target);
   iseg_write_odb_limit(info, target);
}

/// @brief the voltage ceiling: min(ODB Voltage Limit, M% x Vmax, Max Voltage)
/// @param refresh read M<n> fresh from the unit ('#' comes from the connect)
/// @return the ceiling [V], or -1 when the hardware limit is unknown
static float iseg_ceiling(ISEG_NHQ_FE_INFO *info, bool refresh)
{
   if (refresh) {
      if (!iseg_link_up(info)) {
         return -1.f;
      }
      if (!iseg_read_m(info)) {
         return -1.f;
      }
      iseg_follow_ceiling(info);
   }
   if (!info->ident_valid || !info->m_valid) {
      return -1.f;
   }
   float c = info->vmax * (float) info->m_pct / 100.f;
   if (info->settings.max_voltage > 0.f) {
      c = std::min(c, info->settings.max_voltage);
   }
   if (info->odb_limit > 0.f) {
      c = std::min(c, info->odb_limit);
   }
   return c;
}

/// @brief refuse a set point above the ceiling, saying which number wins
/// @return TRUE when @p volts may be written
static bool iseg_check_ceiling(ISEG_NHQ_FE_INFO *info, float asked, bool refresh)
{
   // check what will go over the wire: whole-volt rounding can exceed a
   // fractional limit
   float volts = 0.f;
   iseg_format_d(info, asked, &volts);
   if (volts <= 0.f) {
      return true;   // 0 V is always allowed
   }
   float c = iseg_ceiling(info, refresh);
   if (c < 0.f) {
      if (iseg_may_log(info, "ceiling_unknown")) {
         ISEG_MSG(MERROR, "%.0f V refused: hardware limit (M%d, #) not readable",
                  (double) volts, iseg_hwch(info));
      }
      return false;
   }
   if (volts > c) {
      ISEG_MSG(MERROR, "%.0f V refused: limit %.0f V = min(ODB %.0f, M %d%% x %.0f, Max Voltage %.0f)",
               (double) volts, (double) c, (double) info->odb_limit, info->m_pct,
               (double) info->vmax, (double) info->settings.max_voltage);
      return false;
   }
   return true;
}

/*---- set point writes --------------------------------------------*/

/// @brief put a set point that should not be there back to a safe one
///
/// Always writes D<n>=0 first (decision 9: never leave a clamped or
/// unintended value stored where a later G, or autostart, would apply it).
/// No G is sent, so the output keeps the target of the last G. If the channel
/// was on, the previous set point is then written back so the stored D
/// matches the voltage the output is actually holding - unless that value is
/// above @p limit (the hardware limit a UMAX reply named), in which case the
/// operator is told to switch off.
static void iseg_undo_d(ISEG_NHQ_FE_INFO *info, bool was_on, bool prev_valid,
                        float prev, float limit)
{
   const int ch = iseg_hwch(info);
   const std::string zero = iseg_letter(info, 'D') + "=0";
   IsegReply z = iseg_cmd(info, zero, false);
   if (z.rc != IsegRc::Ok) {
      ISEG_MSG(MERROR, "D%d=0 after a bad set point FAILED (%.14s): check the unit",
               ch, iseg_rc_name(z.rc));
   }

   if (was_on && prev_valid && prev > 0.f) {
      if (limit > 0.f && prev > limit) {
         ISEG_MSG(MERROR, "output may hold %.0f V > limit %.0f V with D%d=0: switch ChState off",
                  (double) prev, (double) limit, ch);
      } else {
         float sent = 0.f;
         std::string txt = iseg_letter(info, 'D') + "=" + iseg_format_d(info, prev, &sent);
         IsegReply w = iseg_cmd(info, txt, false);
         if (w.rc == IsegRc::Umax) {
            iseg_cmd(info, zero, false);
         }
         float rb = 0.f;
         const bool read = iseg_read_d(info, &rb);
         if (w.rc == IsegRc::Ok && read && rb == sent) {
            ISEG_MSG(MINFO, "D%d restored to %.0f V, the voltage the output holds",
                     ch, (double) sent);
         } else if (read) {
            ISEG_MSG(MERROR, "D%d restore to %.0f V failed (%.14s): D%d reads %.0f V, switch ChState off",
                     ch, (double) prev, iseg_rc_name(w.rc), ch, (double) rb);
         } else {
            ISEG_MSG(MERROR, "D%d restore to %.0f V failed (%.14s): D%d unreadable, switch ChState off",
                     ch, (double) prev, iseg_rc_name(w.rc), ch);
         }
         return;
      }
   }
   float rb = 0.f;
   iseg_read_d(info, &rb);   // refresh the cache, whatever happened
}

/// @brief write D<n>, read it back, then (if @p start) send G<n>
///
/// The one path every set point takes (ON, OFF, demand change), mirroring
/// iseg_nhq_probe.set_voltage_guarded() + ramp_text(): a "? UMAX" reply, a
/// read-back that differs by more than the resolution, or a write whose
/// outcome is unclear, are all undone (iseg_undo_d) before any G goes out.
/// @return TRUE when D reads back as asked (and G was acknowledged)
static bool iseg_write_d(ISEG_NHQ_FE_INFO *info, float volts, bool start)
{
   const int ch = iseg_hwch(info);
   if (!iseg_link_up(info)) {
      if (iseg_may_log(info, "write_nolink")) {
         ISEG_MSG(MERROR, "D%d write not sent: no link to the unit", ch);
      }
      return false;
   }
   const bool was_on = info->on;
   const bool prev_valid = info->d_valid;
   const float prev = info->d_unit;

   float sent = 0.f;
   const std::string txt = iseg_letter(info, 'D') + "=" + iseg_format_d(info, volts, &sent);
   IsegReply w = iseg_cmd(info, txt, false);

   if (w.rc == IsegRc::NoLink || w.rc == IsegRc::Refused) {
      return false;   // nothing went out
   }
   if (w.rc == IsegRc::Umax) {
      ISEG_MSG(MERROR, "%.12s answered '%.16s': unit clamped D%d to %.0f V, writing D%d=0",
               txt.c_str(), w.text.c_str(), ch, (double) w.umax, ch);
      iseg_undo_d(info, was_on, prev_valid, prev, w.umax);
      return false;
   }

   float rb = 0.f;
   if (!iseg_read_d(info, &rb)) {
      // Unknown outcome: do not send G on a number nobody has seen.
      ISEG_MSG(MERROR, "%.12s: D%d not readable back (%.14s), G not sent",
               txt.c_str(), ch, iseg_rc_name(w.rc));
      return false;
   }
   if (sent == 0.f && rb != 0.f) {
      // Switching off is the safe direction: "undoing" would put the old set
      // point back. Send D=0 once more instead (an echo error in the first
      // copy leaves the unit with a prefix of it, see iseg_sync()).
      w = iseg_cmd(info, txt, false);
      if (!iseg_read_d(info, &rb) || rb != 0.f) {
         ISEG_MSG(MERROR, "%.12s twice, D%d still reads %.1f V: G not sent, check the unit",
                  txt.c_str(), ch, (double) rb);
         return false;
      }
   } else if (std::fabs(rb - sent) > iseg_d_resolution(info) + 1e-3f) {
      ISEG_MSG(MERROR, "%.12s (%.14s) reads back D%d=%.1f V: undoing, G not sent",
               txt.c_str(), iseg_rc_name(w.rc), ch, (double) rb);
      iseg_undo_d(info, was_on, prev_valid, prev, 0.f);
      return false;
   }
   if (w.rc != IsegRc::Ok && iseg_may_log(info, "write_ack")) {
      // the value took although the acknowledgement was lost
      ISEG_MSG(MINFO, "%.12s: no clean ack (%.14s) but D%d reads back %.1f V",
               txt.c_str(), iseg_rc_name(w.rc), ch, (double) rb);
   }

   if (!start) {
      return true;
   }
   const std::string g = iseg_letter(info, 'G');
   IsegReply gr = iseg_cmd(info, g, false);
   if (gr.rc != IsegRc::Ok) {
      ISEG_MSG(MERROR, "%.4s failed (%.14s): D%d=%.0f V set, output may not follow",
               g.c_str(), iseg_rc_name(gr.rc), ch, (double) rb);
      return false;
   }
   DWORD bits = 0;
   std::string word;
   if (!iseg_parse_status(gr.text, ch, &bits, &word)) {
      info->linked = false;
      ISEG_MSG(MERROR, "%.4s answered '%.16s' (other channel): reconnecting", g.c_str(),
               gr.text.c_str());
      return false;
   }
   info->s_bits = bits;
   info->s_valid = true;
   return true;
}

/// @brief refuse a write while the front panel has the channel (OFF / MAN)
/// @param fresh read S<n> and T<n> now instead of using the last poll
static bool iseg_front_panel_ok(ISEG_NHQ_FE_INFO *info, const char *what, bool fresh)
{
   if (fresh) {
      iseg_read_s(info);
      iseg_read_t(info);
   }
   const DWORD panel = (1u << iseg_nhq::kStatOff) | (1u << iseg_nhq::kStatMan);
   const bool t_panel = info->t_valid && (info->t_byte & (8u | 2u));
   if ((info->s_valid && (info->s_bits & panel)) || t_panel) {
      ISEG_MSG(MERROR, "%.10s refused: front panel %.20s (CONTROL to DAC, HV-ON on)", what,
               (info->s_bits & (1u << iseg_nhq::kStatOff)) || (info->t_byte & 8u)
                  ? "HV-ON switch off" : "in manual control");
      return false;
   }
   return true;
}

/// @brief a settings write cannot go out without a link: say so plainly
static bool iseg_refuse_nolink(ISEG_NHQ_FE_INFO *info, const char *what)
{
   if (iseg_link_up(info)) {
      return false;
   }
   if (iseg_may_log(info, "nolink_set")) {
      ISEG_MSG(MERROR, "%.16s not sent: no link to the unit", what);
   }
   return true;
}

/// @brief refuse any write while autostart is armed (decision 12)
static bool iseg_refuse_autostart(ISEG_NHQ_FE_INFO *info, const char *what)
{
   if (!info->autostart) {
      return false;
   }
   if (iseg_may_log(info, "autostart_write")) {
      ISEG_MSG(MERROR, "%.16s refused: A%d=%d (autostart) armed, frontend is read-only",
               what, iseg_hwch(info), info->auto_flags);
   }
   return true;
}

/// @brief run a ChState OFF that was latched while the link was down
static void iseg_service_pending(ISEG_NHQ_FE_INFO *info)
{
   if (!info->pending_off || !info->linked) {
      return;
   }
   info->pending_off = false;
   const int ch = iseg_hwch(info);
   if (!info->on) {
      ISEG_MSG(MINFO, "latched ChState OFF: unit is back already off (D%d=0)", ch);
      return;
   }
   if (iseg_refuse_autostart(info, "latched OFF")) {
      return;
   }
   const float keep = info->demand;
   if (iseg_write_d(info, 0.f, true)) {
      info->on = false;
      info->demand = keep;
      ISEG_MSG(MINFO, "latched ChState OFF executed: D%d=0, G%d; demand %.0f V kept",
               ch, ch, (double) keep);
   } else if (!info->linked) {
      info->pending_off = true;   // lost again on the way: try on the next link
   }
}

/*---- CMD_INIT / CMD_EXIT -----------------------------------------*/

/// @brief seed the demand cache from /Equipment/<eq>/Variables/Demand[0]
///
/// With DF_PRIO_DEVICE | DF_POLL_DEMAND, hv_init() overwrites
/// Variables/Demand with NaN (hv.cxx:1098-1101 and the db_set_record after
/// it) and then takes whatever CMD_GET_DEMAND reports. For a channel that is
/// off (D = 0) that would be 0 and the operator's demand would be lost on
/// every frontend restart. CMD_INIT runs before that, so the old value is
/// still in ODB here. Assumes this driver is the only one of its equipment
/// (its channel is index 0), as in scfe.cxx.
static void iseg_seed_demand(ISEG_NHQ_FE_INFO *info, HNDLE hDB, HNDLE hKey)
{
   const std::string path = db_get_path(hDB, hKey);
   const size_t cut = path.find("/Settings/Devices/");
   if (cut == std::string::npos) {
      return;
   }
   info->eq_path = path.substr(0, cut);
   const std::string demand_path = info->eq_path + "/Variables/Demand";
   HNDLE hDemand;
   if (db_find_key(hDB, 0, demand_path.c_str(), &hDemand) != DB_SUCCESS) {
      return;
   }
   float d = 0.f;
   int size = sizeof(d);
   if (db_get_data_index(hDB, hDemand, &d, &size, 0, TID_FLOAT) == DB_SUCCESS &&
       std::isfinite(d)) {
      info->demand = std::fabs(d);
      info->demand_valid = true;
   }
}

static INT iseg_nhq_fe_init(HNDLE hKey, void **pinfo, INT channels, INT(*bd)(INT cmd, ...))
{
   std::cout << "iseg_nhq init" << std::endl;
   (void) bd;   // no bus driver: this driver owns its serial port

   HNDLE hDB;
   int size;
   std::unique_ptr<ISEG_NHQ_FE_INFO> info = std::make_unique<ISEG_NHQ_FE_INFO>();
   info->hKey = hKey;
   info->num_channels = channels > 0 ? channels : 0;

   cm_get_experiment_database(&hDB, NULL);
   if (db_create_record(hDB, hKey, "./", ISEG_NHQ_SETTINGS_STRING) != DB_SUCCESS) {
      std::cerr << "Failed to create record" << std::endl;
      return FE_ERR_ODB;
   }
   size = sizeof(info->settings.port);
   db_get_value(hDB, hKey, "Port", info->settings.port, &size, TID_STRING, FALSE);
   size = sizeof(int);
   db_get_value(hDB, hKey, "Hardware Channel", &info->settings.hw_channel, &size, TID_INT32, FALSE);
   size = sizeof(float);
   db_get_value(hDB, hKey, "Max Voltage", &info->settings.max_voltage, &size, TID_FLOAT, FALSE);
   size = sizeof(int);
   db_get_value(hDB, hKey, "Echo Timeout ms", &info->settings.echo_timeout_ms, &size, TID_INT32, FALSE);

   if (info->settings.hw_channel < iseg_nhq::kMinHwChannel ||
       info->settings.hw_channel > iseg_nhq::kMaxHwChannel) {
      ISEG_MSG(MERROR, "Hardware Channel %d is not 1 or 2, using 2", info->settings.hw_channel);
      info->settings.hw_channel = 2;
   }
   if (!std::isfinite(info->settings.max_voltage)) {
      info->settings.max_voltage = 1300.f;
   }
   if (info->settings.max_voltage <= 0.f) {
      ISEG_MSG(MINFO, "Max Voltage %.0f: software ceiling off, M%% x Vmax only",
               (double) info->settings.max_voltage);
   }
   if (info->num_channels != 1) {
      ISEG_MSG(MERROR, "scfe.cxx configures %d channels, the driver serves 1 (index 0)",
               (int) channels);
   }

   info->hDB = hDB;
   iseg_seed_demand(info.get(), hDB, hKey);

   // As caen_hv_fe_init(): never fail init on a missing port. hv_init()
   // aborts on FE_ERR_HW (hv.cxx:766-770), so the equipment would never come
   // up and the reopen backoff could never heal the session.
   if (!iseg_port_open(info.get(), true)) {
      if (info->last_open_errno != EWOULDBLOCK) {   // "in use" was reported
         ISEG_MSG(MERROR, "HV %.40s: %.40s - no device, retry %ds",
                  info->settings.port, strerror(info->last_open_errno),
                  (int) iseg_nhq::kReopenInterval.count());
      }
      info->announce_recovery = true;
      *pinfo = info.release();
      return FE_SUCCESS;
   }
   if (!iseg_link_up(info.get(), true)) {
      ISEG_MSG(MERROR, "HV %.40s open but the unit does not answer, retry %ds",
               info->settings.port, (int) iseg_nhq::kReopenInterval.count());
      info->announce_recovery = true;
   }

   *pinfo = info.release();
   return FE_SUCCESS;
}

static INT iseg_nhq_fe_exit(ISEG_NHQ_FE_INFO *info)
{
   delete info;   // closes the port and releases the lock
   std::cout << "iseg_nhq exit" << std::endl;
   return FE_SUCCESS;
}

/*---- multithreaded get commands ----------------------------------*/

/// @brief CMD_GET_FIRST .. CMD_GET_LAST, from MIDAS's sc_thread (and from
///        hv_init() on the main thread before CMD_START)
///
/// @warning CMD_GET_STATUS hands us a @c float* that really points at a
///          @c DWORD; see caen_hv_fe_get(). The status word is written
///          bit-preserving and never NaN.
static INT iseg_nhq_fe_get(ISEG_NHQ_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
   if (channel != 0 || channel >= info->num_channels) {
      iseg_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
   iseg_service_pending(info);

   switch (cmd) {

   case CMD_GET: {           // U<n>, measured voltage
      IsegReply r = iseg_cmd(info, iseg_letter(info, 'U'), true);
      double v = 0;
      bool has_exp = false;
      if (r.rc == IsegRc::Ok && iseg_parse_number(r.text, &v, &has_exp)) {
         iseg_note_series(info, has_exp);
         *pvalue = (float) std::fabs(v);   // magnitude; sign in Variables/Polarity
         return FE_SUCCESS;
      }
      if (r.rc == IsegRc::Ok) {
         iseg_parse_failed(info, "U", r.text);
      }
      // NaN is honest here and cd_hv's Measured block rescues it
      // (hv.cxx:220-221); contrast CMD_GET_CURRENT / CMD_GET_DEMAND.
      *pvalue = (float) ss_nan();
      return FE_ERR_HW;
   }

   case CMD_GET_CURRENT: {   // I<n>, measured current [uA]
      IsegReply r = iseg_cmd(info, iseg_letter(info, 'I'), true);
      double amps = 0;
      if (r.rc == IsegRc::Ok && iseg_parse_current(r.text, &amps)) {
         info->imon = (float) std::fabs(amps * 1e6);
         info->imon_valid = true;
         *pvalue = info->imon;
         return FE_SUCCESS;
      }
      if (r.rc == IsegRc::Ok) {
         iseg_parse_failed(info, "I", r.text);
      }
      // Never NaN: cd_hv's Current block has no NaN rescue (hv.cxx:242-268).
      *pvalue = info->imon_valid ? info->imon : iseg_nhq::kCurrentNeverRead;
      return FE_ERR_HW;
   }

   case CMD_GET_DEMAND: {    // D<n> while on, the cached demand while off
      float d = 0.f;
      bool ok = iseg_read_d(info, &d);
      if (info->on && ok) {
         // While on, Demand is what the unit holds. The cache (what an OFF
         // keeps and the next ON writes) follows it, except to 0: a D of 0
         // while on is either an OFF half done (D=0 written, G failed) or an
         // explicit Demand 0, which iseg_set_demand() caches itself.
         if (d > 0.f) {
            info->demand = d;
            info->demand_valid = true;
         }
         *pvalue = d;
         return FE_SUCCESS;
      }
      // Off (or D unreadable): the cached demand. Never NaN: hv_read()
      // compares demand to its mirror with != and would rewrite
      // Variables/Demand on every cycle (hv.cxx:272-282).
      *pvalue = info->demand_valid ? info->demand : 0.f;
      return ok ? FE_SUCCESS : FE_ERR_HW;
   }

   case CMD_GET_STATUS: {    // S<n> + T<n> + driver bits, *not* a float
      bool ok = true;
      if (info->autostart) {
         info->s_bits = 0;
         info->s_valid = false;
      } else if (!iseg_read_s(info)) {
         ok = false;
      }
      if (!iseg_read_t(info)) {
         ok = false;
      }
      DWORD word = 0;
      if (info->s_valid) {
         word |= info->s_bits;
      }
      if (info->t_valid) {
         word |= (DWORD) (info->t_byte & iseg_nhq::kTMask) << iseg_nhq::kStatTShift;
      }
      if (info->autostart) {
         word |= 1u << iseg_nhq::kStatAutostart;
      }
      if (info->tot_seen) {
         word |= 1u << iseg_nhq::kStatTot;
         info->tot_seen = false;
      }
      if (info->d_valid && info->d_unit > 0.f) {
         word |= 1u << iseg_nhq::kStatDSet;
      }
      if (!ok) {
         word |= iseg_nhq::kStatStale;
      }
      *reinterpret_cast<DWORD *>(pvalue) = word;
      return ok ? FE_SUCCESS : FE_ERR_HW;
   }

   case CMD_GET_TRIP:        // no trip readback
   case CMD_GET_TEMPERATURE: // no temperature
      *pvalue = (float) ss_nan();
      return FE_SUCCESS;

   default:
      iseg_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
}

/*---- direct get commands -----------------------------------------*/

/// @brief CMD_GET_DIRECT .. CMD_GET_DIRECT_LAST, only from hv_init() on the
///        main thread before CMD_START. Served from the cache the connect
///        sequence filled, without serial traffic.
/// @note a value that is not known leaves @c *pvalue untouched: it holds the
///       ODB value (or cd_hv's default), see caen_hv_fe_get_direct().
static INT iseg_nhq_fe_get_direct(ISEG_NHQ_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
   if (channel != 0 || channel >= info->num_channels) {
      iseg_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }

   switch (cmd) {
   case CMD_GET_VOLTAGE_LIMIT: {
      // *pvalue is the ODB Voltage Limit here; keep it if it is lower than
      // the hardware / software ceiling, so an operator's lower limit
      // survives a frontend restart.
      float lim = std::numeric_limits<float>::infinity();
      if (std::isfinite(*pvalue) && *pvalue > 0.f) {
         lim = std::fabs(*pvalue);
      }
      if (info->settings.max_voltage > 0.f) {
         lim = std::min(lim, info->settings.max_voltage);
      }
      if (info->ident_valid && info->m_valid) {
         lim = std::min(lim, info->vmax * (float) info->m_pct / 100.f);
      }
      if (!std::isfinite(lim)) {
         return FE_ERR_HW;
      }
      info->odb_limit = lim;   // the true limit stays the write ceiling
      // A unit already running above the limit must not be clamped by cd_hv:
      // hv_read() would write Demand = D, hv_demand() would clamp it to the
      // Voltage Limit and send CMD_SET (hv.cxx:428-436), ramping the PMT
      // down on a frontend restart. So report at least D to cd_hv, and drop
      // hv_init's write-back of it so the internal ceiling keeps the truth.
      float reported = lim;
      if (info->on && info->d_valid && info->d_unit > lim) {
         reported = info->d_unit;
         iseg_arm_echo(info->echo_vlimit, reported);
         ISEG_MSG(MERROR, "S5 at %.0f V above limit %.0f V: switch off or lower Demand",
                  (double) info->d_unit, (double) lim);
      }
      *pvalue = reported;
      info->odb_shown = reported;
      return (info->ident_valid && info->m_valid) ? FE_SUCCESS : FE_ERR_HW;
   }
   case CMD_GET_CURRENT_LIMIT:
      if (!info->l_valid && info->linked) {
         iseg_read_l(info);    // the connect's read may have been unlucky
      }
      if (!info->l_valid) {
         // *pvalue keeps the ODB value, which hv_init writes back and whose
         // echo must then not reach the unit
         iseg_arm_echo(info->echo_ilimit, *pvalue);
         return FE_ERR_HW;
      }
      *pvalue = info->l_ua;
      iseg_arm_echo(info->echo_ilimit, *pvalue);
      return FE_SUCCESS;
   case CMD_GET_RAMPUP:
   case CMD_GET_RAMPDOWN: {    // one register for both directions
      if (!info->v_valid && info->linked) {
         iseg_read_v(info);
      }
      ISEG_NHQ_FE_INFO::Echo &e =
         cmd == CMD_GET_RAMPUP ? info->echo_rampup : info->echo_rampdown;
      if (!info->v_valid) {
         iseg_arm_echo(e, *pvalue);
         return FE_ERR_HW;
      }
      *pvalue = (float) info->v_ramp;
      iseg_arm_echo(e, *pvalue);
      return FE_SUCCESS;
   }
   case CMD_GET_TRIP_TIME:     // not supported by the NHQ
      *pvalue = 0.f;
      return FE_SUCCESS;
   case CMD_GET_CHSTATE:
      // DWORD behind a float*, as CMD_GET_STATUS
      *reinterpret_cast<DWORD *>(pvalue) = info->on ? 1u : 0u;
      iseg_arm_echo(info->echo_chstate, info->on ? 1.f : 0.f);
      return info->d_valid ? FE_SUCCESS : FE_ERR_HW;
   case CMD_GET_DEMAND_DIRECT:
      if (!info->demand_valid) {
         return FE_ERR_HW;
      }
      *pvalue = info->demand;
      return FE_SUCCESS;
   default:
      iseg_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
}

/*---- set commands ------------------------------------------------*/

/// @brief CMD_SET_CHSTATE: ON = D<n>=demand + G<n>, OFF = D<n>=0 + G<n>
///
/// hv_init() writes ChState from CMD_GET_CHSTATE and the hotlink echoes it
/// straight back, so a request equal to the current state is a no-op: that is
/// what keeps a frontend restart from writing anything. An OFF while the
/// state was never read is dropped too (a restart with an unreadable D must
/// not ramp a running PMT down).
static INT iseg_set_chstate(ISEG_NHQ_FE_INFO *info, float value)
{
   const int ch = iseg_hwch(info);
   if (value != 0.0f && value != 1.0f) {
      if (iseg_may_log(info, "chstate_value")) {
         ISEG_MSG(MERROR, "ChState %.3g ignored, expect exactly 0 or 1", (double) value);
      }
      return FE_SUCCESS;
   }
   const bool want_on = (value == 1.0f);
   info->last_chstate = want_on ? 1 : 0;   // what ODB shows from now on
   if (iseg_startup_echo(info->echo_chstate, value)) {
      return FE_SUCCESS;   // hv_init's write-back of what we reported
   }
   if (want_on) {
      info->pending_off = false;   // the operator's latest word wins
   }

   if (!info->d_valid) {
      if (iseg_may_log(info, "chstate_unknown")) {
         ISEG_MSG(MERROR, "ChState %d ignored: D%d never read from the unit", want_on ? 1 : 0, ch);
      }
      return FE_ERR_HW;
   }
   if (want_on == info->on) {
      return FE_SUCCESS;
   }
   // bring the link up first: a (re)connect re-derives info->on and reads
   // A<n>, and both decide what happens next
   if (!iseg_link_up(info)) {
      if (!want_on && info->ever_linked) {
         // OFF is the safe direction: keep it and run it on the reconnect
         info->pending_off = true;
         ISEG_MSG(MERROR, "ChState OFF latched: no link, D%d=0 + G%d go out when the unit answers",
                  ch, ch);
      } else if (iseg_may_log(info, "chstate_nolink")) {
         ISEG_MSG(MERROR, "ChState %d not applied: no link to the unit", want_on ? 1 : 0);
      }
      return FE_ERR_HW;
   }
   if (want_on == info->on) {
      return FE_SUCCESS;
   }
   if (iseg_refuse_autostart(info, want_on ? "ChState ON" : "ChState OFF")) {
      return FE_ERR_HW;
   }

   if (want_on) {
      if (!info->demand_valid) {
         ISEG_MSG(MERROR, "ChState ON refused: no demand voltage known");
         return FE_ERR_HW;
      }
      if (!iseg_front_panel_ok(info, "ChState ON", true)) {
         return FE_ERR_HW;
      }
      if (!iseg_check_ceiling(info, info->demand, true)) {
         return FE_ERR_HW;
      }
      if (!iseg_write_d(info, info->demand, true)) {
         // Keep D != 0 <=> on: if the set point went in but the switch-on
         // did not complete (G lost), take it out again. D=0 plus G is the
         // complete OFF, safe whether or not the first G was executed.
         if (info->d_valid && info->d_unit > 0.f) {
            ISEG_MSG(MERROR, "ChState ON failed after D%d was written: switching back off", ch);
            iseg_write_d(info, 0.f, true);
         }
         return FE_ERR_HW;
      }
      info->on = true;
      ISEG_MSG(MINFO, "S5 on: D%d=%.0f V, G%d -> %.12s", ch, (double) info->d_unit, ch,
               iseg_nhq::stat_text(info->s_bits).c_str());
      return FE_SUCCESS;
   }

   const float keep = info->demand;
   if (!iseg_write_d(info, 0.f, true)) {
      return FE_ERR_HW;
   }
   info->on = false;
   info->demand = keep;   // the demand survives the switch-off (decision 8)
   ISEG_MSG(MINFO, "S5 off: D%d=0, G%d -> %.12s; demand %.0f V kept", ch, ch,
            iseg_nhq::stat_text(info->s_bits).c_str(), (double) keep);
   return FE_SUCCESS;
}

/// @brief CMD_SET: while ON write D + G, while OFF only cache (decision 8)
static INT iseg_set_demand(ISEG_NHQ_FE_INFO *info, float value)
{
   if (!std::isfinite(value)) {
      return FE_SUCCESS;
   }
   const float v = std::fabs(value);
   if (iseg_refuse_autostart(info, "Demand")) {
      return FE_ERR_HW;
   }

   if (!info->on) {
      // cached only; checked against the ceiling now as far as it is known
      // (so the operator hears about it at once) and again, with M read
      // fresh, when ChState goes to 1
      if (iseg_ceiling(info, false) >= 0.f && !iseg_check_ceiling(info, v, false)) {
         return FE_ERR_HW;
      }
      info->demand = v;
      info->demand_valid = true;
      return FE_SUCCESS;
   }

   if (info->d_valid && v == info->d_unit) {
      return FE_SUCCESS;
   }
   if (info->last_chstate == 0) {
      // adopted on (a link that came up after the start, or a reconnect)
      // while ODB still shows OFF: no D + G behind the operator's back
      ISEG_MSG(MERROR, "Demand not applied: S5 is on but ChState shows OFF, set ChState first");
      return FE_ERR_HW;
   }
   const DWORD tripped = (1u << iseg_nhq::kStatTrp) | (1u << iseg_nhq::kStatErr) |
                         (1u << iseg_nhq::kStatInh);
   if (info->s_valid && (info->s_bits & tripped)) {
      // a G here would release the shut-off and ramp straight back up
      ISEG_MSG(MERROR, "S5 tripped (%.8s): Demand not applied, set ChState 0 then 1",
               iseg_nhq::stat_text(info->s_bits & tripped).c_str());
      return FE_ERR_HW;
   }
   if (!iseg_front_panel_ok(info, "Demand", false)) {
      return FE_ERR_HW;
   }
   if (!iseg_check_ceiling(info, v, true)) {
      return FE_ERR_HW;
   }
   if (!iseg_write_d(info, v, true)) {
      return FE_ERR_HW;
   }
   info->demand = info->d_unit;
   info->demand_valid = true;
   return FE_SUCCESS;
}

/// @brief CMD_SET_CURRENT_LIMIT -> L<n> in uA (1 count = 1 uA on the 208L)
static INT iseg_set_current_limit(ISEG_NHQ_FE_INFO *info, float value)
{
   if (!std::isfinite(value)) {
      return FE_SUCCESS;
   }
   if (iseg_startup_echo(info->echo_ilimit, value)) {
      if (!info->l_valid && iseg_may_log(info, "l_echo")) {
         ISEG_MSG(MERROR, "ODB Current Limit %.0f uA not sent at start (L%d not read), unit keeps its own",
                  (double) value, iseg_hwch(info));
      }
      return FE_SUCCESS;
   }
   const long counts = std::lround(std::fabs((double) value));
   if (info->l_valid && std::fabs(std::fabs(value) - info->l_ua) < 0.5f) {
      return FE_SUCCESS;   // what the unit already has
   }
   if (iseg_refuse_nolink(info, "Current Limit") ||
       iseg_refuse_autostart(info, "Current Limit")) {
      return FE_ERR_HW;
   }
   if (info->series == IsegSeries::Precision) {
      // L<n>= takes counts of the first current range, whose size depends on
      // the model option; only the standard-series 208L (1 uA) is known
      if (iseg_may_log(info, "l_precision")) {
         ISEG_MSG(MERROR, "Current Limit not written: precision series L count size unknown");
      }
      return FE_ERR_HW;
   }
   const std::string txt = iseg_letter(info, 'L') + "=" + std::to_string(counts);
   IsegReply r = iseg_cmd(info, txt, false);
   iseg_read_l(info);
   if (r.rc != IsegRc::Ok || !info->l_valid || info->l_ua != (float) counts) {
      ISEG_MSG(MERROR, "%.16s failed (%.14s), L%d reads %.0f uA", txt.c_str(),
               iseg_rc_name(r.rc), iseg_hwch(info), info->l_valid ? (double) info->l_ua : -1.0);
      return FE_ERR_HW;
   }
   ISEG_MSG(MINFO, "current trip L%d = %ld uA%.12s", iseg_hwch(info), counts,
            counts == 0 ? " (no trip)" : "");
   return FE_SUCCESS;
}

/// @brief CMD_SET_RAMPUP -> V<n> (the one ramp register, decision 10)
static INT iseg_set_rampup(ISEG_NHQ_FE_INFO *info, float value)
{
   if (!std::isfinite(value)) {
      return FE_SUCCESS;
   }
   if (iseg_startup_echo(info->echo_rampup, value)) {
      if (!info->v_valid && value != 0.f && iseg_may_log(info, "v_echo")) {
         ISEG_MSG(MERROR, "ODB Ramp Up Speed %.0f V/s not sent at start (V%d not read), unit keeps its own",
                  (double) value, iseg_hwch(info));
      }
      return FE_SUCCESS;
   }
   const long vps = std::lround(std::fabs((double) value));
   if (info->v_valid && vps == info->v_ramp) {
      return FE_SUCCESS;   // what the unit already has
   }
   if (iseg_refuse_nolink(info, "Ramp Up Speed") ||
       iseg_refuse_autostart(info, "Ramp Up Speed")) {
      return FE_ERR_HW;
   }
   if (vps < iseg_nhq::kMinRamp || vps > iseg_nhq::kMaxRamp) {
      // cd_hv defaults Ramp Up Speed to 0 when the device read failed at
      // init (hv.cxx:915-920); the unit would answer ????
      if (iseg_may_log(info, "ramp_range")) {
         ISEG_MSG(MERROR, "Ramp Up Speed %.3g V/s outside %d..%d, not sent", (double) value,
                  iseg_nhq::kMinRamp, iseg_nhq::kMaxRamp);
      }
      return FE_ERR_HW;
   }
   const std::string txt = iseg_letter(info, 'V') + "=" + std::to_string(vps);
   IsegReply r = iseg_cmd(info, txt, false);
   iseg_read_v(info);
   if (r.rc != IsegRc::Ok || !info->v_valid || info->v_ramp != vps) {
      ISEG_MSG(MERROR, "%.16s failed (%.14s), V%d reads %d V/s", txt.c_str(),
               iseg_rc_name(r.rc), iseg_hwch(info), info->v_valid ? info->v_ramp : -1);
      return FE_ERR_HW;
   }
   ISEG_MSG(MINFO, "ramp V%d = %ld V/s (up and down)", iseg_hwch(info), vps);
   return FE_SUCCESS;
}

/// @brief CMD_SET_RAMPDOWN: refused, the NHQ has one ramp register
///
/// Equal to V<n> (cd_hv's startup echo of what CMD_GET_RAMPDOWN returned),
/// 0 or NaN are dropped silently; anything else gets a message.
static INT iseg_set_rampdown(ISEG_NHQ_FE_INFO *info, float value)
{
   if (iseg_startup_echo(info->echo_rampdown, value)) {
      return FE_SUCCESS;
   }
   if (!std::isfinite(value) || value == 0.f ||
       (info->v_valid && std::lround(std::fabs((double) value)) == info->v_ramp)) {
      return FE_SUCCESS;
   }
   if (iseg_may_log(info, "rampdown")) {
      ISEG_MSG(MERROR, "Ramp Down Speed refused: one ramp register V%d (%d V/s), use Ramp Up",
               iseg_hwch(info), info->v_valid ? info->v_ramp : -1);
   }
   return FE_ERR_HW;
}

/// @brief CMD_SET_VOLTAGE_LIMIT: remembered for the ceiling, not a device write
static INT iseg_set_voltage_limit(ISEG_NHQ_FE_INFO *info, float value)
{
   if (!std::isfinite(value)) {
      return FE_SUCCESS;
   }
   if (iseg_startup_echo(info->echo_vlimit, value)) {
      return FE_SUCCESS;   // raised for cd_hv only, see CMD_GET_VOLTAGE_LIMIT
   }
   const float v = std::fabs(value);
   if (v == info->odb_limit) {
      info->odb_shown = v;
      return FE_SUCCESS;   // unchanged, or the hotlink echo of our own write-back
   }
   const float fixed = iseg_fixed_ceiling(info);
   if (fixed > 0.f && v > fixed) {
      // put the effective limit into ODB, so the page and cd_hv's own demand
      // clamp agree with the driver's ceiling
      ISEG_MSG(MINFO, "S5: Voltage Limit %.0f V > ceiling %.0f V %.60s: set to %.0f V",
               (double) v, (double) fixed, iseg_ceiling_source(info).c_str(), (double) fixed);
      iseg_write_odb_limit(info, fixed);
      return FE_SUCCESS;
   }
   info->odb_limit = v;
   info->odb_shown = v;
   return FE_SUCCESS;
}

/// @brief CMD_SET_FIRST .. CMD_SET_LAST, from MIDAS's sc_thread
static INT iseg_nhq_fe_set(ISEG_NHQ_FE_INFO *info, INT channel, float value, INT cmd)
{
   if (channel != 0 || channel >= info->num_channels) {
      return FE_SUCCESS;
   }
   switch (cmd) {
   case CMD_SET:
      return iseg_set_demand(info, value);
   case CMD_SET_VOLTAGE_LIMIT:
      return iseg_set_voltage_limit(info, value);
   case CMD_SET_CURRENT_LIMIT:
      return iseg_set_current_limit(info, value);
   case CMD_SET_RAMPUP:
      return iseg_set_rampup(info, value);
   case CMD_SET_RAMPDOWN:
      return iseg_set_rampdown(info, value);
   case CMD_SET_TRIP_TIME:
      if (std::isfinite(value) && value != 0.f && iseg_may_log(info, "trip_time")) {
         ISEG_MSG(MINFO, "Trip Time not supported by the NHQ, ignored (use Current Limit)");
      }
      return FE_SUCCESS;
   case CMD_SET_CHSTATE:
      return iseg_set_chstate(info, value);
   default:
      return FE_SUCCESS;
   }
}

/*---- driver entry point ------------------------------------------*/

INT iseg_nhq_fe(INT cmd, ...)
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

   switch (cmd) {
   case CMD_INIT:
      hKey = va_arg(argptr, HNDLE);
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      flags = va_arg(argptr, DWORD);
      bd = va_arg(argptr, INT(*)(INT, ...));
      (void) flags;
      status = iseg_nhq_fe_init(hKey, (void **) info, channel, bd);
      break;

   case CMD_EXIT:
      info = va_arg(argptr, void *);
      status = iseg_nhq_fe_exit((ISEG_NHQ_FE_INFO *) info);
      break;

   case CMD_GET:
   case CMD_GET_CURRENT:
   case CMD_GET_TRIP:
   case CMD_GET_STATUS:
   case CMD_GET_TEMPERATURE:
   case CMD_GET_DEMAND:
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      pvalue = va_arg(argptr, float *);
      status = iseg_nhq_fe_get((ISEG_NHQ_FE_INFO *) info, channel, pvalue, cmd);
      break;

   case CMD_GET_VOLTAGE_LIMIT:
   case CMD_GET_CURRENT_LIMIT:
   case CMD_GET_RAMPUP:
   case CMD_GET_RAMPDOWN:
   case CMD_GET_TRIP_TIME:
   case CMD_GET_CHSTATE:
   case CMD_GET_DEMAND_DIRECT:
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      pvalue = va_arg(argptr, float *);
      status = iseg_nhq_fe_get_direct((ISEG_NHQ_FE_INFO *) info, channel, pvalue, cmd);
      break;

   case CMD_SET:
   case CMD_SET_VOLTAGE_LIMIT:
   case CMD_SET_CURRENT_LIMIT:
   case CMD_SET_RAMPUP:
   case CMD_SET_RAMPDOWN:
   case CMD_SET_TRIP_TIME:
   case CMD_SET_CHSTATE:
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      value = (float) va_arg(argptr, double);   // floats are passed as double
      status = iseg_nhq_fe_set((ISEG_NHQ_FE_INFO *) info, channel, value, cmd);
      break;

   case CMD_GET_THRESHOLD:
   case CMD_GET_THRESHOLD_CURRENT:
   case CMD_GET_THRESHOLD_ZERO:
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      pvalue = va_arg(argptr, float *);
      (void) info;
      (void) channel;
      if (cmd == CMD_GET_THRESHOLD) {
         *pvalue = iseg_nhq::kThresholdVoltage;
      } else if (cmd == CMD_GET_THRESHOLD_CURRENT) {
         *pvalue = iseg_nhq::kThresholdCurrent;
      } else {
         *pvalue = iseg_nhq::kThresholdZero;
      }
      break;

   case CMD_GET_LABEL: {
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      name = va_arg(argptr, char *);
      ISEG_NHQ_FE_INFO *hv = (ISEG_NHQ_FE_INFO *) info;
      if (hv && channel == 0) {
         snprintf(name, NAME_LENGTH, "%s", iseg_nhq::kChannelLabel);
      } else {
         name[0] = 0;
      }
      break;
   }

   default:
      // CMD_SET_LABEL (the unit has no channel name), CMD_START/STOP/IDLE,
      // CMD_GET_CRATEMAP: deliberately not provided.
      break;
   }

   va_end(argptr);
   return status;
}
