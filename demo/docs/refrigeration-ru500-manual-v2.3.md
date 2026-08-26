# RU-500 Transport Refrigeration Unit Manual — Version 2.3

## Overview

The RU-500 is a diesel-electric transport refrigeration unit for semi-trailers.
It maintains setpoint temperature between -25 C and +12 C with electric
standby. This revision covers controller software 2.3 with the low-GWP
refrigerant retrofit and door-switch supervision.

## Electrical Specifications

| Component | Rating |
| --- | --- |
| Compressor motor | 240 V 3-phase, 11.5 A |
| Condenser fan motor | 24 V DC, 3 A |
| Evaporator fan motor | 24 V DC, 3 A |
| Defrost heater | 240 V, 8 A |

## Refrigerant Specifications

| Item | Value |
| --- | --- |
| Refrigerant type | R-450A |
| Charge quantity | 1.6 kg |
| Compressor oil | POE, 400 ml |

## Pressure Settings

| Setting | Value |
| --- | --- |
| Suction operating pressure | 1.2 bar |
| Suction cut-out pressure | 0.4 bar |
| Discharge operating pressure | 18 bar |
| Discharge cut-out pressure | 21 bar |

## Alarm Codes

| Code | Meaning |
| --- | --- |
| A-101 | High discharge temperature |
| A-102 | Low suction pressure |
| A-103 | Defrost timeout — defrost cycle exceeded the time limit |
| A-104 | Door switch fault — door state signal inconsistent with the unit program |

## Service Intervals

Replace the compressor oil every 2500 running hours. Replace the filter drier
every 12 months. Inspect the condenser coil monthly during peak season. Calibrate
the suction transducer yearly.

## Defrost Procedure

1. Stop the evaporator fan and energize the defrost heater.
2. Hold defrost until the coil temperature reaches +15 C.
3. If defrost exceeds 45 minutes, alarm A-103 is raised — inspect the drain
   line for ice blockage before restarting.
4. Restart the fans and confirm suction pressure returns to 1.2 bar.

## Compressor Service Procedure

1. Recover the refrigerant charge with certified recovery equipment.
2. Replace the compressor oil, then the filter drier.
3. Evacuate the circuit to 300 microns and hold for 30 minutes.
4. Recharge with the listed quantity, then verify discharge pressure stays
   under 18 bar at high ambient.
