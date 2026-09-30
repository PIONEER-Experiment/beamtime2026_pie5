#ifndef ISEG_NHQ_FE_H
#define ISEG_NHQ_FE_H

#include <string>

#include <midas.h>

/*---- channel status word (public: the single source of truth) -----*/
// Variables/ChStatus of the IsegHV equipment. The NHQ has no status *word*
// of its own: it answers a status *text* to S<n> ("S2=ON ") and a module
// status *byte* to T<n>. The driver folds both, plus a few conditions only it
// can see, into one DWORD laid out below. hv_alarm.cxx and custom/caenhv.html
// take the bit numbers and names from here; do not copy them anywhere else.
//
// Layout:
//   bits  0..10  the S<n> status text, exactly one bit set per successful read
//                (the unit reports a single word); 0 while S is not polled
//                (autostart armed) or never read
//   bits 16..23  the T<n> module status byte shifted up by 16, bit 16 (the
//                DISPLAY bit, which only says what the front display shows)
//                deliberately dropped
//   bits 24..26  conditions the driver detects
//   bit  31      stale: the last S or T read failed, the other bits are the
//                last good ones

namespace iseg_nhq {

   /// @brief bit numbers of Variables/ChStatus, see the layout above
   enum stat_bit_t {
      // --- S<n> status text (NHQ manual, "status word") ---
      kStatOn        =  0,  ///< "ON":  output voltage has reached the set voltage (also at D = 0!)
      kStatOff       =  1,  ///< "OFF": channel switched off at the front panel (HV-ON switch)
      kStatMan       =  2,  ///< "MAN": channel is on but under manual control (CONTROL switch up)
      kStatErr       =  3,  ///< "ERR": Vmax or Imax is or was exceeded
      kStatInh       =  4,  ///< "INH": INHIBIT signal is or was active
      kStatQua       =  5,  ///< "QUA": quality of the output voltage not given at present
      kStatL2H       =  6,  ///< "L2H": output voltage increasing (ramping up)
      kStatH2L       =  7,  ///< "H2L": output voltage decreasing (ramping down)
      kStatLas       =  8,  ///< "LAS": look at status (only after a G command)
      kStatTrp       =  9,  ///< "TRP": the current trip (L<n>) was active; output shut off
      kStatSUnknown  = 10,  ///< the S<n> text was none of the ten words above

      // --- T<n> module status byte, bit (16 + k) = T bit value (1 << k) ---
      kStatTMan      = 17,  ///< T   2: control is manual (clear = via RS232/DAC)
      kStatTPolPos   = 18,  ///< T   4: polarity switch positive (clear = negative)
      kStatTHvOff    = 19,  ///< T   8: front panel HV-ON switch in the OFF position
      kStatTKillEna  = 20,  ///< T  16: KILL switch in the ENABLE position
      kStatTInh      = 21,  ///< T  32: INHIBIT is or was active
      kStatTErr      = 22,  ///< T  64: Vmax or Imax is or was exceeded
      kStatTQua      = 23,  ///< T 128: quality of the output voltage not given

      // --- driver-detected conditions ---
      /// A<n> (autostart / EEPROM flags) read nonzero at connect. While set
      /// the driver never reads S<n> (a read of S can restore a shut-off
      /// voltage on its own when autostart is armed) and refuses every write
      /// except ChState OFF: D<n>=0 (no G with the autostart bit set, since D
      /// then ramps by itself), also for an OFF latched while the link was down.
      /// Cleared only by a reconnect or frontend restart that reads A<n> = 0.
      kStatAutostart = 24,
      /// the unit answered "?TOT" (timeout error, it re-initialises itself)
      /// since the previous status poll; reported once, then cleared
      kStatTot       = 25,
      /// the last D<n> read was nonzero, i.e. a set point is applied. Together
      /// with the S text this is how "voltage on" is told from "ON at 0 V",
      /// see stat_is_on().
      kStatDSet      = 26
   };

   /// @brief one past the highest defined bit (bits 11..16 are unused)
   constexpr int kStatNumBits = 27;

   /// @brief first bit of the T<n> byte inside the status word
   constexpr int kStatTShift = 16;

   /// @brief driver-private "stale" flag OR'ed into Variables/ChStatus when
   ///        the S or T read failed. Not a device condition: alarm masks must
   ///        not include it (hv_alarm uses it for the Comm alarm).
   constexpr DWORD kStatStale = 1u << 31;

   /// @brief default Settings/Alarm/Status Mask: bits that mean a fault
   ///
   /// ERR, INH, TRP and an unrecognised S text; the T byte's ERR and INH
   /// (still visible while S is not polled); AUTOSTART (the frontend has
   /// stopped controlling the channel); TOT (the unit re-initialised).
   /// Not included, because they are operator states rather than faults:
   /// OFF / MAN / T_HVOFF / T_MAN (front panel switches), KILL_ENA,
   /// POL, QUA, LAS, the ramp bits, ON and DSET. A channel that is supposed
   /// to be on but is not is caught by hv_alarm's "ChState ON but unit not
   /// on" check through stat_is_on().
   constexpr DWORD kStatFaultMask =
      (1u << kStatErr) | (1u << kStatInh) | (1u << kStatTrp) |
      (1u << kStatSUnknown) |
      (1u << kStatTErr) | (1u << kStatTInh) |
      (1u << kStatAutostart) | (1u << kStatTot);

   /// @brief Variables/Current value meaning "I<n> was never read"
   ///
   /// Same convention as caen_hv::kCurrentNeverRead: CMD_GET_CURRENT must
   /// never report NaN, because cd_hv's Current block would then never write
   /// Variables/Current again (hv.cxx:242-268). A measured current is a
   /// magnitude, so -1 cannot occur.
   constexpr float kCurrentNeverRead = -1.0f;

   /// @brief name of one status bit, e.g. "TRP" or "T_HVOFF"
   /// @param bit a stat_bit_t value
   /// @return the name, or "?" for an undefined or out of range bit
   const char *stat_bit_name(int bit);

   /// @brief decode a status word into "ON|T_POS|DSET" for a log or alarm message
   /// @return "none" when no known bit is set; kStatStale prints as "STALE"
   std::string stat_text(DWORD stat);

   /// @brief TRUE when the status word says voltage is applied to the output
   ///
   /// The S text "ON" only means "output has reached the set voltage", which
   /// the NHQ also reports at D = 0. So: ramping (L2H or H2L), or ON with a
   /// nonzero set point (DSET). OFF, MAN, ERR, INH and TRP are separate S
   /// texts and so are never "on". While S is not polled (AUTOSTART set) the
   /// T byte decides: on = DSET and none of T_HVOFF, T_MAN, T_ERR, T_INH (with
   /// autostart the output follows D unless one of those is set, NHQ manuals
   /// "Auto start"). The stale bit is not looked at: the
   /// caller decides whether a stale word is usable.
   bool stat_is_on(DWORD stat);

   /// @brief TRUE while the S text says the output is ramping (L2H or H2L)
   bool stat_is_ramping(DWORD stat);

}  // namespace iseg_nhq

/// @brief MIDAS device driver entry point for an iseg NHQ 208L HV supply.
///
/// Serves the stock @c cd_hv class driver with ONE MIDAS channel (named "S5"),
/// which is the hardware channel given by the driver setting
/// "Hardware Channel" (1 or 2, default 2). Expected DEVICE_DRIVER flags:
/// @code
/// DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP |
/// DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND
/// @endcode
///
/// The NHQ has no software on/off. ChState is emulated on the set point:
/// ON writes D<n>=<demand>, reads it back and sends G<n>; OFF writes D<n>=0
/// and sends G<n> (ramp to 0 V). At start ChState = (D<n> != 0), and nothing
/// is written, so a frontend restart never moves the voltage:
///  - every Settings value hv_init() writes back at start (Voltage/Current
///    Limit, Ramp Up/Down, ChState) is dropped when it comes back as a
///    CMD_SET_*, whether or not the register could be read;
///  - a unit found running above the Voltage Limit is reported to cd_hv with
///    a limit of at least its D<n> (plus an error message), so cd_hv's demand
///    clamp cannot ramp it down; the true limit stays the driver's ceiling.
///
/// A Voltage Limit set above min(M% x Vmax, Max Voltage) is written back into
/// Settings/Voltage Limit[0] as that ceiling (from sc_thread; the hotlink's
/// echo comes back unchanged and is accepted silently), so ODB, the page and
/// cd_hv's own demand clamp agree with the driver. The same happens when an
/// M<n> read shows the ceiling dropped below ODB's value, but never below the
/// D of a unit that is on (cd_hv's clamp would then ramp it down).
///
/// While on, a Demand change is refused when the S word shows TRP/ERR/INH
/// (a G would release the shut-off: switch ChState 0 then 1), and when ODB's
/// ChState shows 0 for a unit the driver found on. A ChState 0 that arrives
/// while the link is down (after it had been up) is latched and executed when
/// the unit answers again.
///
/// @note CMD_GET_THRESHOLD_ZERO returns -1 V so that a trip to 0 V shows in
///       Variables/Measured at once (hv.cxx:215-218). cd_hv asks for it only
///       when Settings/Zero Threshold is missing: on an existing ODB set
///       /Equipment/IsegHV/Settings/Zero Threshold[0] to -1 by hand.
/// @note The port lock is flock(LOCK_EX|LOCK_NB), the same as the CLI's. It
///       only excludes openers of the same device inode: run the CLI on the
///       machine and in the container where the frontend runs (a host and a
///       container each with their own /dev node do not see each other's lock).
/// @note An echo error in the middle of a write leaves the unit with a prefix
///       of the line (e.g. "L2=45" of "L2=450"), which the sync then executes.
///       Every write is read back and a mismatch is reported (D is undone).
///
/// @warning Call this only the way MIDAS does. The serial port, the driver's
///          value cache and its message rate limiter carry no lock: they
///          belong to the main thread until CMD_START and to MIDAS's sc_thread
///          afterwards. In particular the direct gets (>= CMD_GET_DIRECT),
///          CMD_GET_LABEL/CMD_SET_LABEL and the CMD_GET_THRESHOLD* commands are
///          only valid before CMD_START, where hv_init() issues them; calling
///          any of them at run time would race sc_thread on the port and
///          corrupt the rate limiter's std::map. iseg_nhq_polarity() below is
///          the only entry point that is safe at run time.
INT iseg_nhq_fe(INT cmd, ...);

/// @brief Polarity of the channel as last read from the unit's T<n> byte.
///
/// The NHQ polarity is a switch on the side cover. Every value this driver
/// exchanges with MIDAS is a magnitude, so the sign is published separately
/// (Variables/Polarity, by hv_alarm).
///
/// @param info the driver's private info pointer (DEVICE_DRIVER::dd_info)
/// @param ch   MIDAS channel index (only 0 exists)
/// @return +1 positive, -1 negative, 0 unknown (never read, or bad channel/info)
/// @note Reads only a std::atomic cache, refreshed on every T<n> read; safe to
///       call from the main thread while the poll thread is running.
int iseg_nhq_polarity(void *info, int ch);

#endif // ISEG_NHQ_FE_H
