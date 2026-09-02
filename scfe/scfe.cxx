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

#include "pi_generic.h"

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
   return CM_SUCCESS;
}

/*-- Frontend Exit -------------------------------------------------*/

INT frontend_exit()
{
   return CM_SUCCESS;
}

/*-- Frontend Loop -------------------------------------------------*/

INT frontend_loop()
{
   ss_sleep(100);
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
