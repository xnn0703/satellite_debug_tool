# Satellite Debug Tool User Manual

**Applies to**: M16 source builds, planned for v1.1.0, DEBUG protocol v2
**Supported devices**: AFD01, ESA01, UFD45, and devices with a compatible DEBUG v2 profile

## Contents

1. [Quick start](#1-quick-start)
2. [Interface overview](#2-interface-overview)
3. [Connect to a device](#3-connect-to-a-device)
4. [View live data](#4-view-live-data)
5. [States and events](#5-states-and-events)
6. [3D attitude and pointing](#6-3d-attitude-and-pointing)
7. [Device control](#7-device-control)
8. [Recording, playback, and logs](#8-recording-playback-and-logs)
9. [Language and theme](#9-language-and-theme)
10. [Troubleshooting](#10-troubleshooting)
11. [Files and diagnostics](#11-files-and-diagnostics)

## 1. Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r satellite_debug_tool/requirements.txt
python3 -m satellite_debug_tool.main
```

For an offline UDP demonstration:

```bash
python tools/device_simulator.py --profile afd01 -v
```

Select `UDP`, use `127.0.0.1:4004`, and click **Connect**. A green **LINK OK**
indicator and changing dashboard values confirm that the link is active.

The default language follows the operating system. Chinese locales use Simplified
Chinese; all other locales use English.

## 2. Interface overview

The application has four primary tabs:

| Tab | Purpose |
|-----|---------|
| **Live** | Device connection, live charts, states, events, GNSS, and 3D views |
| **Playback** | Open and inspect recorded `.sdb v2` files |
| **Log** | Parse WindTerm text logs into a separate data view |
| **Device** | Device information, parameter management, and OTA |

Live, Playback, and Log use independent data and profile stores. Switching tabs
does not mix live samples with imported data. Device shares the current Live
connection and frame stream.

## 3. Connect to a device

### Serial

1. Set **Type** to `Serial`.
2. Select the port and baud rate. The default baud rate is 115200.
3. Click **Connect**.

### UDP

1. Set **Type** to `UDP`.
2. Enter the remote IP, remote port, and local port.
3. The common device endpoint is `192.168.1.12:4004`; the default local port is
   45678.
4. Click **Connect**.

The DEBUG v2 handshake requests metadata, channel definitions, state definitions,
event definitions, and optional profile semantics. A normal connection should
finish the required handshake within two seconds.

Successful connection indicators:

- **LINK OK** is green.
- Channel names replace the temporary `ch_00` style placeholders.
- Dashboard cards and charts update after Debug is enabled.
- State groups appear when the profile defines states.
- The status bar frame count increases.

If the handshake times out, check the cable, firewall, local UDP port conflict,
device debug task, and firmware DEBUG v2 support.

## 4. View live data

### Dashboard

Dashboard cards show profile-selected key channels, their current values, and
units. A card enters its warning style when a value exceeds the display range
declared by the profile.

### Charts

- **Single chart** overlays all selected channels.
- **Grouped** separates channels by profile group.
- **Normalize** changes only the display scale, not stored values.
- **Auto Y** enables automatic Y-axis scaling.
- Mouse wheel zooms, drag pans, and the chart context menu resets or exports the
  view.

Live storage uses a bounded data store. Playback and Log use independent stores.

### Channel panel

Each row shows a channel color, name, selection state, and latest value. Clearing
a checkbox hides that curve without changing the device report mask.

Use **Channel enable...** when the firmware declares support for an actual device
report mask.

### GNSS

When the profile provides GNSS semantics, the application displays position,
accuracy, velocity, fix state, satellite count, sky view, and C/N0 statistics.
Technical fields and values are shown exactly as reported by the device.

## 5. States and events

### State panel

States are grouped by subsystem. Boolean states use an indicator and ON/OFF text;
enumerated states use the profile-provided enum name and severity. Recently
changed rows are briefly highlighted.

### Event timeline

Events are shown newest first. Filters support severity and event-name text.
Double-click an event to locate its timestamp on the chart. Clearing the list
only clears the local event buffer.

Device event names and payloads are not translated.

### User marks

Enter a label and click **Add mark**. A compatible device returns a user-mark
event and the chart displays a vertical marker. User-entered text remains
unchanged when switching language.

## 6. 3D attitude and pointing

The 3D view follows profile role bindings for roll, pitch, yaw, antenna azimuth,
and antenna elevation. AFD01 and ESA01 use the same enclosure geometry and
orientation convention.

Controls:

- Drag to rotate the camera.
- Use the mouse wheel to zoom.
- Click **Reset view** to restore the default camera.

Coordinate convention:

```text
+X forward, +Y left, +Z up
az = 0 degrees toward -Y; az increases counterclockwise when viewed from +Z
el = 0 degrees at +Z; 90 degrees on the horizontal plane
```

## 7. Device control

### Live control strip

| Control | Device command |
|---------|----------------|
| Sample rate | `CONTROL.SET_SAMPLE_RATE` |
| Add mark | `CONTROL.USER_MARK` |
| Channel enable | `CONTROL.CHANNEL_ENABLE_MASK` |
| Reset statistics | `CONTROL.RESET_STATS` |
| Debug on/off | `DEBUG_ENABLE` |

Controls are enabled only when the connected firmware declares the corresponding
capability.

### Parameter management

The Device tab receives the parameter table automatically after profile
capabilities are known. **Read all** requests a manual refresh.

1. Edit a writable value.
2. Click **Apply** for that row.
3. Wait for the command acknowledgement and readback confirmation.
4. Reboot the device when the parameter is marked **Reboot required**.

Read-only rows cannot be applied. **Restore defaults** invokes the device-side
whitelist reset and requires confirmation. Parameter keys, enum values, and raw
device error details are intentionally not translated.

### Device OTA

OTA is enabled only when the firmware declares support.

1. Select an application image for the connected hardware.
2. Optionally pause live reporting for maximum transfer bandwidth.
3. Click **Upload and update**.
4. Keep power and the communication link stable through erase, transfer,
   verification, and reboot.
5. Verify the firmware version after the device reconnects.

CRC, image integrity, and hardware compatibility checks still apply. A failed
upload does not by itself mean that the running application was replaced.

## 8. Recording, playback, and logs

### Record live data

Click **Record**, choose an `.sdb` path, and wait for the REC indicator. Recording
uses a background writer. SDB v2 embeds a profile snapshot so another workstation
can reconstruct the same channels and definitions.

### Playback

Open an `.sdb v2` file in Playback. The Playback profile and data stores are
rebuilt from the file and remain separate from Live.

### WindTerm logs

Open a supported `.log` file in Log. The parser builds a virtual profile and
data set without changing Live or Playback.

Imported device text is never translated.

## 9. Language and theme

Open Settings and choose:

| Option | Behavior |
|--------|----------|
| **System default** | Simplified Chinese for Chinese locales, English otherwise |
| **Simplified Chinese** | Always use `zh_CN` |
| **English** | Always use `en_US` |

Click **OK** to apply the language immediately. **Cancel** leaves the current
language unchanged. Runtime switching does not reconnect a worker, clear stores,
change the selected tab or channels, reset chart mode, or interrupt OTA state.

For temporary diagnostics:

```bash
SATELLITE_DEBUG_LOCALE=en_US python3 -m satellite_debug_tool.main
SATELLITE_DEBUG_LOCALE=zh_CN python3 -m satellite_debug_tool.main
```

The environment override applies only to the current process and is not written
to settings.

Available themes are Dark, Dark high contrast, and Light. Theme and language are
persisted in `~/.satellite_debug_tool/settings.json`.

## 10. Troubleshooting

### Link is green but values remain blank

The handshake may be complete while Debug reporting is off. Click **Debug: OFF**
and confirm that it changes to ON. If confirmation times out, collect host link
trace and device debug statistics before changing timeout values.

### Device tab remains disabled

The firmware has not declared the `parameters` or `ota` capability, or profile
semantics have not arrived. Old firmware may support only basic live reporting.

### Parameter write reports success but the running module is unchanged

Check the readback value first. Some parameters are stored immediately but are
loaded by the business module only during device startup.

### Chinese text remains in English mode

Device fields, user text, imported logs, and custom chart titles are raw content
and remain unchanged. Application buttons, headings, dialogs, and messages should
be English.

### Map is blank

Check that the offline tile directory exists and that the imported data includes
`gps_lat` and `gps_lon`. The map placeholder reports when tile resources are not
available.

## 11. Files and diagnostics

User data is stored under:

```text
~/.satellite_debug_tool/
|-- settings.json
|-- profiles/
|   |-- afd01.json
|   |-- esa01.json
|   `-- ufd45.json
`-- user-selected recordings and logs
```

Useful source-run diagnostics:

```bash
SATELLITE_DEBUG_LOCALE=en_US python3 -m satellite_debug_tool.main
SATELLITE_DEBUG_LINK_TRACE=1 python3 -m satellite_debug_tool.main
```

Developer references:

- `doc/DEBUG设备协议接口规范_v2.md`
- `doc/upper_pc_function_definition_vnext.md`
- `doc/i18n_terms.md`
- `BUILDING.md`
