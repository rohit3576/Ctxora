# DS-200 Door Sensor Manual — Version 1 (firmware 1.x)

## Overview

The DS-200 is a magnetic door sensor for coldchain trailers and containers. It
reports door state (open or closed), open-duration telemetry, and battery health
over the trailer telematics bus. This revision covers firmware 1.x units.

## Specifications

| Item | Value |
| --- | --- |
| Sensor unit part number | TS-001 |
| Mounting bracket part number | TS-002 |
| Supply voltage | 12-24V DC |
| Standby current | 15 mA |
| Ingress protection | IP67 |
| Operating temperature | -30 C to +60 C |

## Error Codes

| Code | Meaning |
| --- | --- |
| E-301 | Door open too long — door held open beyond the configured threshold |
| E-302 | Sensor signal loss — no reed-switch transition within the expected window |
| E-303 | Battery low — replace the sensor battery pack |

## Maintenance Procedure

1. Inspect the sensor housing and mounting bracket for cracks or corrosion.
2. Clean the magnet face and sensor face with a dry cloth.
3. Lubricate the door hinges with lithium grease every 90 days.
4. Torque the mounting bolts to 45 Nm in a crossed pattern.
5. Test full door closure and confirm the state change is reported.
6. Record the service date in the fleet maintenance log.

## Troubleshooting

For E-302 (sensor signal loss), check the wiring harness at connector C4 for
corrosion, verify the magnet alignment gap is under 10 mm, and confirm the
telematics bus reports a heartbeat for the sensor. For E-301, review the door
seal and adjust the open-duration threshold if loading patterns require it.
