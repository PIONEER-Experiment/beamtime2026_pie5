/**
 * @file pi_generic.cxx
 * @author Patrick Schwendimann (schwenpa@phys.ethz.ch)
 * @brief an adaption of the default midas generic class driver that also reads the channel status.
 *
 */

#include <algorithm>
#include <assert.h>
#include <cmath>
#include <cstdint>
#include <stdio.h>
#include <stdlib.h>
#include <string>
#include <unordered_map>


#include <midas.h>
#include <mfe.h>

#include "pi_generic.h"

typedef struct {

   /* ODB keys */
   HNDLE hKeyRoot, hKeyDemand, hKeyMeasured, hKeyStatus;

   /* globals */
   INT num_channels;
   INT format;
   INT last_channel;


   /* items in /Variables record */
   float *demand;
   float *measured;
   float *status;

   /* items in /Settings */
   char *names;
   float *update_threshold;

   /* mirror arrays */
   float *status_mirror;
   float *demand_mirror;
   float *measured_mirror;

   DEVICE_DRIVER **driver;
   INT *channel_offset;

} PI_GEN_INFO;

#ifndef abs
#define abs(a) (((a) < 0)   ? -(a) : (a))
#endif

#define conv_entry(x) case static_cast<uint32_t>(pi_gen_status_t::x) : return pi_gen_status_t::x;
pi_gen_status_t status_from_float(float f) {
   if (!std::isfinite(f) || f < 0.0f || f != std::floor(f)) {
      return pi_gen_status_t::kMALFORM;
   }
   uint32_t i = static_cast<uint32_t>(f);

    switch (i) {
      conv_entry(kOK);
      conv_entry(kTRANSITION);
      conv_entry(kTIMEOUT);
      conv_entry(kDISCONNECT);
      conv_entry(kERROR);
      default: return pi_gen_status_t::kMALFORM;
    }
}

namespace {
   struct status_display_config_t {
      std::string name;
      std::string color;
   };

   std::unordered_map<pi_gen_status_t, status_display_config_t> status_names {
      {pi_gen_status_t::kOK,         {"Ok",         "greenLight"  }},
      {pi_gen_status_t::kTRANSITION, {"Transition", "yellowLight" }},
      {pi_gen_status_t::kTIMEOUT,    {"Timeout",    "orange"      }},
      {pi_gen_status_t::kDISCONNECT, {"DISCONNECT", "redLight"    }},
      {pi_gen_status_t::kERROR,      {"ERROR",      "red"         }},
      {pi_gen_status_t::kMALFORM,    {"MALFORM",    "red"         }}
   };
}

/*------------------------------------------------------------------*/

static void free_mem(PI_GEN_INFO * pi_gen_info)
{
   free(pi_gen_info->status);
   free(pi_gen_info->names);
   free(pi_gen_info->demand);
   free(pi_gen_info->measured);

   free(pi_gen_info->update_threshold);

   free(pi_gen_info->demand_mirror);
   free(pi_gen_info->measured_mirror);
   free(pi_gen_info->status_mirror);

   free(pi_gen_info->channel_offset);
   free(pi_gen_info->driver);

   free(pi_gen_info);
}

/*------------------------------------------------------------------*/

INT pi_gen_read(EQUIPMENT * pequipment, int channel)
{
   int i, status;
   PI_GEN_INFO *pi_gen_info;
   HNDLE hDB;
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;
   cm_get_experiment_database(&hDB, NULL);
   /* if driver is multi-threaded, read all channels at once */
   for (i=0 ; i < pi_gen_info->num_channels ; i++) {

      if (pi_gen_info->driver[i]->flags & DF_MULTITHREAD) {
         status = device_driver(pi_gen_info->driver[i], CMD_GET,
                                i - pi_gen_info->channel_offset[i],
                                &pi_gen_info->measured[i]);
      }
   }

   /* else read only single channel */
   if (!(pi_gen_info->driver[channel]->flags & DF_MULTITHREAD)) {
      status = device_driver(pi_gen_info->driver[channel], CMD_GET,
                             channel - pi_gen_info->channel_offset[channel],
                             &pi_gen_info->measured[channel]);
   }
   /* check for update measured */
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      /* update if change is more than update_threshold */
      if ((ss_isnan(pi_gen_info->measured[i]) && !ss_isnan(pi_gen_info->measured_mirror[i])) ||
          (!ss_isnan(pi_gen_info->measured[i]) && ss_isnan(pi_gen_info->measured_mirror[i])) ||
          (!ss_isnan(pi_gen_info->measured[i]) && !ss_isnan(pi_gen_info->measured_mirror[i]) &&
           abs(pi_gen_info->measured[i] - pi_gen_info->measured_mirror[i]) >
           pi_gen_info->update_threshold[i])) {
         for (i = 0; i < pi_gen_info->num_channels; i++)
            pi_gen_info->measured_mirror[i] = pi_gen_info->measured[i];

         db_set_data(hDB, pi_gen_info->hKeyMeasured, pi_gen_info->measured,
                     sizeof(float) * pi_gen_info->num_channels, pi_gen_info->num_channels,
                     TID_FLOAT);

         pequipment->odb_out++;

         break;
      }
   }

   /*---- read demand value ----*/

   status = device_driver(pi_gen_info->driver[channel], CMD_GET_DEMAND,
                          channel - pi_gen_info->channel_offset[channel],
                          &pi_gen_info->demand[channel]);

   if ((pi_gen_info->demand[channel] != pi_gen_info->demand_mirror[channel] && !ss_isnan(pi_gen_info->demand[channel])) ||
       (ss_isnan(pi_gen_info->demand[channel]) && !ss_isnan(pi_gen_info->demand_mirror[channel])) ||
       (!ss_isnan(pi_gen_info->demand[channel]) && ss_isnan(pi_gen_info->demand_mirror[channel]))) {
      pi_gen_info->demand_mirror[channel] = pi_gen_info->demand[channel];
      db_set_data(hDB, pi_gen_info->hKeyDemand, pi_gen_info->demand,
                  sizeof(float) * pi_gen_info->num_channels, pi_gen_info->num_channels,
                  TID_FLOAT);
   }

   device_driver(pi_gen_info->driver[channel], CMD_GET_STATUS,
                          channel - pi_gen_info->channel_offset[channel],
                          &pi_gen_info->status[channel]);

   /* check for updated status */
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      /* update if change is more than update_threshold */
      if (( ss_isnan(pi_gen_info->status[i]) && !ss_isnan(pi_gen_info->status_mirror[i])) ||
          (!ss_isnan(pi_gen_info->status[i]) &&  ss_isnan(pi_gen_info->status_mirror[i])) ||
          (!ss_isnan(pi_gen_info->status[i]) && !ss_isnan(pi_gen_info->status_mirror[i]) &&
           (pi_gen_info->status[i] != pi_gen_info->status_mirror[i]))) {

         for (int j = 0; j < pi_gen_info->num_channels; j++) {
            pi_gen_info->status_mirror[j] = pi_gen_info->status[j];
         }

         db_set_data(hDB, pi_gen_info->hKeyStatus, pi_gen_info->status,
                     sizeof(float) * pi_gen_info->num_channels, pi_gen_info->num_channels,
                     TID_FLOAT);

         int max_status = *std::max_element(pi_gen_info->status, pi_gen_info->status + pi_gen_info->num_channels);

         auto it1 = status_names.find(status_from_float(max_status));
         if (it1 == status_names.end()) {
            set_equipment_status(pequipment->name, "StatusError", "red");
         } else {
            set_equipment_status(pequipment->name, it1->second.name.c_str(), it1->second.color.c_str());
         }
         break;
      }
   }

   return status;
}

/*------------------------------------------------------------------*/

INT pi_gen_read_direct(EQUIPMENT * pequipment) {
   int i, status = 0;
   PI_GEN_INFO *pi_gen_info;
   HNDLE hDB;
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;
   cm_get_experiment_database(&hDB, NULL);

   for (i = 0; i < pi_gen_info->num_channels; i++)
      status = device_driver(pi_gen_info->driver[i], CMD_GET_DIRECT,
                             i - pi_gen_info->channel_offset[i],
                             &pi_gen_info->measured[i]);

   return status;
}

/*------------------------------------------------------------------*/

void pi_gen_demand(INT hDB, INT hKey, void *info)
{
   INT i;
   PI_GEN_INFO *pi_gen_info;
   EQUIPMENT *pequipment;

   pequipment = (EQUIPMENT *) info;
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;

   /* set individual channels only if demand value differs */
   for (i = 0; i < pi_gen_info->num_channels; i++)
      if (pi_gen_info->demand[i] != pi_gen_info->demand_mirror[i]) {
         if ((pi_gen_info->driver[i]->flags & DF_READ_ONLY) == 0) {
            device_driver(pi_gen_info->driver[i], CMD_SET,
                          i - pi_gen_info->channel_offset[i], pi_gen_info->demand[i]);
         }
         pi_gen_info->demand_mirror[i] = pi_gen_info->demand[i];
      }

   pequipment->odb_in++;
}

/*------------------------------------------------------------------*/

void pi_gen_update_label(INT hDB, INT hKey, void *info)
{
   INT i;
   PI_GEN_INFO *pi_gen_info;
   EQUIPMENT *pequipment;

   pequipment = (EQUIPMENT *) info;
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;

   /* update channel labels based on the midas channel names */
   for (i = 0; i < pi_gen_info->num_channels; i++)
      device_driver(pi_gen_info->driver[i], CMD_SET_LABEL,
                    i - pi_gen_info->channel_offset[i],
                    pi_gen_info->names + NAME_LENGTH * i);
}

/*------------------------------------------------------------------*/

INT pi_gen_init(EQUIPMENT * pequipment)
{
   int status, size, i, j, index, offset;
   char str[256];
   HNDLE hDB, hKey, hNames, hThreshold;
   PI_GEN_INFO *pi_gen_info;

   /* allocate private data */
   pequipment->cd_info = calloc(1, sizeof(PI_GEN_INFO));
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;

   /* get class driver root key */
   cm_get_experiment_database(&hDB, NULL);
   snprintf(str, sizeof(str), "/Equipment/%s", pequipment->name);
   db_create_key(hDB, 0, str, TID_KEY);
   db_find_key(hDB, 0, str, &pi_gen_info->hKeyRoot);

   /* save event format */
   size = sizeof(str);
   db_get_value(hDB, pi_gen_info->hKeyRoot, "Common/Format", str, &size, TID_STRING, TRUE);

   if (equal_ustring(str, "Fixed"))
      pi_gen_info->format = FORMAT_FIXED;
   else if (equal_ustring(str, "MIDAS"))
      pi_gen_info->format = FORMAT_MIDAS;
   else
      assert(!"unknown ODM Common/Format");

   /* count total number of channels */
   for (i = 0, pi_gen_info->num_channels = 0; pequipment->driver[i].name[0]; i++) {
      if (pequipment->driver[i].channels == 0) {
         cm_msg(MERROR, "pi_gen_init", "Driver with zero channels not allowed");
         return FE_ERR_ODB;
      }

      pi_gen_info->num_channels += pequipment->driver[i].channels;
   }

   if (pi_gen_info->num_channels == 0) {
      cm_msg(MERROR, "pi_gen_init", "No channels found in device driver list");
      return FE_ERR_ODB;
   }

   /* Allocate memory for buffers */
   pi_gen_info->names = (char *) calloc(pi_gen_info->num_channels, NAME_LENGTH);

   pi_gen_info->demand = (float *) calloc(pi_gen_info->num_channels, sizeof(float));
   pi_gen_info->measured = (float *) calloc(pi_gen_info->num_channels, sizeof(float));
   pi_gen_info->status = (float *) calloc(pi_gen_info->num_channels, sizeof(float));

   pi_gen_info->update_threshold = (float *) calloc(pi_gen_info->num_channels, sizeof(float));

   pi_gen_info->demand_mirror = (float *) calloc(pi_gen_info->num_channels, sizeof(float));
   pi_gen_info->measured_mirror = (float *) calloc(pi_gen_info->num_channels, sizeof(float));
   pi_gen_info->status_mirror = (float *) calloc(pi_gen_info->num_channels, sizeof(float));

   pi_gen_info->channel_offset = (INT *) calloc(pi_gen_info->num_channels, sizeof(INT));
   pi_gen_info->driver = (DEVICE_DRIVER **) calloc(pi_gen_info->num_channels, sizeof(void *));

   if (!pi_gen_info->driver) {
      cm_msg(MERROR, "hv_init", "Not enough memory");
      return FE_ERR_ODB;
   }

   /*---- Initialize device drivers ----*/

   /* call init method */
   for (i = 0; pequipment->driver[i].name[0]; i++) {
      snprintf(str, sizeof(str), "Settings/Devices/%s", pequipment->driver[i].name);
      status = db_find_key(hDB, pi_gen_info->hKeyRoot, str, &hKey);
      if (status != DB_SUCCESS) {
         db_create_key(hDB, pi_gen_info->hKeyRoot, str, TID_KEY);
         status = db_find_key(hDB, pi_gen_info->hKeyRoot, str, &hKey);
         if (status != DB_SUCCESS) {
            cm_msg(MERROR, "hv_init", "Cannot create %s entry in online database", str);
            free_mem(pi_gen_info);
            return FE_ERR_ODB;
         }
      }

      /* check enabled flag */
      size = sizeof(pequipment->driver[i].enabled);
      pequipment->driver[i].enabled = 1;
      snprintf(str, sizeof(str), "Settings/Devices/%s/Enabled", pequipment->driver[i].name);
      status = db_get_value(hDB, pi_gen_info->hKeyRoot, str, &pequipment->driver[i].enabled
                            , &size, TID_BOOL, TRUE);
      if (status != DB_SUCCESS)
         return FE_ERR_ODB;

      if (pequipment->driver[i].enabled) {
         printf("Connecting %s:%s...", pequipment->name, pequipment->driver[i].name);
         fflush(stdout);
         status = device_driver(&pequipment->driver[i], CMD_INIT, hKey);
         if (status != FE_SUCCESS) {
            free_mem(pi_gen_info);
            return status;
         }
         printf("OK\n");
      }
   }

   /* compose device driver channel assignment */
   for (i = 0, j = 0, index = 0, offset = 0; i < pi_gen_info->num_channels; i++, j++) {
      while (j >= pequipment->driver[index].channels && pequipment->driver[index].name[0]) {
         offset += j;
         index++;
         j = 0;
      }

      pi_gen_info->driver[i] = &pequipment->driver[index];
      pi_gen_info->channel_offset[i] = offset;
   }

   /*---- create demand variables ----*/

   /* get demand from ODB */
   status =
       db_find_key(hDB, pi_gen_info->hKeyRoot, "Variables/Demand", &pi_gen_info->hKeyDemand);
   if (status == DB_SUCCESS) {
      size = sizeof(float) * pi_gen_info->num_channels;
      db_get_data(hDB, pi_gen_info->hKeyDemand, pi_gen_info->demand, &size, TID_FLOAT);
   }
   /* let device driver overwrite demand values, if it supports it */
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      if ((pi_gen_info->driver[i]->flags & DF_PRIO_DEVICE) &&
          !(pi_gen_info->driver[i]->flags & DF_QUICKSTART)) {
         device_driver(pi_gen_info->driver[i], CMD_GET_DEMAND_DIRECT,
                       i - pi_gen_info->channel_offset[i], &pi_gen_info->demand[i]);
         pi_gen_info->demand_mirror[i] = pi_gen_info->demand[i];

         if (pi_gen_info->driver[i]->flags &DF_MULTITHREAD)
            pi_gen_info->driver[i]->mt_buffer->channel[i].variable[CMD_GET_DEMAND] = pi_gen_info->demand[i];

      } else
         pi_gen_info->demand_mirror[i] = ss_nan();
   }
   /* write back demand values */
   status =
       db_find_key(hDB, pi_gen_info->hKeyRoot, "Variables/Demand", &pi_gen_info->hKeyDemand);
   if (status != DB_SUCCESS) {
      db_create_key(hDB, pi_gen_info->hKeyRoot, "Variables/Demand", TID_FLOAT);
      db_find_key(hDB, pi_gen_info->hKeyRoot, "Variables/Demand", &pi_gen_info->hKeyDemand);
   }
   size = sizeof(float) * pi_gen_info->num_channels;
   db_set_data(hDB, pi_gen_info->hKeyDemand, pi_gen_info->demand, size,
               pi_gen_info->num_channels, TID_FLOAT);
   db_open_record(hDB, pi_gen_info->hKeyDemand, pi_gen_info->demand,
                  pi_gen_info->num_channels * sizeof(float), MODE_READ, pi_gen_demand,
                  pequipment);

   /*---- create measured variables ----*/
   db_merge_data(hDB, pi_gen_info->hKeyRoot, "Variables/Measured",
                 pi_gen_info->measured, sizeof(float) * pi_gen_info->num_channels,
                 pi_gen_info->num_channels, TID_FLOAT);
   db_find_key(hDB, pi_gen_info->hKeyRoot, "Variables/Measured", &pi_gen_info->hKeyMeasured);
   for (i=0 ; i<pi_gen_info->num_channels ; i++)
      pi_gen_info->measured[i] = (float)ss_nan();

   /*---- create status variables ----*/
   db_merge_data(hDB, pi_gen_info->hKeyRoot, "Variables/Status",
                 pi_gen_info->status, sizeof(float) * pi_gen_info->num_channels,
                 pi_gen_info->num_channels, TID_FLOAT);
   db_find_key(hDB, pi_gen_info->hKeyRoot, "Variables/Status", &pi_gen_info->hKeyStatus);
   for (i=0 ; i<pi_gen_info->num_channels ; i++)
      pi_gen_info->status[i] = (float)ss_nan();

   /*---- get default names from device driver ----*/
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      snprintf(pi_gen_info->names + NAME_LENGTH * i, NAME_LENGTH, "Default%%CH %d", i);
      device_driver(pi_gen_info->driver[i], CMD_GET_LABEL,
                    i - pi_gen_info->channel_offset[i], pi_gen_info->names + NAME_LENGTH * i);
   }
   db_merge_data(hDB, pi_gen_info->hKeyRoot, "Settings/Names",
                 pi_gen_info->names, NAME_LENGTH * pi_gen_info->num_channels,
                 pi_gen_info->num_channels, TID_STRING);

   /*---- set labels form midas SC names ----*/
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;
      device_driver(pi_gen_info->driver[i], CMD_SET_LABEL,
                    i - pi_gen_info->channel_offset[i], pi_gen_info->names + NAME_LENGTH * i);
   }

   /* open hotlink on channel names */
   if (db_find_key(hDB, pi_gen_info->hKeyRoot, "Settings/Names", &hNames) == DB_SUCCESS)
      db_open_record(hDB, hNames, pi_gen_info->names, NAME_LENGTH*pi_gen_info->num_channels,
                     MODE_READ, pi_gen_update_label, pequipment);

   /*---- get default update threshold from device driver ----*/
   for (i = 0; i < pi_gen_info->num_channels; i++) {
      pi_gen_info->update_threshold[i] = 1.f;      /* default 1 unit */
      device_driver(pi_gen_info->driver[i], CMD_GET_THRESHOLD,
                    i - pi_gen_info->channel_offset[i], &pi_gen_info->update_threshold[i]);
   }
   db_merge_data(hDB, pi_gen_info->hKeyRoot, "Settings/Update Threshold Measured",
                 pi_gen_info->update_threshold, sizeof(float)*pi_gen_info->num_channels,
                 pi_gen_info->num_channels, TID_FLOAT);

   /* open hotlink on update threshold */
   if (db_find_key(hDB, pi_gen_info->hKeyRoot, "Settings/Update Threshold Measured", &hThreshold) == DB_SUCCESS)
      db_open_record(hDB, hThreshold, pi_gen_info->update_threshold, sizeof(float)*pi_gen_info->num_channels,
                     MODE_READ, NULL, NULL);

   /*---- set initial demand values ----*/
   pi_gen_demand(hDB, pi_gen_info->hKeyDemand, pequipment);

   /* initially read all channels */
   if (!(pi_gen_info->driver[0]->flags & DF_QUICKSTART)) {
      pi_gen_read_direct(pequipment);

      if (pi_gen_info->driver[0]->flags & DF_MULTITHREAD) {
         for (i = 0; i < pi_gen_info->num_channels; i++)
            pi_gen_info->driver[i]->mt_buffer->channel[i].variable[CMD_GET] = pi_gen_info->measured[i];
      }
   }

   return FE_SUCCESS;
}

/*----------------------------------------------------------------------------*/

INT pi_gen_start(EQUIPMENT * pequipment)
{
   INT i;

   /* call start method of device drivers */
   for (i = 0; pequipment->driver[i].dd != NULL ; i++)
      if (pequipment->driver[i].flags & DF_MULTITHREAD) {
         pequipment->driver[i].pequipment = &pequipment->info;
         device_driver(&pequipment->driver[i], CMD_START);
      }

  return FE_SUCCESS;
}

/*----------------------------------------------------------------------------*/

INT pi_gen_stop(EQUIPMENT * pequipment)
{
   INT i;

   /* call stop method of device drivers */
   for (i = 0; pequipment->driver[i].dd != NULL && pequipment->driver[i].flags & DF_MULTITHREAD ; i++)
      device_driver(&pequipment->driver[i], CMD_STOP);

   return FE_SUCCESS;
}

/*------------------------------------------------------------------*/

INT pi_gen_exit(EQUIPMENT * pequipment)
{
   INT i;

   free_mem((PI_GEN_INFO *) pequipment->cd_info);

   /* call exit method of device drivers */
   for (i = 0; pequipment->driver[i].dd != NULL; i++)
      device_driver(&pequipment->driver[i], CMD_EXIT);

   return FE_SUCCESS;
}

/*------------------------------------------------------------------*/

INT pi_gen_idle(EQUIPMENT * pequipment)
{
   INT act, status;
   PI_GEN_INFO *pi_gen_info;

   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;

   /* select next measurement channel */
   act = (pi_gen_info->last_channel + 1) % pi_gen_info->num_channels;

   /* measure channel */
   status = pi_gen_read(pequipment, act);
   pi_gen_info->last_channel = act;

   return status;
}

/*------------------------------------------------------------------*/

INT cd_pi_gen_read(char *pevent, int offset)
{
   float *pdata;
   PI_GEN_INFO *pi_gen_info;
   EQUIPMENT *pequipment;
#ifdef HAVE_YBOS
   DWORD *pdw;
#endif

   pequipment = *((EQUIPMENT **) pevent);
   pi_gen_info = (PI_GEN_INFO *) pequipment->cd_info;

   if (pi_gen_info->format == FORMAT_FIXED) {
      memcpy(pevent, pi_gen_info->demand, sizeof(float) * pi_gen_info->num_channels);
      pevent += sizeof(float) * pi_gen_info->num_channels;

      memcpy(pevent, pi_gen_info->measured, sizeof(float) * pi_gen_info->num_channels);
      pevent += sizeof(float) * pi_gen_info->num_channels;

      return 2 * sizeof(float) * pi_gen_info->num_channels;
   } else if (pi_gen_info->format == FORMAT_MIDAS) {
      bk_init32(pevent);

      /* create DMND bank */
      bk_create(pevent, "DMND", TID_FLOAT, (void **)&pdata);
      memcpy(pdata, pi_gen_info->demand, sizeof(float) * pi_gen_info->num_channels);
      pdata += pi_gen_info->num_channels;
      bk_close(pevent, pdata);

      /* create MSRD bank */
      bk_create(pevent, "MSRD", TID_FLOAT, (void **)&pdata);
      memcpy(pdata, pi_gen_info->measured, sizeof(float) * pi_gen_info->num_channels);
      pdata += pi_gen_info->num_channels;
      bk_close(pevent, pdata);

      return bk_size(pevent);
   } else {
      assert(!"unknown pi_gen_info->format");
   }

   return 0;
}

/*------------------------------------------------------------------*/

INT cd_pi_gen(INT cmd, EQUIPMENT * pequipment)
{
   INT status;

   switch (cmd) {
   case CMD_INIT:
      status = pi_gen_init(pequipment);
      break;

   case CMD_START:
     status = pi_gen_start(pequipment);
     break;

   case CMD_STOP:
      status = pi_gen_stop(pequipment);
      break;

   case CMD_EXIT:
      status = pi_gen_exit(pequipment);
      break;

   case CMD_IDLE:
      status = pi_gen_idle(pequipment);
      break;

   default:
      cm_msg(MERROR, "Generic class driver", "Received unknown command %d", cmd);
      status = FE_ERR_DRIVER;
      break;
   }

   return status;
}
