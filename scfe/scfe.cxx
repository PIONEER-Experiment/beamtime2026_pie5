/********************************************************************\

  Name:         scfe.cxx
  Created by:   Patrick Schwendimann

  Credits:      Heavily based on an example by Stefan Ritt

  Contents:     Slow control frontend for the 2026 PSM beamtime


  $Id$

\********************************************************************/

#include <stdio.h>
#include <midas.h>
#include <mfe.h>
#include "arcus_stage_fe.h"
#include "isel_fe.h"
#include "caen_hv_fe.h"
#include "iseg_nhq_fe.h"
#include "hv_alarm.h"

#include "pi_generic.h"
#include "hv.h"

/*-- Globals -------------------------------------------------------*/

/* The frontend name (client name) as seen by other MIDAS clients   */
const char *frontend_name = "SlowControl";
/* The frontend file name, don't change it */
const char *frontend_file_name = __FILE__;

/* frontend_loop is called periodically if this variable is TRUE    */
BOOL frontend_call_loop = TRUE;

/* a frontend status page is displayed with this frequency in ms    */
INT display_period = 1000;

/* maximum event size produced by this frontend */
INT max_event_size = 10000;

/* maximum event size for fragmented events (EQ_FRAGMENTED) */
INT max_event_size_frag = 5 * 1024 * 1024;

/* buffer size to hold events */
INT event_buffer_size = 10 * 10000;

/*-- Equipment list ------------------------------------------------*/

/* device driver list */
DEVICE_DRIVER arcus_stage_driver[] = {
   {"Arcus Stage", arcus_stage_fe, 1, NULL, DF_INPUT | DF_OUTPUT | DF_PRIO_DEVICE | DF_MULTITHREAD},
   {""}
};

DEVICE_DRIVER isel_driver[] = {
   {"ISEL XYTable", isel_fe, 2, NULL, DF_INPUT | DF_OUTPUT | DF_PRIO_DEVICE | DF_MULTITHREAD},
   {""}
};

DEVICE_DRIVER caen_hv_driver[] = {
   {"CAEN HV", caen_hv_fe, 4, NULL,
    DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP | DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND},
   {""}
};

/* iseg NHQ 208L for the S5 PMT: ONE MIDAS channel (named "S5" by the driver's
   CMD_GET_LABEL on a fresh ODB), mapped to the unit's output given by
   Settings/Devices/iseg NHQ/Hardware Channel (default 2). Same flags as
   CaenHV; DF_PRIO_DEVICE makes hv_init() read the unit and write nothing, so
   a frontend restart never moves the voltage. */
DEVICE_DRIVER iseg_hv_driver[] = {
   {"iseg NHQ", iseg_nhq_fe, 1, NULL,
    DF_MULTITHREAD | DF_PRIO_DEVICE | DF_HW_RAMP | DF_REPORT_STATUS | DF_REPORT_CHSTATE | DF_POLL_DEMAND},
   {""}
};

/* What scfe/hv_alarm.cxx needs to know about the CAEN driver. The bit numbers
   and names come from caen_hv_fe.h, their single source of truth, never from
   literal numbers here. */
static const hv_alarm_driver_t caen_hv_alarm = {
   .device_label        = "CAEN HV",
   .off_hint            = " - check front switch KILL/OFF/ON",
   .stale_bit           = caen_hv::kStatStale,
   /* OVC | OVV | TRIP | OVP | OVT | ILK = 0x1398 today */
   .default_status_mask = (1u << caen_hv::kStatOvC)  | (1u << caen_hv::kStatOvV) |
                          (1u << caen_hv::kStatTrip) | (1u << caen_hv::kStatOvP) |
                          (1u << caen_hv::kStatOvT)  | (1u << caen_hv::kStatIlk),
   /* not the device MAXV: hv_alarm_init() runs before cd_hv has read it.
      8000 V / 3000 uA are the DT1470ET full scale, i.e. "no software limit"
      until the operator sets one per detector */
   .default_voltage_max = 8000.f,
   .default_current_max = 3000.f,
   .is_on               = [](DWORD stat) { return (stat & (1u << caen_hv::kStatOn)) != 0; },
   .stat_text           = caen_hv::stat_text,
   .polarity            = caen_hv_polarity,
   .deviation_check     = false,   /* no Deviation Max / Hold keys for CaenHV */
   .default_deviation_max = 0.f,
   .is_ramping          = nullptr,
   .default_deviation_hold_s = 0,
   .on_while_off_check  = false,   /* CAEN has a real ON bit and a real ChState */
};

/* The same for the iseg NHQ. Bits, names and the fault mask come from
   iseg_nhq_fe.h. Thresholds are the S5 starting values: 1300 V (the driver's
   own Max Voltage ceiling), 350 uA (294 uA measured at 1229 V, unit trip at
   450 uA), 20 V deviation held for 30 s (the NHQ says ON while its read-back
   still trails the set point by up to ~25 V after a ramp). */
static const hv_alarm_driver_t iseg_hv_alarm = {
   .device_label        = "iseg NHQ",
   .off_hint            = " - check HV-ON/KILL switches, CONTROL on DAC",
   .stale_bit           = iseg_nhq::kStatStale,
   .default_status_mask = iseg_nhq::kStatFaultMask,
   .default_voltage_max = 1300.f,
   .default_current_max = 350.f,
   .is_on               = iseg_nhq::stat_is_on,
   .stat_text           = iseg_nhq::stat_text,
   .polarity            = iseg_nhq_polarity,
   .deviation_check     = true,
   .default_deviation_max = 20.f,
   .is_ramping          = iseg_nhq::stat_is_ramping,
   .default_deviation_hold_s = 30,
   /* ChState is emulated on the set point (D = 0 is "off"), so voltage from
      a front-panel or CLI change while MIDAS says OFF needs its own alarm */
   .on_while_off_check  = true,
};

/* hv_alarm.cxx treats a ChStatus word whose bits 23-30 are all set (a float
   NaN pattern) as "never read". That is only safe while no driver defines a
   status bit in 27..30: bits 23..26 alone can never make the pattern. */
static_assert(caen_hv::kStatNumBits <= 27, "CAEN status bits must stay below bit 27");
static_assert(iseg_nhq::kStatNumBits <= 27, "iseg status bits must stay below bit 27");
static_assert(caen_hv::kStatStale == (1u << 31) && iseg_nhq::kStatStale == (1u << 31),
              "the stale flag is bit 31");

BOOL equipment_common_overwrite = TRUE;

EQUIPMENT equipment[] = {
    {"XYTable",                       /* equipment name */
    {7, 0,                             /* event ID, trigger mask */
     "SYSTEM",                         /* event buffer */
     EQ_SLOW,                          /* equipment type */
     0,                                /* event source */
     "MIDAS",                          /* format */
     TRUE,                             /* enabled */
     RO_RUNNING | RO_TRANSITIONS,      /* read when running and on transitions */
     60000,                            /* read every 60 sec */
     0,                                /* stop run after this event limit */
     0,                                /* number of sub events */
     10,                               /* log history at most every ten seconds */
     "", "", ""} ,
    cd_pi_gen_read,                       /* readout routine */
    cd_pi_gen,                            /* class driver main routine */
    isel_driver,                       /* device driver list */
    NULL,                              /* init string */
    },

    {"Degrader",                       /* equipment name */
    {6, 0,                             /* event ID, trigger mask */
     "SYSTEM",                         /* event buffer */
     EQ_SLOW,                          /* equipment type */
     0,                                /* event source */
     "MIDAS",                          /* format */
     TRUE,                             /* enabled */
     RO_RUNNING | RO_TRANSITIONS,      /* read when running and on transitions */
     60000,                            /* read every 60 sec */
     0,                                /* stop run after this event limit */
     0,                                /* number of sub events */
     10,                               /* log history at most every ten seconds */
     "", "", ""} ,
    cd_pi_gen_read,                       /* readout routine */
    cd_pi_gen,                            /* class driver main routine */
    arcus_stage_driver,                /* device driver list */
    NULL,                              /* init string */
    },

    {"CaenHV",                         /* equipment name */
    {8, 0,                             /* event ID, trigger mask */
     "SYSTEM",                         /* event buffer */
     EQ_SLOW,                          /* equipment type */
     0,                                /* event source */
     "MIDAS",                          /* format */
     TRUE,                             /* enabled */
     RO_RUNNING | RO_TRANSITIONS,      /* read when running and on transitions */
     60000,                            /* read every 60 sec */
     1000,                             /* NOT an event limit here: the slow control
                                          poll thread reuses this field as the device
                                          poll period in ms (device_driver.cxx:48-55) */
     0,                                /* number of sub events */
     10,                               /* minimum CMD_IDLE interval in ms */
     "", "", ""} ,
    cd_hv_read,                           /* readout routine */
    cd_hv,                                /* class driver main routine */
    caen_hv_driver,                    /* device driver list */
    NULL,                              /* init string */
    },

    {"IsegHV",                         /* equipment name */
    {9, 0,                             /* event ID, trigger mask */
     "SYSTEM",                         /* event buffer */
     EQ_SLOW,                          /* equipment type */
     0,                                /* event source */
     "MIDAS",                          /* format */
     TRUE,                             /* enabled */
     RO_RUNNING | RO_TRANSITIONS,      /* read when running and on transitions */
     60000,                            /* read every 60 sec */
     1000,                             /* NOT an event limit here: the slow control
                                          poll thread reuses this field as the device
                                          poll period in ms (device_driver.cxx:48-55) */
     0,                                /* number of sub events */
     10,                               /* minimum CMD_IDLE interval in ms */
     "", "", ""} ,
    cd_hv_read,                           /* readout routine */
    cd_hv,                                /* class driver main routine */
    iseg_hv_driver,                    /* device driver list */
    NULL,                              /* init string */
    },

   {""}
};


/*-- Dummy routines ------------------------------------------------*/

INT poll_event(INT source, INT count, BOOL test)
{
   return 1;
};
INT interrupt_configure(INT cmd, INT source, POINTER_T adr)
{
   return 1;
};

/*-- Frontend Init -------------------------------------------------*/

INT frontend_init()
{
   /* per-channel HV alarms, ODB thresholds and Variables/Polarity; runs on the
      mfe main thread only. Never fatal: on failure it disables itself. */
   hv_alarm_init("CaenHV", 4, caen_hv_alarm);
   hv_alarm_init("IsegHV", 1, iseg_hv_alarm);

   return CM_SUCCESS;
}

/*-- Frontend Exit -------------------------------------------------*/

INT frontend_exit()
{
   hv_alarm_exit();

   return CM_SUCCESS;
}

/*-- Frontend Loop -------------------------------------------------*/

INT frontend_loop()
{
   ss_sleep(100);

   /* HV alarm check, self-throttled to once per second */
   hv_alarm_loop();

   return CM_SUCCESS;
}

/*-- Begin of Run --------------------------------------------------*/

INT begin_of_run(INT run_number, char *error)
{
   return CM_SUCCESS;
}

/*-- End of Run ----------------------------------------------------*/

INT end_of_run(INT run_number, char *error)
{
   return CM_SUCCESS;
}

/*-- Pause Run -----------------------------------------------------*/

INT pause_run(INT run_number, char *error)
{
   return CM_SUCCESS;
}

/*-- Resume Run ----------------------------------------------------*/

INT resume_run(INT run_number, char *error)
{
   return CM_SUCCESS;
}

/*------------------------------------------------------------------*/
