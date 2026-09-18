#ifndef CAEN_HV_FE_H
#define CAEN_HV_FE_H

#include <string>

#include <midas.h>

/*---- channel status word (public: the single source of truth) -----*/
// These bit numbers come from the N1470 family manual and are to be corrected
// after the first hardware run; single source of truth, do not copy them.

namespace caen_hv {

   /// @brief STAT bit numbers as reported by the board
   enum stat_bit_t {
      kStatOn    =  0,  ///< channel is on
      kStatRUp   =  1,  ///< ramping up
      kStatRDwn  =  2,  ///< ramping down
      kStatOvC   =  3,  ///< over current
      kStatOvV   =  4,  ///< over voltage
      kStatUnV   =  5,  ///< under voltage
      kStatMaxV  =  6,  ///< at MAXV
      kStatTrip  =  7,  ///< tripped
      kStatOvP   =  8,  ///< over power
      kStatOvT   =  9,  ///< over temperature
      kStatDis   = 10,  ///< disabled
      kStatKill  = 11,  ///< killed
      kStatIlk   = 12,  ///< interlock
      kStatNoCal = 13   ///< not calibrated
   };

   /// @brief number of defined STAT bits, i.e. one past kStatNoCal
   constexpr int kStatNumBits = 14;

   /// @brief driver-private "stale" flag OR'ed into Variables/ChStatus when
   ///        the STAT read failed. Bit 31 is far above the bits the board
   ///        uses, so it cannot collide with a real alarm bit. Alarm masks
   ///        must not include it.
   constexpr DWORD kStatStale = 1u << 31;

   /// @brief STAT bits that reflect the channel's 3-position front switch
   ///
   /// Measured on a DT1470ET with firmware 1.08: the switch is reported in
   /// STAT, not in a parameter of its own -
   ///   KILL -> bit 11 (kStatKill, 2048)
   ///   OFF  -> bit 10 (kStatDis,  1024)
   ///   ON   -> neither bit set
   /// While either bit is set the board still answers CMD:OK to PAR:ON but
   /// does not switch the channel on, so a set is acknowledged and silently
   /// not executed. Use this mask to tell an operator-disabled channel from a
   /// fault before raising an alarm about it.
   constexpr DWORD kStatSwitchMask = (1u << kStatDis) | (1u << kStatKill);

   /// @brief Variables/Current value meaning "IMON was never read"
   ///
   /// CMD_GET_CURRENT must never report NaN, because cd_hv's Current block has
   /// no NaN rescue: once current_mirror is NaN, both `ABS(x - NaN) > thr` and
   /// `NaN > max_diff` are false, so Variables/Current is never written again
   /// for the rest of the session (hv.cxx:242-268). A measured current is a
   /// magnitude, so a negative value cannot occur and makes an unambiguous
   /// sentinel: -1.0 means "the driver has not managed to read IMON yet".
   /// Variables/Measured is *not* treated this way - there NaN is the honest
   /// signal and cd_hv rescues it (hv.cxx:220-221).
   constexpr float kCurrentNeverRead = -1.0f;

   /// @brief name of one STAT bit, e.g. "OVC"
   /// @param bit a stat_bit_t value
   /// @return the name, or "?" when @p bit is out of range
   const char *stat_bit_name(int bit);

   /// @brief decode a STAT word into "ON|OVC|TRIP" for a log or alarm message
   /// @return "none" when no known bit is set; kStatStale prints as "STALE"
   std::string stat_text(DWORD stat);

}  // namespace caen_hv

/// @brief MIDAS device driver entry point for a CAEN DT1470ET HV supply.
///
/// Serves the stock @c cd_hv class driver. Expected DEVICE_DRIVER flags:
/// @code
/// DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP |
/// DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND
/// @endcode
///
/// @warning Call this only the way MIDAS does. The serial port, the driver's
///          value cache and its message rate limiter carry no lock: they
///          belong to the main thread until CMD_START and to MIDAS's sc_thread
///          afterwards. In particular the direct gets (>= CMD_GET_DIRECT),
///          CMD_GET_LABEL/CMD_SET_LABEL and the CMD_GET_THRESHOLD* commands are
///          only valid before CMD_START, where hv_init() issues them; calling
///          any of them at run time would race sc_thread on the port and
///          corrupt the rate limiter's std::map. caen_hv_polarity() below is
///          the only entry point that is safe at run time.
INT caen_hv_fe(INT cmd, ...);

/// @brief Polarity of one channel as last read from the board.
///
/// The DT1470ET polarity is set by a physical switch per channel and @c POL is
/// read only. All values this driver exchanges with MIDAS are magnitudes, so
/// the sign has to be published separately (Variables/Polarity, WP3).
///
/// @param info the driver's private info pointer (DEVICE_DRIVER::dd_info)
/// @param ch   channel index
/// @return +1 for a positive channel, -1 for a negative one, 0 if unknown
///         (never read, read failed, or bad channel/info).
/// @note Reads only a cached value, refreshed on every CMD_GET_STATUS; safe to
///       call from the main thread while the poll thread is running.
int caen_hv_polarity(void *info, int ch);

#endif // CAEN_HV_FE_H
