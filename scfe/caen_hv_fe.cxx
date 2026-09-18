/********************************************************************\

  Name:         caen_hv_fe.cxx

  Contents:     MIDAS device driver for a CAEN DT1470ET desktop HV supply
                (4 channels, reversible polarity, USB CDC-ACM or FTDI serial),
                serving the stock "cd_hv" class driver.

                Same shape as scfe/isel_fe.cxx: unique_ptr info struct,
                db_create_record settings string, va_arg dispatcher.

  Threading:    The equipment uses
                  DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP |
                  DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND
                With DF_MULTITHREAD, MIDAS routes every command in
                [CMD_GET_FIRST, CMD_GET_LAST] and [CMD_SET_FIRST, CMD_SET_LAST]
                through its own "sc_thread" (src/device_driver.cxx), i.e. those
                run on exactly one thread. Everything else reaches us directly
                on the main thread and *only before* CMD_START creates that
                thread: CMD_INIT, CMD_GET_LABEL/CMD_SET_LABEL, the update
                thresholds, and the direct gets (>= CMD_GET_DIRECT), which
                hv_init() issues while it builds the ODB tree (mfe.cxx calls
                CMD_START afterwards). So no two threads ever touch the serial
                port and this driver needs no mutex.

                Consequently the serial port, the whole per-channel cache
                except CAEN_HV_CHANNEL::pol, and the last_log std::map are
                main-thread-only until CMD_START and sc_thread-only afterwards.
                No other code may issue a direct get, a label command or any
                other driver command at run time: doing so would race
                sc_thread on the port and corrupt the std::map. A mutex around
                caen_hv_transact() and caen_hv_may_log() would be required
                first.

                caen_hv_polarity() is the one entry point that is safe at run
                time from the main thread (WP3 uses it to publish
                Variables/Polarity): it only loads a std::atomic<int> and does
                no I/O.

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
#include <sys/select.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <midas.h>
#include <msystem.h>

#include "caen_hv_fe.h"

/*---- protocol constants ------------------------------------------*/
// Everything the wire protocol depends on lives here, in one place: these
// names, value formats and bit numbers are taken from the N1470 family manual
// and are expected to be corrected after the first run against real hardware
// (drivers/caen_hv/caen_hv_probe.py prints what the unit really answers).

namespace caen_hv {

   /// @brief channel parameters (CMD:MON / CMD:SET, with CH:n)
   constexpr const char *kParVSet = "VSET";   ///< set voltage        [V]
   constexpr const char *kParISet = "ISET";   ///< current limit      [uA]
   constexpr const char *kParVMon = "VMON";   ///< measured voltage   [V]
   constexpr const char *kParIMon = "IMON";   ///< measured current   [uA]
   constexpr const char *kParMaxV = "MAXV";   ///< hardware voltage limit [V]
   constexpr const char *kParRUp  = "RUP";    ///< ramp up speed      [V/s]
   constexpr const char *kParRDwn = "RDW";    ///< ramp down speed    [V/s]
   constexpr const char *kParTrip = "TRIP";   ///< trip time          [s]
   constexpr const char *kParPol  = "POL";    ///< polarity, read only, "+"/"-"
   constexpr const char *kParStat = "STAT";   ///< channel status word
   constexpr const char *kParOn   = "ON";     ///< switch channel on  (no VAL)
   constexpr const char *kParOff  = "OFF";    ///< switch channel off (no VAL)

   // IMON has no "no value" encoding on the wire, so the driver uses
   // kCurrentNeverRead (= -1.0, declared in caen_hv_fe.h) until the first
   // successful read: a measured current is a magnitude and can never be
   // negative. NaN is not an option there, see CMD_GET_CURRENT below.

   // printf format for the VAL field of each settable parameter. The board
   // refuses a value it cannot represent with "VAL:ERR", so the resolution
   // matters: the manual gives 0.1 V for VSET, 0.05 uA for ISET, 0.1 s for
   // TRIP, and integer V/s (1..500) for RUP/RDW; MAXV is whole volts.
   // @todo confirm all five on hardware (the emulator accepts any float).
   constexpr const char *kFmtVSet = "%.1f";
   constexpr const char *kFmtISet = "%.2f";
   constexpr const char *kFmtMaxV = "%.0f";
   constexpr const char *kFmtRUp  = "%.0f";
   constexpr const char *kFmtRDwn = "%.0f";
   constexpr const char *kFmtTrip = "%.1f";

   /// @brief smallest ramp speed the board accepts [V/s]; see
   ///        caen_hv_set_ramp() for why this has to be checked
   constexpr float kMinRampSpeed = 1.0f;

   /// @brief board parameters (CMD:MON, no CH)
   constexpr const char *kParBdName = "BDNAME"; ///< model name, e.g. "DT1470ET"
   constexpr const char *kParBdNCh  = "BDNCH";  ///< number of channels
   constexpr const char *kParBdFRel = "BDFREL"; ///< firmware release
   constexpr const char *kParBdSNum = "BDSNUM"; ///< serial number
   constexpr const char *kParBdCtr  = "BDCTR";  ///< "LOCAL" or "REMOTE"

   /// @brief commands
   constexpr const char *kCmdMon = "MON";
   constexpr const char *kCmdSet = "SET";

   // stat_bit_t, kStatNumBits and kStatStale are public and live in
   // caen_hv_fe.h, so hv_alarm.cxx uses the same definitions. Only the name
   // table stays here, behind stat_bit_name().

   /// @brief human readable STAT bit names, indexed by stat_bit_t
   static const char *const kStatBitName[] = {
      "ON", "RUP", "RDW", "OVC", "OVV", "UNV", "MAXV",
      "TRIP", "OVP", "OVT", "DIS", "KILL", "ILK", "NOCAL"
   };
   static_assert((int) (sizeof(kStatBitName) / sizeof(kStatBitName[0])) == kStatNumBits,
                 "STAT bit name table and kStatNumBits disagree");

   /// @brief ODB update thresholds handed to cd_hv (cd_hv's own defaults of
   ///        1 V / 0.1 uA / 20 V zero threshold would suppress Measured
   ///        updates below 20 V, hv.cxx:216-218, 899).
   constexpr float kThresholdVoltage = 1.0f;   ///< [V]
   constexpr float kThresholdCurrent = 0.1f;   ///< [uA]
   constexpr float kThresholdZero    = 0.5f;   ///< [V]

   /// @brief how often at most any one kind of repetitive cm_msg is emitted
   constexpr auto kLogInterval = std::chrono::seconds(30);

   /// @brief how often at most open() is retried after the port went away
   constexpr auto kReopenInterval = std::chrono::seconds(5);

   /// @brief refresh the cached POL every n-th CMD_GET_STATUS of a channel
   ///        (and always at CMD_INIT and after a reconnect). POL can only
   ///        change when somebody flips a rear panel switch, which needs the
   ///        cable out, so polling it on every sweep is wasted serial time.
   constexpr int kPolRefreshEvery = 10;

   /// @brief highest board address the %02d request framing allows; the bus
   ///        itself supports 0..31
   constexpr int kMaxBoardAddress = 31;

   /// @brief fallback when the ODB timeout setting is nonsense
   constexpr int kDefaultTimeoutMs = 500;

   /// @brief settling time between a PAR:ON / PAR:OFF and the STAT re-read
   ///        that confirms it [ms]
   constexpr int kChStateSettleMs = 100;

   /// @brief longest message body this driver hands to cm_msg
   ///
   /// MIDAS's frontend printer memcpy()s the body into a char[160] without a
   /// bound check (mfe.cxx:1357), so anything from ~159 characters up kills
   /// the frontend. 120 leaves room for the "[file:line:routine,LEVEL] "
   /// prefix that message_print() strips and a safety margin.
   constexpr size_t kMaxMsgLen = 120;

}  // namespace caen_hv

/*---- ODB settings ------------------------------------------------*/

#define CAEN_HV_SETTINGS_STRING "\
Port = STRING : [64] /dev/caen_hv0\n\
Board Address = INT32 : 0\n\
Timeout ms = INT32 : 500\n\
Expected Channels = INT32 : 4\n\
"

/// @brief device settings stored in ODB
/// @note keep as fixed length struct as it maps the ODB layout
struct CAEN_HV_SETTINGS {
   char port[64];
   int board;
   int timeout_ms;
   int expected_channels;
};

/*---- driver state ------------------------------------------------*/

/// @brief everything the driver caches per channel
struct CAEN_HV_CHANNEL {
   /// @brief last successfully read VSET. Handed out by CMD_GET_DEMAND even
   ///        when the board is unreachable: hv_read() compares demand against
   ///        its mirror with != , so a NaN (NaN != NaN) would make it rewrite
   ///        Variables/Demand on every single idle cycle (hv.cxx:272-282).
   float vset{0.f};
   /// @brief FALSE until VSET was read at least once (only then can
   ///        CMD_GET_DEMAND avoid NaN at all)
   bool vset_valid{false};

   /// @brief last successfully read IMON. Handed out by CMD_GET_CURRENT even
   ///        when the board is unreachable, because a NaN there would freeze
   ///        Variables/Current for the whole session (hv.cxx:242-268).
   float imon{caen_hv::kCurrentNeverRead};
   /// @brief FALSE until IMON was read at least once; CMD_GET_CURRENT then
   ///        reports caen_hv::kCurrentNeverRead
   bool imon_valid{false};

   /// @brief last successfully read STAT word
   DWORD stat{0};
   /// @brief FALSE until STAT was read at least once. CMD_SET_CHSTATE will not
   ///        switch a channel *on* while this is FALSE, see
   ///        caen_hv_set_chstate().
   bool stat_valid{false};

   /// @brief cached POL, +1 / -1 / 0 = unknown. Written by the poll thread,
   ///        read by the main thread through caen_hv_polarity().
   std::atomic<int> pol{0};
   /// @brief CMD_GET_STATUS calls left before POL is read again; 0 forces a
   ///        refresh on the next one (set at init and on every reconnect)
   int pol_countdown{0};
};

/// @brief the internal information to run the FE
struct CAEN_HV_FE_INFO {
   /// @brief The settings related to the FE
   CAEN_HV_SETTINGS settings{};

   /// @brief DB handle of this device's settings subtree
   HNDLE hKey{};

   /// @brief number of channels as declared in the DEVICE_DRIVER table
   int num_channels{0};

   /// @brief per channel cache, num_channels entries
   std::vector<CAEN_HV_CHANNEL> channel{};

   /// @brief serial port, -1 when closed
   int fd{-1};

   /// @brief TRUE while the port is open
   bool connected{false};

   /// @brief TRUE after a loss, until the first *successful reply* following
   ///        the next open(); drives the single MINFO on recovery. A bare
   ///        successful open() proves nothing on a USB tty.
   bool announce_recovery{false};

   /// @brief TRUE after a request went unanswered: a late reply may still be
   ///        in flight and would otherwise be taken for the answer to the next
   ///        request. Replies carry no PAR echo, so that would silently
   ///        publish e.g. VMON as IMON. Cleared by caen_hv_resync().
   bool desync{false};

   /// @brief consecutive read()s that returned 0 bytes (CDC-ACM signals an
   ///        unplugged device this way instead of failing)
   int zero_reads{0};

   /// @brief last time open() was attempted, for the 5 s retry limit
   std::chrono::steady_clock::time_point last_open{};
   bool have_last_open{false};
   /// @brief errno of the last failed open(), for the caller's message
   int last_open_errno{0};

   /// @brief last time each kind of repetitive message was logged
   std::map<std::string, std::chrono::steady_clock::time_point> last_log{};

   ~CAEN_HV_FE_INFO() {
      if (fd >= 0) {
         close(fd);
      }
   }
};

/*---- small helpers -----------------------------------------------*/

/// @brief rate limiter: TRUE at most once per kLogInterval per @p kind
/// @note main-thread-only before CMD_START, sc_thread-only after; see the
///       file header. The std::map is not thread safe.
static bool caen_hv_may_log(CAEN_HV_FE_INFO *info, const char *kind)
{
   auto now = std::chrono::steady_clock::now();
   auto it = info->last_log.find(kind);
   if (it != info->last_log.end() && now - it->second < caen_hv::kLogInterval) {
      return false;
   }
   info->last_log[kind] = now;
   return true;
}

/// @brief send a message whose body can never overrun MIDAS's printer
///
/// mfe.cxx's message_print() copies every cm_msg body into a fixed
/// `char str[160]` with memcpy and no bound (mfe.cxx:1348-1357, the copy is at
/// mfe.cxx:1357), so a body of ~159 characters or more aborts the whole
/// frontend with glibc's "*** buffer overflow detected ***". Every format
/// below is written to stay inside caen_hv::kMaxMsgLen, with an explicit
/// precision on every runtime string (a port name is up to 63 characters, a
/// strerror() text ~40, a board reply up to 1024). This macro truncates as a
/// last line of defence. It is a macro, not a function, so __FILE__/__LINE__
/// still point at the real call site.
#define CAEN_HV_MSG(type, fmt, ...)                                            \
   do {                                                                        \
      char caen_hv_body_[caen_hv::kMaxMsgLen + 1];                             \
      snprintf(caen_hv_body_, sizeof(caen_hv_body_), fmt __VA_OPT__(,) __VA_ARGS__); \
      cm_msg(type, "caen_hv_fe", "%s", caen_hv_body_);                         \
   } while (0)

/// @brief reply timeout in ms, guarded against a nonsense ODB setting
static int caen_hv_timeout_ms(const CAEN_HV_FE_INFO *info)
{
   return info->settings.timeout_ms > 0 ? info->settings.timeout_ms
                                        : caen_hv::kDefaultTimeoutMs;
}

// --- public STAT helpers, declared in caen_hv_fe.h ---

const char *caen_hv::stat_bit_name(int bit)
{
   if (bit < 0 || bit >= caen_hv::kStatNumBits) {
      return "?";
   }
   return caen_hv::kStatBitName[bit];
}

std::string caen_hv::stat_text(DWORD stat)
{
   std::string out;
   for (int bit = 0; bit < caen_hv::kStatNumBits; bit++) {
      if (stat & (1u << bit)) {
         if (!out.empty()) {
            out += "|";
         }
         out += caen_hv::kStatBitName[bit];
      }
   }
   if (stat & caen_hv::kStatStale) {
      out += out.empty() ? "STALE" : "|STALE";
   }
   return out.empty() ? std::string("none") : out;
}

/// @brief write "no value available" into the slot MIDAS handed us
///
/// @warning CMD_GET_STATUS and CMD_GET_CHSTATE hand us a @c float* that really
///          points at a @c DWORD (hv.cxx:159-170, 1031-1037, 1075-1077), so
///          those two must never receive a NaN: the 0x7FC00000 bit pattern
///          decodes as almost every fault bit set *and* as "channel on".
static void caen_hv_write_invalid(float *pvalue, INT cmd)
{
   if (cmd == CMD_GET_STATUS) {
      // stale flag only: no real alarm bit, but visible in Variables/ChStatus
      *reinterpret_cast<DWORD *>(pvalue) = caen_hv::kStatStale;
   } else if (cmd == CMD_GET_CHSTATE) {
      *reinterpret_cast<DWORD *>(pvalue) = 0u;   // "off" is the safe answer
   } else {
      *pvalue = (float) ss_nan();
   }
}

/*---- serial port -------------------------------------------------*/

/// @brief close the port and remember that we lost it
static void caen_hv_port_lost(CAEN_HV_FE_INFO *info, const char *what, int err)
{
   bool was_connected = info->connected;

   if (info->fd >= 0) {
      close(info->fd);
      info->fd = -1;
   }
   info->connected = false;
   info->desync = false;          // a fresh open() flushes anyway
   info->announce_recovery = true;
   info->zero_reads = 0;
   info->last_open = std::chrono::steady_clock::now();
   info->have_last_open = true;

   // One message per loss, and rate limited on top so a flapping cable cannot
   // fill the message log every 5 s.
   if (was_connected && caen_hv_may_log(info, "lost")) {
      CAEN_HV_MSG(MERROR, "HV %.40s lost (%.20s, errno %d), retry %ds",
             info->settings.port, what, err,
             (int) caen_hv::kReopenInterval.count());
   }
}

/// @brief open the serial port if it is not open, honouring the retry delay
/// @param quiet suppress the "cannot open" message because the caller reports
///              the failure itself (CMD_INIT does, with more context)
/// @return TRUE when info->fd is usable
static bool caen_hv_port_open(CAEN_HV_FE_INFO *info, bool quiet = false)
{
   if (info->fd >= 0) {
      return true;
   }

   auto now = std::chrono::steady_clock::now();
   if (info->have_last_open && now - info->last_open < caen_hv::kReopenInterval) {
      // too soon after the last attempt: fail fast, do not stall the poll loop
      return false;
   }
   info->last_open = now;
   info->have_last_open = true;

   // O_NONBLOCK on open() so a modem-control-less tty cannot block us, then
   // cleared: all read and write timing is done with select() below.
   int fd = open(info->settings.port, O_RDWR | O_NOCTTY | O_NONBLOCK);
   if (fd < 0) {
      info->last_open_errno = errno;
      if (!quiet && caen_hv_may_log(info, "open")) {
         CAEN_HV_MSG(MERROR, "cannot open HV %.40s: %.40s",
                info->settings.port, strerror(errno));
      }
      return false;
   }

   int fl = fcntl(fd, F_GETFL, 0);
   if (fl >= 0) {
      fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);
   }

   // Raw 9600 8N1, no flow control. A CDC-ACM device ignores the line
   // settings, an FTDI-based N1470 on /dev/ttyUSB* does not, so the same
   // code works for both.
   struct termios tio;
   memset(&tio, 0, sizeof(tio));
   if (tcgetattr(fd, &tio) != 0) {
      if (caen_hv_may_log(info, "termios")) {
         CAEN_HV_MSG(MERROR, "tcgetattr %.40s: errno %d", info->settings.port, errno);
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
   tio.c_cc[VMIN] = 0;    // never block in read(), select() does the waiting
   tio.c_cc[VTIME] = 0;
   if (tcsetattr(fd, TCSANOW, &tio) != 0) {
      if (caen_hv_may_log(info, "termios")) {
         CAEN_HV_MSG(MERROR, "tcsetattr %.40s: errno %d", info->settings.port, errno);
      }
      close(fd);
      return false;
   }
   tcflush(fd, TCIOFLUSH);

   info->fd = fd;
   info->connected = true;
   info->desync = false;
   info->zero_reads = 0;

   // A new fd means a possibly different unit (cable swap): re-read POL on the
   // next CMD_GET_STATUS of every channel.
   for (auto &ch : info->channel) {
      ch.pol_countdown = 0;
   }
   // The MINFO is *not* emitted here: open() succeeding on a USB tty says
   // nothing about the board answering. caen_hv_query() announces recovery
   // after the first good reply.
   return true;
}

/// @brief TRUE for the errno values that mean "the device went away"
static bool caen_hv_fatal_errno(int err)
{
   return err == ENXIO || err == EIO || err == ENODEV ||
          err == EBADF || err == EPIPE || err == ENOENT;
}

/// @brief discard whatever is already in the input queue
/// @return FALSE when the port was lost while draining
static bool caen_hv_drain(CAEN_HV_FE_INFO *info)
{
   for (;;) {
      fd_set readfds;
      FD_ZERO(&readfds);
      FD_SET(info->fd, &readfds);
      timeval timeout{0, 0};

      int n = select(info->fd + 1, &readfds, NULL, NULL, &timeout);
      if (n <= 0) {
         return true;   // nothing pending
      }

      char buf[256];
      ssize_t rd = read(info->fd, buf, sizeof(buf));
      if (rd > 0) {
         continue;   // discard
      }
      if (rd < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
         return true;
      }
      if (rd < 0 && caen_hv_fatal_errno(errno)) {
         caen_hv_port_lost(info, "read while draining", errno);
         return false;
      }
      return true;
   }
}

/// @brief after a timeout: discard everything until the port has been quiet
///        for a full reply timeout, so a late reply cannot be mistaken for the
///        answer to the next request
static void caen_hv_resync(CAEN_HV_FE_INFO *info)
{
   int timeout_ms = caen_hv_timeout_ms(info);
   // bound the total effort: a device babbling continuously must not stall
   // the poll loop for ever
   auto give_up = std::chrono::steady_clock::now() +
                  std::chrono::milliseconds(5 * timeout_ms);

   for (;;) {
      fd_set readfds;
      FD_ZERO(&readfds);
      FD_SET(info->fd, &readfds);
      timeval timeout{(time_t) (timeout_ms / 1000),
                      (suseconds_t) ((timeout_ms % 1000) * 1000)};

      int n = select(info->fd + 1, &readfds, NULL, NULL, &timeout);
      if (n == 0) {
         info->desync = false;    // quiet for a full timeout: back in step
         return;
      }
      if (n < 0) {
         if (errno == EINTR) {
            continue;
         }
         if (caen_hv_fatal_errno(errno)) {
            caen_hv_port_lost(info, "select while resyncing", errno);
         }
         return;
      }

      char buf[256];
      ssize_t rd = read(info->fd, buf, sizeof(buf));
      if (rd < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
         continue;
      }
      if (rd < 0) {
         if (caen_hv_fatal_errno(errno)) {
            caen_hv_port_lost(info, "read while resyncing", errno);
         }
         return;
      }
      if (rd == 0 && ++info->zero_reads >= 3) {
         caen_hv_port_lost(info, "read returned 0 bytes", 0);
         return;
      }

      if (std::chrono::steady_clock::now() >= give_up) {
         if (caen_hv_may_log(info, "resync")) {
            CAEN_HV_MSG(MERROR,
                   "HV %.40s babbling, cannot resync", info->settings.port);
         }
         return;    // desync stays set, we try again next time
      }
   }
}

/// @brief write the whole request, bounded by the reply timeout
/// @note the fd is blocking, so a device that never accepts data (full
///       kernel buffer, stopped USB endpoint) would hang write() for ever
///       inside sc_thread; hence select() on writability.
static bool caen_hv_write_all(CAEN_HV_FE_INFO *info, const std::string &req)
{
   int timeout_ms = caen_hv_timeout_ms(info);
   auto deadline = std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(timeout_ms);
   size_t done = 0;

   while (done < req.size()) {
      auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
         if (caen_hv_may_log(info, "write")) {
            CAEN_HV_MSG(MERROR, "HV %.40s write timeout (%d ms)",
                   info->settings.port, timeout_ms);
         }
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
         if (caen_hv_fatal_errno(errno)) {
            caen_hv_port_lost(info, "select for write", errno);
            return false;
         }
         if (caen_hv_may_log(info, "write")) {
            CAEN_HV_MSG(MERROR, "HV %.40s select-write: errno %d", info->settings.port, errno);
         }
         return false;
      }
      if (n == 0) {
         continue;   // deadline check at the top of the loop reports it
      }

      ssize_t w = write(info->fd, req.data() + done, req.size() - done);
      if (w > 0) {
         done += (size_t) w;
         continue;
      }
      if (w < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
         continue;
      }
      if (w < 0 && caen_hv_fatal_errno(errno)) {
         caen_hv_port_lost(info, "write", errno);
         return false;
      }
      if (caen_hv_may_log(info, "write")) {
         CAEN_HV_MSG(MERROR, "HV %.40s write: errno %d", info->settings.port, errno);
      }
      return false;
   }
   return true;
}

/// @brief read one reply line, up to the "Timeout ms" setting
/// @return the line without its terminator, or nullopt on timeout/error
static std::optional<std::string> caen_hv_read_line(CAEN_HV_FE_INFO *info)
{
   int timeout_ms = caen_hv_timeout_ms(info);
   auto deadline = std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(timeout_ms);
   std::string line;

   for (;;) {
      auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
         return std::nullopt;
      }
      auto left = std::chrono::duration_cast<std::chrono::microseconds>(deadline - now).count();

      fd_set readfds;
      FD_ZERO(&readfds);
      FD_SET(info->fd, &readfds);
      timeval timeout{(time_t) (left / 1000000), (suseconds_t) (left % 1000000)};

      int n = select(info->fd + 1, &readfds, NULL, NULL, &timeout);
      if (n < 0) {
         if (errno == EINTR) {
            continue;
         }
         if (caen_hv_fatal_errno(errno)) {
            caen_hv_port_lost(info, "select", errno);
         }
         return std::nullopt;
      }
      if (n == 0) {
         return std::nullopt;   // timeout
      }

      char buf[256];
      ssize_t rd = read(info->fd, buf, sizeof(buf));
      if (rd < 0) {
         if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            continue;
         }
         if (caen_hv_fatal_errno(errno)) {
            caen_hv_port_lost(info, "read", errno);
         }
         return std::nullopt;
      }
      if (rd == 0) {
         // select() said readable but there is nothing: an unplugged CDC-ACM
         // device does this forever, so give up after a few tries.
         if (++info->zero_reads >= 3) {
            caen_hv_port_lost(info, "read returned 0 bytes", 0);
         }
         return std::nullopt;
      }
      info->zero_reads = 0;
      line.append(buf, (size_t) rd);

      // accept "\r\n", a lone "\n" and a lone "\r" as the terminator
      size_t pos = line.find_first_of("\r\n");
      if (pos != std::string::npos) {
         line.erase(pos);
         return line;
      }
      if (line.size() > 1024) {
         return std::nullopt;   // runaway, treat as garbage
      }
   }
}

/// @brief one request / one reply
static std::optional<std::string> caen_hv_transact(CAEN_HV_FE_INFO *info,
                                                   const std::string &req)
{
   if (!caen_hv_port_open(info)) {
      return std::nullopt;
   }

   if (info->desync) {
      caen_hv_resync(info);
   } else {
      caen_hv_drain(info);
   }

   // drain/resync may have discovered that the device is gone and closed the
   // fd; writing to -1 would only produce a second "connection lost"
   if (!info->connected || info->desync) {
      return std::nullopt;
   }

   if (!caen_hv_write_all(info, req)) {
      return std::nullopt;
   }

   auto line = caen_hv_read_line(info);
   if (!line && info->connected) {
      // a reply may still show up later; do not let it answer the next request
      info->desync = true;
   }
   return line;
}

/// @brief strip trailing whitespace and control characters
static std::string caen_hv_rtrim(const std::string &s)
{
   size_t end = s.size();
   while (end > 0 && (unsigned char) s[end - 1] <= ' ') {
      --end;
   }
   return s.substr(0, end);
}

/// @brief build and run one protocol exchange
///
/// Requests are
/// @code
/// $BD:%02d,CMD:MON,CH:%d,PAR:%s\r\n
/// $BD:%02d,CMD:SET,CH:%d,PAR:%s,VAL:%s\r\n
/// @endcode
/// with the ",CH:%d" left out for board level parameters (@p ch < 0) and the
/// ",VAL:%s" left out when @p val is NULL. The reply is
/// @code
/// #BD:%02d,CMD:OK[,VAL:%s]
/// @endcode
///
/// @param ch  channel index, or < 0 for a board level parameter
/// @param val value for CMD:SET, or NULL
/// @return the VAL field (an empty string when the reply carries none), or
///         nullopt on any error; all errors are reported with a rate limited
///         cm_msg here, so callers only have to map nullopt to FE_ERR_HW.
static std::optional<std::string> caen_hv_query(CAEN_HV_FE_INFO *info,
                                                const char *cmd, int ch,
                                                const char *par, const char *val)
{
   char req[256];
   if (ch < 0) {
      if (val) {
         snprintf(req, sizeof(req), "$BD:%02d,CMD:%s,PAR:%s,VAL:%s\r\n",
                  info->settings.board, cmd, par, val);
      } else {
         snprintf(req, sizeof(req), "$BD:%02d,CMD:%s,PAR:%s\r\n",
                  info->settings.board, cmd, par);
      }
   } else {
      if (val) {
         snprintf(req, sizeof(req), "$BD:%02d,CMD:%s,CH:%d,PAR:%s,VAL:%s\r\n",
                  info->settings.board, cmd, ch, par, val);
      } else {
         snprintf(req, sizeof(req), "$BD:%02d,CMD:%s,CH:%d,PAR:%s\r\n",
                  info->settings.board, cmd, ch, par);
      }
   }

   auto reply = caen_hv_transact(info, req);
   if (!reply) {
      if (info->connected && caen_hv_may_log(info, "timeout")) {
         CAEN_HV_MSG(MERROR, "no reply in %d ms to %.4s PAR:%.8s",
                caen_hv_timeout_ms(info), cmd, par);
      }
      return std::nullopt;
   }

   const std::string line = caen_hv_rtrim(*reply);

   // any *:ERR is a refusal; LOC:ERR (board in LOCAL) gets its own message
   if (line.find(":ERR") != std::string::npos) {
      if (line.find("LOC:ERR") != std::string::npos) {
         if (caen_hv_may_log(info, "local")) {
            CAEN_HV_MSG(MERROR,
                   "board in LOCAL mode, sets ignored (set REMOTE)");
         }
      } else if (caen_hv_may_log(info, "err")) {
         CAEN_HV_MSG(MERROR, "board rejected %.4s PAR:%.8s VAL:%.12s: %.40s",
                cmd, par, val ? val : "-", line.c_str());
      }
      return std::nullopt;
   }

   // the reply must come from the board we addressed
   int rbd = -1;
   if (line.size() < 4 || line[0] != '#' ||
       sscanf(line.c_str(), "#BD:%d", &rbd) != 1) {
      if (caen_hv_may_log(info, "parse")) {
         CAEN_HV_MSG(MERROR, "bad reply to %.4s PAR:%.8s: '%.40s'", cmd, par, line.c_str());
      }
      return std::nullopt;
   }
   if (rbd != info->settings.board) {
      if (caen_hv_may_log(info, "board")) {
         CAEN_HV_MSG(MERROR, "reply from board %d, expected %d: '%.40s'",
                rbd, info->settings.board, line.c_str());
      }
      return std::nullopt;
   }
   if (line.find("CMD:OK") == std::string::npos) {
      if (caen_hv_may_log(info, "parse")) {
         CAEN_HV_MSG(MERROR, "no CMD:OK for %.4s PAR:%.8s: '%.40s'", cmd, par, line.c_str());
      }
      return std::nullopt;
   }

   // A well formed reply is the first proof that the board is really there.
   if (info->announce_recovery) {
      info->announce_recovery = false;
      if (caen_hv_may_log(info, "recovered")) {
         CAEN_HV_MSG(MINFO, "HV %.40s answering again", info->settings.port);
      }
   }

   size_t vpos = line.find("VAL:");
   if (vpos == std::string::npos) {
      return std::string();   // OK, no value (as for every CMD:SET)
   }
   return caen_hv_rtrim(line.substr(vpos + 4));
}

/*---- typed parameter access --------------------------------------*/

/// @brief MON one channel parameter as a float magnitude
static bool caen_hv_mon_float(CAEN_HV_FE_INFO *info, int ch, const char *par,
                              float *out)
{
   auto val = caen_hv_query(info, caen_hv::kCmdMon, ch, par, NULL);
   if (!val || val->empty()) {
      return false;
   }
   char *end = NULL;
   double d = strtod(val->c_str(), &end);
   if (end == val->c_str()) {
      if (caen_hv_may_log(info, "parse")) {
         CAEN_HV_MSG(MERROR, "PAR:%.8s CH:%d non-numeric '%.20s'", par, ch, val->c_str());
      }
      return false;
   }
   // The board reports magnitudes; POL carries the sign and is published
   // separately (see caen_hv_polarity()). fabs() guards against a firmware
   // that decides otherwise, so ODB never sees a negative voltage that
   // hv_demand()'s limit clamp would then fight (hv.cxx:428-436).
   *out = (float) std::fabs(d);
   return true;
}

/// @brief MON one channel parameter as an unsigned word (STAT)
static bool caen_hv_mon_dword(CAEN_HV_FE_INFO *info, int ch, const char *par,
                              DWORD *out)
{
   auto val = caen_hv_query(info, caen_hv::kCmdMon, ch, par, NULL);
   if (!val || val->empty()) {
      return false;
   }
   char *end = NULL;
   // base 10, never 0: the board zero-pads ("VAL:02048") and auto-detection would read that as octal
   unsigned long ul = strtoul(val->c_str(), &end, 10);
   if (end == val->c_str()) {
      if (caen_hv_may_log(info, "parse")) {
         CAEN_HV_MSG(MERROR, "PAR:%.8s CH:%d non-numeric '%.20s'", par, ch, val->c_str());
      }
      return false;
   }
   *out = (DWORD) ul;
   return true;
}

/// @brief MON POL and update the cache
/// @return +1 / -1, or 0 when the read failed or the answer was unexpected
static int caen_hv_read_polarity(CAEN_HV_FE_INFO *info, int ch)
{
   auto val = caen_hv_query(info, caen_hv::kCmdMon, ch, caen_hv::kParPol, NULL);
   if (!val) {
      return 0;
   }
   int pol = 0;
   if (val->find('+') != std::string::npos) {
      pol = +1;
   } else if (val->find('-') != std::string::npos) {
      pol = -1;
   } else if (caen_hv_may_log(info, "pol")) {
      CAEN_HV_MSG(MERROR, "PAR:POL CH:%d: '%.20s' is not + or -", ch, val->c_str());
   }
   if (pol != 0) {
      info->channel[ch].pol.store(pol);
      info->channel[ch].pol_countdown = caen_hv::kPolRefreshEvery;
   }
   return pol;
}

/// @brief SET one channel parameter from a float, using that parameter's format
/// @note values are magnitudes; the board refuses a negative or out of range
///       VAL with "VAL:ERR", which caen_hv_query() reports.
/// @param sent optional; on success receives the magnitude as it went over
///             the wire, i.e. after @p fmt rounded it. Caching that instead of
///             the caller's float keeps CMD_GET_DEMAND in step with what the
///             board will report back, so hv_read() does not rewrite
///             Variables/Demand once for the rounding difference.
static INT caen_hv_set_float(CAEN_HV_FE_INFO *info, int ch, const char *par,
                             const char *fmt, float value, float *sent = NULL)
{
   if (!std::isfinite(value)) {
      return FE_SUCCESS;   // nothing sensible to send, and not an error
   }
   char val[32];
   snprintf(val, sizeof(val), fmt, std::fabs((double) value));

   auto reply = caen_hv_query(info, caen_hv::kCmdSet, ch, par, val);
   if (!reply) {
      return FE_ERR_HW;
   }
   if (sent) {
      *sent = (float) strtod(val, NULL);
   }
   return FE_SUCCESS;
}

/// @brief SET RUP or RDW, refusing the value cd_hv defaults to
///
/// cd_hv creates Settings/Ramp Up/Down Speed with a default of 0
/// (hv.cxx:915-920). When the device read at hv_init() fails the ODB keeps
/// that 0, and the hotlink then sends "RUP VAL:0" on every startup and every
/// ODB touch - which the board rejects with VAL:ERR forever, because its
/// range is 1..500 V/s. Dropping the write keeps the message log readable and
/// leaves the board's own ramp speed in place.
static INT caen_hv_set_ramp(CAEN_HV_FE_INFO *info, int ch, const char *par,
                            const char *fmt, float value)
{
   if (std::isfinite(value) && value < caen_hv::kMinRampSpeed) {
      if (caen_hv_may_log(info, "ramp_zero")) {
         CAEN_HV_MSG(MERROR,
                "PAR:%.4s CH:%d: ramp %.3g V/s below %.3g, not sent",
                par, ch, (double) value, (double) caen_hv::kMinRampSpeed);
      }
      return FE_ERR_HW;
   }
   return caen_hv_set_float(info, ch, par, fmt, value);
}

int caen_hv_polarity(void *info, int ch)
{
   CAEN_HV_FE_INFO *hv = (CAEN_HV_FE_INFO *) info;
   if (!hv || ch < 0 || ch >= hv->num_channels) {
      return 0;
   }
   return hv->channel[ch].pol.load();
}

/*---- CMD_INIT / CMD_EXIT -----------------------------------------*/

INT caen_hv_fe_init(HNDLE hKey, void **pinfo, INT channels, INT(*bd)(INT cmd, ...))
{
   std::cout << "caen_hv init" << std::endl;
   (void) bd;   // no bus driver: this driver owns its serial port

   int status, size;
   HNDLE hDB;

   // allocate and initialise information object
   std::unique_ptr<CAEN_HV_FE_INFO> info = std::make_unique<CAEN_HV_FE_INFO>();
   info->hKey = hKey;
   info->num_channels = channels > 0 ? channels : 0;
   info->channel = std::vector<CAEN_HV_CHANNEL>(info->num_channels);

   // Synchronise with ODB
   cm_get_experiment_database(&hDB, NULL);

   status = db_create_record(hDB, hKey, "./", CAEN_HV_SETTINGS_STRING);
   if (status != DB_SUCCESS) {
      std::cerr << "Failed to create record" << std::endl;
      return FE_ERR_ODB;
   }

   size = sizeof(info->settings.port);
   db_get_value(hDB, hKey, "Port", info->settings.port, &size, TID_STRING, FALSE);
   size = sizeof(int);
   db_get_value(hDB, hKey, "Board Address", &info->settings.board, &size, TID_INT32, FALSE);
   size = sizeof(int);
   db_get_value(hDB, hKey, "Timeout ms", &info->settings.timeout_ms, &size, TID_INT32, FALSE);
   size = sizeof(int);
   db_get_value(hDB, hKey, "Expected Channels", &info->settings.expected_channels,
                &size, TID_INT32, FALSE);

   // The "$BD:%02d" framing breaks above 99 and the bus only goes to 31.
   if (info->settings.board < 0 || info->settings.board > caen_hv::kMaxBoardAddress) {
      int clamped = info->settings.board < 0 ? 0 : caen_hv::kMaxBoardAddress;
      CAEN_HV_MSG(MERROR, "Board Address %d out of 0..%d, using %d",
             info->settings.board, caen_hv::kMaxBoardAddress, clamped);
      info->settings.board = clamped;
   }

   // Try to open the port, but do *not* fail init if it is not there:
   // hv_init() aborts on FE_ERR_HW (hv.cxx:766-770), so CMD_START would never
   // run, /Equipment/.../Variables would never be created and the 5 s reopen
   // backoff below could never heal the session. Coming up with ODB defaults
   // and an empty cache is strictly better: every read then reports NaN /
   // ChStatus STALE / ChState 0 until the cable appears, after which the
   // driver picks the board up on its own.
   // The helper is asked to stay quiet so this is the *only* message about a
   // missing port at startup.
   if (!caen_hv_port_open(info.get(), true)) {
      CAEN_HV_MSG(MERROR,
             "HV %.40s: %.30s - no device, retry %ds",
             info->settings.port, strerror(info->last_open_errno),
             (int) caen_hv::kReopenInterval.count());
      // Arm the recovery notice: without this only a *loss* would announce a
      // later reconnect, and a device attached after the frontend started
      // would come up silently.
      info->announce_recovery = true;
      *pinfo = info.release();
      return FE_SUCCESS;
   }

   // --- identify the board -----------------------------------------
   auto bdname = caen_hv_query(info.get(), caen_hv::kCmdMon, -1, caen_hv::kParBdName, NULL);
   auto bdnch  = caen_hv_query(info.get(), caen_hv::kCmdMon, -1, caen_hv::kParBdNCh, NULL);
   auto bdfrel = caen_hv_query(info.get(), caen_hv::kCmdMon, -1, caen_hv::kParBdFRel, NULL);
   auto bdsnum = caen_hv_query(info.get(), caen_hv::kCmdMon, -1, caen_hv::kParBdSNum, NULL);
   auto bdctr  = caen_hv_query(info.get(), caen_hv::kCmdMon, -1, caen_hv::kParBdCtr, NULL);

   CAEN_HV_MSG(MINFO,
          "CAEN HV %.12s fw %.10s sn %.12s ctrl %.8s",
          bdname ? bdname->c_str() : "?",
          bdfrel ? bdfrel->c_str() : "?",
          bdsnum ? bdsnum->c_str() : "?",
          bdctr  ? bdctr->c_str()  : "?");

   // Mismatches are reported but do not fail init: monitoring the channels we
   // do have is more useful than a disabled equipment.
   if (bdnch) {
      int nch = atoi(bdnch->c_str());
      if (nch != channels) {
         CAEN_HV_MSG(MERROR,
                "board has %d channels, scfe.cxx configures %d", nch, (int) channels);
      }
      if (info->settings.expected_channels > 0 &&
          nch != info->settings.expected_channels) {
         CAEN_HV_MSG(MERROR,
                "board has %d channels, ODB Expected Channels %d",
                nch, info->settings.expected_channels);
      }
   }
   if (bdctr && bdctr->find("LOCAL") != std::string::npos) {
      CAEN_HV_MSG(MERROR,
             "board in LOCAL mode: sets refused until set to REMOTE");
   }

   // --- per channel initial state ----------------------------------
   // POL is cached here (and refreshed every kPolRefreshEvery CMD_GET_STATUS,
   // plus after every reconnect, so flipping the rear polarity switch is
   // noticed). VSET is read so that CMD_GET_DEMAND has a real value to return
   // before the poll thread's first sweep. STAT is read so that
   // CMD_SET_CHSTATE knows the true state before hv_init()'s own hotlink
   // starts re-sending it.
   for (int i = 0; i < info->num_channels; i++) {
      caen_hv_read_polarity(info.get(), i);

      float vset = 0.f;
      if (caen_hv_mon_float(info.get(), i, caen_hv::kParVSet, &vset)) {
         info->channel[i].vset = vset;
         info->channel[i].vset_valid = true;
      }

      DWORD stat = 0;
      if (caen_hv_mon_dword(info.get(), i, caen_hv::kParStat, &stat)) {
         info->channel[i].stat = stat;
         info->channel[i].stat_valid = true;
      }
   }

   // Transfer ownership to MIDAS now that all has succeeded.
   *pinfo = info.release();
   return FE_SUCCESS;
}

INT caen_hv_fe_exit(CAEN_HV_FE_INFO *info)
{
   if (info) {
      delete info;
   }

   std::cout << "caen_hv exit" << std::endl;
   return FE_SUCCESS;
}

/*---- multithreaded get commands ----------------------------------*/

/// @brief CMD_GET_FIRST .. CMD_GET_LAST, all issued from MIDAS's sc_thread
///
/// @warning CMD_GET_STATUS hands us a @c float* that really points at a
///          @c DWORD (hv.cxx:159-170 passes &hv_info->chStatus[i]); the value
///          only travels through sc_thread's float buffer by plain copy. So the
///          status word has to be written bit-preserving, never converted and
///          never NaN - see caen_hv_write_invalid(). A 14 bit STAT word is a
///          denormal float, hence the build must not use -ffast-math /
///          flush-to-zero, which would replace it with 0.
static INT caen_hv_fe_get(CAEN_HV_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
   if (channel < 0 || channel >= info->num_channels) {
      caen_hv_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
   CAEN_HV_CHANNEL &ch = info->channel[channel];

   switch (cmd) {

   case CMD_GET: {           // VMON, measured voltage
      float v = 0.f;
      if (!caen_hv_mon_float(info, channel, caen_hv::kParVMon, &v)) {
         // NaN on purpose here: cd_hv's Measured block does rescue it
         // (hv.cxx:220-221, "measured not NaN while mirror is NaN" forces an
         // update), and NaN is the honest "no reading" the logger filters.
         // Contrast CMD_GET_CURRENT and CMD_GET_DEMAND below.
         *pvalue = (float) ss_nan();
         return FE_ERR_HW;
      }
      *pvalue = v;
      return FE_SUCCESS;
   }

   case CMD_GET_CURRENT: {   // IMON, measured current [uA]
      float i = 0.f;
      if (caen_hv_mon_float(info, channel, caen_hv::kParIMon, &i)) {
         ch.imon = i;
         ch.imon_valid = true;
         *pvalue = i;
         return FE_SUCCESS;
      }
      // Never NaN, unlike CMD_GET above: cd_hv's Current block has no NaN
      // rescue (hv.cxx:242-268) - with current_mirror == NaN both
      // `ABS(x - NaN) > threshold` and `NaN > max_diff` are false, so
      // Variables/Current would never be written again for the rest of the
      // session, not even after the link comes back. Do not "simplify" this
      // to ss_nan(). kCurrentNeverRead (-1.0, impossible for a magnitude)
      // covers the case where the board never answered at all - reachable
      // because CMD_INIT deliberately succeeds on a dead port.
      *pvalue = ch.imon_valid ? ch.imon : caen_hv::kCurrentNeverRead;
      return FE_ERR_HW;
   }

   case CMD_GET_DEMAND: {    // VSET, polled because of DF_POLL_DEMAND
      float v = 0.f;
      if (caen_hv_mon_float(info, channel, caen_hv::kParVSet, &v)) {
         ch.vset = v;
         ch.vset_valid = true;
         *pvalue = v;
         return FE_SUCCESS;
      }
      // Never NaN: hv_read() compares demand to its mirror with != and would
      // rewrite Variables/Demand on every idle cycle (hv.cxx:272-282). Hand
      // back the last good VSET instead; only a board that never answered
      // since CMD_INIT leaves us with nothing to say.
      *pvalue = ch.vset_valid ? ch.vset : (float) ss_nan();
      return FE_ERR_HW;
   }

   case CMD_GET_STATUS: {    // STAT word, *not* a float (see warning above)
      // Refresh the cached polarity occasionally while we are talking to this
      // channel; POL only changes when a rear panel switch is flipped.
      if (--ch.pol_countdown <= 0) {
         caen_hv_read_polarity(info, channel);
         ch.pol_countdown = caen_hv::kPolRefreshEvery;
      }

      DWORD word = 0;
      if (caen_hv_mon_dword(info, channel, caen_hv::kParStat, &word)) {
         ch.stat = word;
         ch.stat_valid = true;
         *reinterpret_cast<DWORD *>(pvalue) = word;
         return FE_SUCCESS;
      }
      // Report the last good word with the driver-private stale flag, so
      // Variables/ChStatus shows the problem without inventing an alarm bit.
      *reinterpret_cast<DWORD *>(pvalue) =
         (ch.stat_valid ? ch.stat : 0u) | caen_hv::kStatStale;
      return FE_ERR_HW;
   }

   case CMD_GET_TRIP:        // no trip *voltage* readback on this board
   case CMD_GET_TEMPERATURE: // no per channel temperature either
      // sc_thread polls every command in the range regardless of flags
      // (device_driver.cxx), so answer without any serial traffic.
      *pvalue = (float) ss_nan();
      return FE_SUCCESS;

   default:
      caen_hv_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
}

/*---- direct get commands -----------------------------------------*/

/// @brief CMD_GET_DIRECT .. CMD_GET_DIRECT_LAST
///
/// @note only issued by hv_init() on the main thread, before CMD_START creates
///       the poll thread (mfe.cxx), so there is no concurrent serial access.
/// @note on a failed read of a *settings* parameter @c *pvalue is left
///       untouched on purpose. hv_init() passes a pointer into its own arrays,
///       which already hold the ODB value (or cd_hv's default,
///       hv.cxx:601-607), and writes them straight back with db_set_record().
///       Storing a NaN there would wipe the operator's software limit:
///       hv_demand()'s clamp is `fabs(demand) > fabs(voltage_limit)`, which is
///       false for any NaN limit, so no demand would ever be clamped again.
static INT caen_hv_fe_get_direct(CAEN_HV_FE_INFO *info, INT channel, float *pvalue, INT cmd)
{
   if (channel < 0 || channel >= info->num_channels) {
      caen_hv_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }
   CAEN_HV_CHANNEL &ch = info->channel[channel];

   const char *par = NULL;
   switch (cmd) {
   case CMD_GET_VOLTAGE_LIMIT: par = caen_hv::kParMaxV; break;
   case CMD_GET_CURRENT_LIMIT: par = caen_hv::kParISet; break;
   case CMD_GET_RAMPUP:        par = caen_hv::kParRUp;  break;
   case CMD_GET_RAMPDOWN:      par = caen_hv::kParRDwn; break;
   case CMD_GET_TRIP_TIME:     par = caen_hv::kParTrip; break;

   case CMD_GET_CHSTATE: {
      // Same DWORD-behind-a-float* punning as CMD_GET_STATUS: hv_init() passes
      // &hv_info->chState[i], a DWORD* (hv.cxx:1031-1037, and
      // validate_odb_array_bool at hv.cxx:1075-1077).
      DWORD word = 0;
      if (caen_hv_mon_dword(info, channel, caen_hv::kParStat, &word)) {
         ch.stat = word;
         ch.stat_valid = true;
         *reinterpret_cast<DWORD *>(pvalue) =
            (word & (1u << caen_hv::kStatOn)) ? 1u : 0u;
         return FE_SUCCESS;
      }
      // 0 on error: hv_init() writes this straight into Variables/ChState and
      // its hotlink then sends it back to us, so "off" is the safe answer.
      *reinterpret_cast<DWORD *>(pvalue) = 0u;
      return FE_ERR_HW;
   }

   case CMD_GET_DEMAND_DIRECT:
      // Unreachable with DF_POLL_DEMAND (hv.cxx takes the other branch), but
      // cheap to serve from the cache filled at CMD_INIT.
      if (!ch.vset_valid) {
         return FE_ERR_HW;      // leave the caller's value alone
      }
      *pvalue = ch.vset;
      return FE_SUCCESS;

   default:
      caen_hv_write_invalid(pvalue, cmd);
      return FE_SUCCESS;
   }

   float v = 0.f;
   if (!caen_hv_mon_float(info, channel, par, &v)) {
      return FE_ERR_HW;         // keep the ODB / default value, see above
   }
   *pvalue = v;
   return FE_SUCCESS;
}

/*---- set commands ------------------------------------------------*/

/// @brief CMD_SET_CHSTATE -> PAR:ON / PAR:OFF
///
/// Asymmetric on purpose. hv_init() writes Variables/ChState from the device
/// (hv.cxx:1052-1054) and MIDAS hotlinks fire for the writing client too, so
/// hv_set_chState() re-sends every channel's state at startup. A garbage read
/// must not turn into an ON, hence switching *on* requires
///   - a value of exactly 1.0, and
///   - a STAT word that was really read back, and
///   - that STAT says the channel is currently off.
/// Switching *off* is the safe direction and is always forwarded, except when
/// STAT already confirmed the channel is off (then there is nothing to do).
static INT caen_hv_set_chstate(CAEN_HV_FE_INFO *info, INT channel, float value)
{
   CAEN_HV_CHANNEL &ch = info->channel[channel];

   if (value != 0.0f && value != 1.0f) {
      if (caen_hv_may_log(info, "chstate_value")) {
         CAEN_HV_MSG(MERROR,
                "ChState[%d] = %.3g ignored, expect exactly 0 or 1",
                (int) channel, (double) value);
      }
      return FE_SUCCESS;
   }

   bool want_on = (value == 1.0f);
   bool is_on = (ch.stat & (1u << caen_hv::kStatOn)) != 0;

   if (want_on) {
      if (!ch.stat_valid) {
         if (caen_hv_may_log(info, "chstate_unknown")) {
            CAEN_HV_MSG(MERROR,
                   "ch %d: ON refused, state never read from board", (int) channel);
         }
         return FE_ERR_HW;
      }
      if (is_on) {
         return FE_SUCCESS;   // already on, do not touch the output
      }
   } else {
      if (ch.stat_valid && !is_on) {
         return FE_SUCCESS;   // confirmed off, nothing to do
      }
      // state unknown: send OFF anyway, it can only make things safer
   }

   auto reply = caen_hv_query(info, caen_hv::kCmdSet, channel,
                              want_on ? caen_hv::kParOn : caen_hv::kParOff, NULL);
   if (!reply) {
      return FE_ERR_HW;
   }

   // CMD:OK does *not* mean the channel switched. Measured on a DT1470ET with
   // firmware 1.08: with the channel's front switch in OFF or KILL the board
   // acknowledges PAR:ON and does nothing (STAT keeps caen_hv::kStatSwitchMask
   // set, VMON stays 0). An optimistic cache update would then report a
   // channel as on that is not, and keep ODB's ChState out of step with the
   // hardware. So always re-read STAT and believe only the board.
   ss_sleep(caen_hv::kChStateSettleMs);

   DWORD word = 0;
   if (!caen_hv_mon_dword(info, channel, caen_hv::kParStat, &word)) {
      // The switch may or may not have happened; saying nothing is better
      // than guessing. Dropping stat_valid also re-arms the "ON needs a
      // confirmed state" guard until the next good CMD_GET_STATUS.
      ch.stat_valid = false;
      if (caen_hv_may_log(info, "chstate_unconfirmed")) {
         CAEN_HV_MSG(MERROR,
                "ch %d: %.3s accepted, state not readable back",
                (int) channel, want_on ? "ON" : "OFF");
      }
      return FE_ERR_HW;
   }
   ch.stat = word;
   ch.stat_valid = true;

   bool now_on = (word & (1u << caen_hv::kStatOn)) != 0;

   if (want_on && !now_on) {
      CAEN_HV_MSG(MERROR,
             "ch %d: ON accepted, not executed (STAT %.32s)"
             " - check front switch KILL/OFF/ON",
             (int) channel, caen_hv::stat_text(word).c_str());
      return FE_ERR_HW;
   }

   if (!want_on && now_on) {
      // A channel that is ramping down is executing the OFF, that is not a
      // failure. @todo confirm on hardware whether STAT clears bit 0 at once
      // or only at the end of the ramp.
      if (!(word & (1u << caen_hv::kStatRDwn))) {
         if (caen_hv_may_log(info, "chstate_off_pending")) {
            CAEN_HV_MSG(MERROR,
                   "ch %d: OFF accepted but still reports ON (STAT %.32s)",
                   (int) channel, caen_hv::stat_text(word).c_str());
         }
      }
      return FE_SUCCESS;
   }

   CAEN_HV_MSG(MINFO, "ch %d switched %.3s (STAT %.32s)",
          (int) channel, want_on ? "on" : "off",
          caen_hv::stat_text(word).c_str());
   return FE_SUCCESS;
}

/// @brief CMD_SET_FIRST .. CMD_SET_LAST, all issued from MIDAS's sc_thread
static INT caen_hv_fe_set(CAEN_HV_FE_INFO *info, INT channel, float value, INT cmd)
{
   if (channel < 0 || channel >= info->num_channels) {
      return FE_SUCCESS;
   }

   switch (cmd) {
   case CMD_SET: {           // demand voltage
      float sent = 0.f;
      INT status = caen_hv_set_float(info, channel, caen_hv::kParVSet,
                                     caen_hv::kFmtVSet, value, &sent);
      if (status == FE_SUCCESS && std::isfinite(value)) {
         // keep the demand cache in step so CMD_GET_DEMAND cannot briefly
         // hand ODB the old set point back
         info->channel[channel].vset = sent;
         info->channel[channel].vset_valid = true;
      }
      return status;
   }
   case CMD_SET_VOLTAGE_LIMIT:
      return caen_hv_set_float(info, channel, caen_hv::kParMaxV,
                               caen_hv::kFmtMaxV, value);
   case CMD_SET_CURRENT_LIMIT:
      return caen_hv_set_float(info, channel, caen_hv::kParISet,
                               caen_hv::kFmtISet, value);
   case CMD_SET_RAMPUP:
      return caen_hv_set_ramp(info, channel, caen_hv::kParRUp,
                              caen_hv::kFmtRUp, value);
   case CMD_SET_RAMPDOWN:
      return caen_hv_set_ramp(info, channel, caen_hv::kParRDwn,
                              caen_hv::kFmtRDwn, value);
   case CMD_SET_TRIP_TIME:
      return caen_hv_set_float(info, channel, caen_hv::kParTrip,
                               caen_hv::kFmtTrip, value);
   case CMD_SET_CHSTATE:
      return caen_hv_set_chstate(info, channel, value);
   default:
      return FE_SUCCESS;
   }
}

/*---- driver entry point ------------------------------------------*/

INT caen_hv_fe(INT cmd, ...)
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
      status = caen_hv_fe_init(hKey, (void **) info, channel, bd);
      break;

   case CMD_EXIT:
      info = va_arg(argptr, void *);
      status = caen_hv_fe_exit((CAEN_HV_FE_INFO *) info);
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
      status = caen_hv_fe_get((CAEN_HV_FE_INFO *) info, channel, pvalue, cmd);
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
      status = caen_hv_fe_get_direct((CAEN_HV_FE_INFO *) info, channel, pvalue, cmd);
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
      status = caen_hv_fe_set((CAEN_HV_FE_INFO *) info, channel, value, cmd);
      break;

   case CMD_GET_THRESHOLD:
   case CMD_GET_THRESHOLD_CURRENT:
   case CMD_GET_THRESHOLD_ZERO:
      // ODB update thresholds, no serial traffic
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      pvalue = va_arg(argptr, float *);
      (void) info;
      (void) channel;
      if (cmd == CMD_GET_THRESHOLD) {
         *pvalue = caen_hv::kThresholdVoltage;
      } else if (cmd == CMD_GET_THRESHOLD_CURRENT) {
         *pvalue = caen_hv::kThresholdCurrent;
      } else {
         *pvalue = caen_hv::kThresholdZero;
      }
      status = FE_SUCCESS;
      break;

   case CMD_GET_LABEL: {
      info = va_arg(argptr, void *);
      channel = va_arg(argptr, INT);
      name = va_arg(argptr, char *);
      CAEN_HV_FE_INFO *hv = (CAEN_HV_FE_INFO *) info;
      if (hv && channel >= 0 && channel < hv->num_channels) {
         // "%CH" is the MIDAS convention for a default name that mhttpd may
         // replace (see hv.cxx:808, "Default%%CH %d")
         snprintf(name, NAME_LENGTH, "HV%%CH %d", (int) channel);
      } else {
         name[0] = 0;
      }
      status = FE_SUCCESS;
      break;
   }

   default:
      // We are ignoring a bunch of possible commands: CMD_SET_LABEL (the board
      // has no channel name parameter), CMD_START/CMD_STOP/CMD_IDLE,
      // CMD_GET_CRATEMAP. This is not an error but a dedicated choice that
      // this FE does not provide these fields.
      break;
   }

   va_end(argptr);
   return status;
}
