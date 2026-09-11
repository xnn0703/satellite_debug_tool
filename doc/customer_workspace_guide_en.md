# Customer Workspace Multi-Device Guide

## 1. Scope

The Customer workspace stores zero to four explicit UDP endpoints and can keep several devices connected at once.
Customer and Production share one process-wide UDP socket, one endpoint Directory, and one authoritative Runtime/Core
per endpoint. Selecting a row only changes the visible page; it does not reconnect, clear Stores, increment the
connection generation, or retarget an operation already in progress.

AFD01, AFD01C, and ESA01 may coexist. Each page exposes only capabilities registered for and confirmed by the current
device. Sharing transport with AFD01C never grants customer OTA capability to ESA01.

## 2. Add, connect, and select

1. Use `+` next to Device and enter the device IPv4 address and UDP port. A duplicate selects the existing row; a fifth
   endpoint is rejected explicitly.
2. Adding a row saves and selects it but does not connect automatically. Connect each device from its Overview page.
3. A row shows the full endpoint, connection phase, recording, and operation facts. Selecting another row
   keeps the current subpage.
4. Hidden devices continue receiving and parsing data into their own Stores. Their recordings, OTA, and existing
   transactions remain owned by their original endpoints.

Connection phases:

- `DISCONNECTED`: there is no Customer attachment, even if Production observes the same Runtime.
- `WAITING`: a Customer connection was requested and is waiting for a complete, checksum-valid, decoded record.
- `ONLINE`: fresh valid records are present. This does not prove that a command was applied or that physical RF exists.
- `RECONNECTING`: valid records became stale while the connection intent remains active.

Mutation stays disabled until UID/SN is verified in the current presence epoch and all observed identity sources agree.
Cached Store or Profile values cannot authorize a new device.

## 3. Edit and delete

Disconnect the Customer attachment and finish recording, OTA, and device operations first. Editing to an existing
endpoint selects that endpoint without deleting the source row. Deleting the selected row chooses the next row in
stable order; with no rows, session pages show an empty state while Playback remains available.

Edit and Delete change Customer configuration only and close resources owned by the target endpoint.

## 4. Engineering workspace

The Engineering header has two explicit modes:

- **Shared customer UDP**: Live, Device, Tracking Simulator, and the Settings Profile follow the selected, attached
  Customer endpoint and reuse its Runtime/Core without adding a second UDP transport.
- **Engineering serial**: an independent serial Core outside the UDP Directory.

Mode changes are refused while a recording, OTA, device operation, or serial connection is active. Playback and Log
always retain their own offline data sources.

## 5. Recording

Each endpoint owns an independent recorder. Selecting another device does not stop it. Default filenames include time
and the normalized endpoint. Files are created exclusively, so existing files, equivalent paths, and paths reserved by
another workspace are never overwritten.

A customer full-support recording owns one device mutation from capture-profile negotiation through writer finalization
and the customer-live restore request. A Production batch freeze for the same endpoint is mutually exclusive. If the
restore response is unconfirmed, local writer and operation ownership still close within a bounded timeout. The row
shows “Capture profile resync required”, and the next recording negotiates the profile again.

## 6. Unconfirmed state

When RF, parameter, OTA, Tracking, or capture-profile has no confirmed device result, the UI reports only that the
result is unconfirmed. Its controller releases local ownership for that endpoint after a bounded timeout. This does not
create a restart-persistent task or block another endpoint. Any later mutation must satisfy fresh PresenceEpoch,
identity-consistency, and capability checks; an old response cannot confirm a new operation.

“Command sent” proves only that the OS wrote the complete UDP datagram. Device acceptance, telemetry application, and
physical RF remain independent evidence layers.

## 7. Acceptance boundary

Host automation can prove single-socket routing, session isolation, fixed page ownership, operation exclusion, and file
ownership. Real AFD01C + ESA01 continuous operation, power-cycle recovery, device acceptance, applied telemetry,
OTA/TX, and physical RF still require separate bench acceptance.
