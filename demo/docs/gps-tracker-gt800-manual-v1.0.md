# GT-800 GPS Tracker Manual — Version 1.0 (firmware 1.0)

## Overview

The GT-800 is a hard-mounted GPS tracker for trucks, trailers, and containers. It combines
GNSS positioning, an LTE uplink, CAN and J1939 telemetry, and a black-box journal for
incident reconstruction. This revision covers firmware 1.0 units.

## Technical Specifications

| Item | Value |
| --- | --- |
| GNSS engine firmware | GFX-2.1 |
| Position engine | 33-channel concurrent receiver |
| Supply voltage | 9-36 V DC |
| Standby current | 45 mA |
| Internal backup battery | 3.7 V, 2.1 Ah |
| Cellular module | LTE Cat-1 |
| Ingress protection | IP69K |
| Operating temperature | -40 C to +85 C |
| Weight | 210 g |

## Error Codes

| Code | Meaning |
| --- | --- |
| E-501 | GNSS module fault — no fix acquired within the startup window |
| E-502 | LTE registration failure — modem cannot attach to any network |
| E-503 | Accelerometer self-test failure — reseat the sensor flex cable |
| E-504 | Backup battery depletion — replace the internal battery pack |

## Telemetry Channel Map

The GT-800 reports the telemetry channels below over the LTE uplink. Cadence is the upload
interval for a changing value; unchanged values age out per the retention profile. Channels
whose Notes cell points to a diagnostic note (DN-nn) have extended reporting behavior
described in the notes block under the table.

| Channel | Parameter | Unit | Cadence | Notes |
| --- | --- | --- | --- | --- |
| CH-01 | Main supply voltage | V | 1 s |  |
| CH-02 | Internal battery voltage | V | 10 s |  |
| CH-03 | GNSS fix quality | enum | 1 s |  |
| CH-04 | Latitude | deg | 1 s |  |
| CH-05 | Longitude | deg | 1 s |  |
| CH-06 | Ground speed | km/h | 1 s |  |
| CH-07 | Heading | deg | 1 s |  |
| CH-08 | Altitude | m | 10 s |  |
| CH-09 | Geofence breach events | event | 60 s |  |
| CH-10 | Harsh braking events | event | on change |  |
| CH-11 | Harsh acceleration events | event | on change |  |
| CH-12 | Harsh cornering events | event | on change |  |
| CH-13 | Towing detection | state | 30 s |  |
| CH-14 | Ignition sense | state | on change |  |
| CH-15 | Engine hours | h | 60 s |  |
| CH-16 | Odometer | km | 60 s |  |
| CH-17 | Trip distance | km | on trip end |  |
| CH-18 | Idle time | h | on trip end |  |
| CH-19 | Engine coolant temperature | C | 30 s |  |
| CH-20 | Engine RPM | rpm | 10 s |  |
| CH-21 | Fuel level | percent | 30 s |  |
| CH-22 | Instant fuel rate | l/h | 30 s |  |
| CH-23 | Total fuel used | l | on trip end |  |
| CH-24 | Battery current | A | 10 s |  |
| CH-25 | Supply health flags | flags | 60 s |  |
| CH-26 | GNSS satellite count | count | 10 s |  |
| CH-27 | GNSS hdop | ratio | 10 s |  |
| CH-28 | Position uncertainty | m | 10 s |  |
| CH-29 | Motion state | state | 5 s |  |
| CH-30 | Vibration rms | mg | 30 s |  |
| CH-31 | Driver identification | id | on change |  |
| CH-32 | Seatbelt state | state | on change |  |
| CH-33 | Engine state | state | on change |  |
| CH-34 | Cruise control state | state | on change |  |
| CH-35 | Brake pedal position | percent | 10 s |  |
| CH-36 | Accelerator pedal position | percent | 10 s |  |
| CH-37 | Engine load | percent | 10 s |  |
| CH-38 | Ambient air temperature | C | 60 s |  |
| CH-39 | Barometric pressure | hPa | 60 s |  |
| CH-40 | Engine oil pressure | kPa | 30 s |  |
| CH-41 | Engine oil temperature | C | 30 s |  |
| CH-42 | Intake manifold temperature | C | 30 s |  |
| CH-43 | Air filter restriction | kPa | 60 s |  |
| CH-44 | Turbo boost pressure | kPa | 10 s |  |
| CH-45 | DEF tank level | percent | 60 s |  |
| CH-46 | Diagnostic trouble code count | count | on change |  |
| CH-47 | Emission cycle state | state | 60 s |  |
| CH-48 | Auxiliary tank level | percent | 30 s | see diagnostic note DN-48 |
| CH-49 | CAN bus load | percent | 10 s |  |
| CH-50 | CAN error counters | count | 60 s |  |
| CH-51 | J1939 source addresses | list | on change |  |
| CH-52 | OBD-II protocol | enum | on change |  |
| CH-53 | Engine model | string | on change |  |
| CH-54 | Vehicle identification number | string | on change |  |
| CH-55 | Firmware build | string | on change |  |
| CH-56 | Modem RSSI | dBm | 60 s |  |
| CH-57 | Modem CSQ | index | 60 s |  |
| CH-58 | Network operator | string | on change |  |
| CH-59 | Roaming state | state | 60 s |  |
| CH-60 | SIM identity hash | hash | on change |  |
| CH-61 | APN state | state | on change |  |
| CH-62 | Uplink queue depth | count | 10 s |  |
| CH-63 | Failed uplinks | count | 60 s |  |
| CH-64 | SMS fallback state | state | on change |  |
| CH-65 | Jamming detection | state | 5 s |  |
| CH-66 | Harmonic interference | dB | 10 s |  |
| CH-67 | GNSS jamming flags | flags | 5 s |  |
| CH-68 | Antenna current | mA | 60 s |  |
| CH-69 | Antenna open circuit | state | on change |  |
| CH-70 | Digital input 1 | state | on change |  |
| CH-71 | Digital input 2 | state | on change |  |
| CH-72 | Digital input 3 | state | on change |  |
| CH-73 | Digital input 4 | state | on change |  |
| CH-74 | Digital input 5 | state | on change |  |
| CH-75 | Digital input 6 | state | on change |  |
| CH-76 | Digital input 7 | state | on change |  |
| CH-77 | Digital input 8 | state | on change |  |
| CH-78 | Temperature probe 1 | C | 30 s |  |
| CH-79 | Temperature probe 2 | C | 30 s |  |
| CH-80 | Temperature probe 3 | C | 30 s |  |
| CH-81 | Temperature probe 4 | C | 30 s |  |
| CH-82 | Temperature probe 5 | C | 60 s |  |
| CH-83 | Temperature probe 6 | C | 60 s |  |
| CH-84 | Temperature probe 7 | C | 60 s |  |
| CH-85 | Temperature probe 8 | C | 60 s |  |
| CH-86 | Humidity sensor | percent | 60 s |  |
| CH-87 | Auxiliary switch input | state | on change |  |
| CH-88 | Latching output 1 | state | on change |  |
| CH-89 | Latching output 2 | state | on change |  |
| CH-90 | Pulse counter 1 | pulses | on trip end |  |
| CH-91 | Pulse counter 2 | pulses | on trip end |  |
| CH-92 | Low-voltage disconnect | state | on change | see diagnostic note DN-92 |
| CH-93 | Geofence dwell time | min | 60 s |  |
| CH-94 | Geofence entry count | event | on change |  |
| CH-95 | Trip count | trips | on trip end |  |
| CH-96 | Harsh event total | count | on change |  |
| CH-97 | Engine restarts | count | on trip end |  |
| CH-98 | Power cycles | count | on change |  |
| CH-99 | Controller uptime | h | 60 s |  |
| CH-100 | Configuration checksum | hash | on change |  |
| CH-101 | GNSS antenna short | state | on change |  |
| CH-102 | Modem temperature | C | 60 s |  |
| CH-103 | Internal humidity | percent | 60 s |  |
| CH-104 | Backup battery health | percent | 60 s |  |
| CH-105 | Backup battery cycles | count | on change |  |
| CH-106 | Flash wear indicator | percent | 60 s |  |
| CH-107 | Runtime software errors | count | 60 s |  |
| CH-108 | Watchdog resets | count | on change |  |
| CH-109 | Storage free | kB | 60 s |  |
| CH-110 | Black-box record count | count | 60 s |  |

Channel diagnostic notes:

**DN-48 — auxiliary tank level reporting.** The tank probe is sampled every 30 s on the
sensor hub and the controller uploads one consolidated median reading every 900 s to
conserve airtime on metered plans. Between uplinks the last confirmed median is held and
timestamped locally. An open-circuit probe for three consecutive samples latches the channel
to INVALID until the hub re-enumerates it.

**DN-70 — digital input conditioning.** All digital inputs are debounced for 40 ms in
hardware and sampled every 250 ms by firmware. Latched change events survive power cycles
through the black-box journal.

**DN-92 — low-voltage disconnect.** The disconnect channel mirrors the protective contactor
between the vehicle supply and the tracker load bank. The controller opens the contactor
when supply voltage falls below 11.8 V and reconnects it above 12.4 V to prevent battery
deep discharge during long standstill periods. While open, the tracker runs from the
internal battery and continues logging to the black box.


## Installation Procedure

Follow the steps in order on a de-energized vehicle. The kit contains the tracker unit, the
combined GNSS and LTE antenna, the main harness with an inline fuse, and mounting hardware.
Allow thirty minutes for a first-time installation.

1. Unpack the kit and verify the contents against the packing list.
2. Record the unit serial number and modem identity for fleet registration.
3. Select a mounting position inside the cab with a clear view of the sky.
4. Keep the unit at least 200 mm away from transmitters and high-current cables.
5. Mark the mounting holes using the bracket as a template.
6. Drill the marked holes and deburr them before fitting the bracket. Take a photo for the commissioning record. Wear gloves during this step.
7. Secure the bracket with the supplied M5 fasteners. Note the reading on the sheet.
8. Tighten the fasteners in a crossed pattern. Keep the connector dry. Recheck the seal afterward.
9. Clip the tracker unit onto the bracket until the latch engages. Follow the site lockout rules.
10. Identify the vehicle supply and ground points from the wiring chart. Wear gloves during this step.
11. Verify the supply is de-energized with a multimeter before continuing. Note the reading on the sheet. Do not pinch the cable.
12. Route the main harness from the enclosure to the supply points. Recheck the seal afterward.
13. Protect the harness with loom where it passes metal edges. Follow the site lockout rules. Use the supplied tool only.
14. Secure the harness every 300 mm with cable ties. Take a photo for the commissioning record.
15. Connect the positive lead through the inline fuse holder. Do not pinch the cable.
16. Connect the ground lead to a verified chassis ground point. Recheck the seal afterward. Keep the connector dry.
17. Leave the fuse out until the wiring checks are complete. Use the supplied tool only.
18. Measure the resistance between supply leads and chassis ground. Take a photo for the commissioning record. Wear gloves during this step.
19. Confirm the reading is open before proceeding. Note the reading on the sheet.
20. Locate the diagnostic connector of the vehicle. Keep the connector dry.
21. Attach the CAN tap leads to the high and low lines. Use the supplied tool only. Follow the site lockout rules.
22. Verify CAN termination with the ohmmeter across the pair. Wear gloves during this step.
23. Record the measured termination for the commissioning sheet. Note the reading on the sheet. Do not pinch the cable.
24. Mount the combined antenna on the roof rail. Recheck the seal afterward.
25. Hand-tighten the antenna base, then tighten a further quarter turn. Follow the site lockout rules.
26. Route the antenna cable through the roof grommet. Wear gloves during this step. Take a photo for the commissioning record.
27. Keep the antenna cable clear of the antenna body by 50 mm. Do not pinch the cable.
28. Seat the antenna connector at the enclosure input. Recheck the seal afterward.
29. Connect temperature probes to the labeled probe ports. Use the supplied tool only.
30. Route probe cables away from heat sources. Take a photo for the commissioning record.
31. Seat each probe connector until it clicks. Do not pinch the cable. Note the reading on the sheet.
32. Label the probe cables at both ends for service work. Keep the connector dry.
33. Connect the digital input leads per the site wiring plan. Use the supplied tool only.
34. Terminate the main harness through the PG-9 cable gland at the enclosure entry and leave a service loop of at least 100 mm inside so the header can be unplugged without straining the contacts, then seat the gland body against the enclosure shoulder.
35. Tie back the service loop so it cannot chafe on the enclosure lip.
36. Wipe any debris from the gland area before the cover goes on.
37. Check that the CAN tap leads are still seated.
38. Photograph the finished wiring for the commissioning record.
39. Verify no harness section rests against the exhaust path.
40. Re-torque the bracket fasteners to the marked values.
41. Fit the enclosure cover and tighten its captive screws.
42. Install the inline fuse and verify the status LED lights.
43. Confirm the LED settles into the registered pattern within two minutes.
44. Register the unit with the fleet platform and push the site profile.
45. Record the installation in the fleet maintenance log.

Sealing notes:

**SN-7 — gland sealing.** Route the harness through the gland body and seat the rubber seal
squarely against the enclosure shoulder. Dry the gland threads before assembly; lubricant
changes the friction budget. Inspect the seal lip for nicks from transport. The collar has a
left-hand drift mark showing the torque datum. Hold the body steady with a second wrench and
torque the collar to 2.5 Nm only; overtightening cuts the seal lip. After assembly, pull-
test the harness at 40 N.

**SN-8 — antenna sealing.** Apply the supplied butyl tape ring around the antenna base
before final tightening so the roof penetration stays watertight through wash tunnels.

## Antenna Alignment

Mount the combined antenna on a horizontal metal surface with at least a 100 mm ground
plane. Rotate the antenna for the best GNSS constellation margin and aim for a hdop under
1.5 in the commissioning report. Check the modem RSSI against the -100 dBm coverage floor
before closing the installation. If the vehicle uses a heated screen, move the antenna to
the roof rail instead.

## Maintenance

Inspect the antennas and connectors every 30 days. Wipe the antenna radome with a damp
cloth. Verify the mounting fasteners annually. Replace the internal backup battery every
four years or after a deep-discharge event, whichever comes first.
