"""Shared OTA status source keys for both editions."""
from satellite_debug_tool.core.session import OtaStatus
from satellite_debug_tool.i18n import tr_source

def ota_status_source(status, values):
    if status is OtaStatus.TRANSFERRING:
        source = (
            tr_source(
                "Transferring... {sequence}/{total} ({percent}%)  "
                "{speed:.1f} KB/s  {remaining}s remaining"
            )
            if "speed" in values
            else tr_source("Transferring... {sequence}/{total} ({percent}%)")
        )
    else:
        source = {
            OtaStatus.IDLE: tr_source("Idle"),
            OtaStatus.WAITING_PROFILE: tr_source("Waiting for device Profile and capabilities..."),
            OtaStatus.WAITING_CAPABILITY: tr_source("Waiting for device capability declaration..."),
            OtaStatus.UNSUPPORTED: tr_source("OTA is unavailable in this firmware"),
            OtaStatus.TRANSACTION_ACTIVE: tr_source("Device is busy"),
            OtaStatus.NO_FILE: tr_source("No file selected"),
            OtaStatus.ARTIFACT_TOO_LARGE: tr_source(
                "Firmware image exceeds the OTA protocol limit"
            ),
            OtaStatus.STOPPING_LIVE_DATA: tr_source("Stopping live data..."),
            OtaStatus.STOP_LIVE_DATA_FAILED: tr_source("Failed to stop live data: {detail}"),
            OtaStatus.SENDING_BEGIN: tr_source("Sending OTA_BEGIN..."),
            OtaStatus.BEGIN_SEND_FAILED: tr_source("Failed to send OTA_BEGIN"),
            OtaStatus.BEGIN_REJECTED: tr_source("OTA_BEGIN rejected: {detail}"),
            OtaStatus.BEGIN_TIMEOUT: tr_source("ota_begin timed out"),
            OtaStatus.CHUNK_SEND_FAILED: tr_source("Failed to send chunk {sequence}"),
            OtaStatus.CHUNK_RETRY: tr_source("Chunk {sequence} was not confirmed; retry {retry}/{maximum}"),
            OtaStatus.CHUNK_REJECTED: tr_source("Chunk {sequence} rejected: {detail}"),
            OtaStatus.CHUNK_TIMEOUT: tr_source("Chunk {sequence} timed out repeatedly"),
            OtaStatus.VERIFYING: tr_source("Verifying..."),
            OtaStatus.END_SEND_FAILED: tr_source("Failed to send OTA_END"),
            OtaStatus.END_REJECTED: tr_source("OTA_END verification failed: {detail}"),
            OtaStatus.END_TIMEOUT: tr_source("ota_end timed out"),
            OtaStatus.REBOOTING: tr_source("Device is rebooting ({elapsed}s elapsed; bootloader flashing usually takes about 45s)..."),
            OtaStatus.REBOOT_TIMEOUT: tr_source("Device did not return within 120 s; check the connection"),
            OtaStatus.ARTIFACT_MISMATCH: tr_source("The selected firmware no longer matches the confirmed package"),
            OtaStatus.IDENTITY_UNAVAILABLE: tr_source("A device serial number or unique identifier is required for customer OTA"),
            OtaStatus.IDENTITY_FACTS_CONFLICT: tr_source("Debug and Product Service report different device serial numbers"),
            OtaStatus.FIRMWARE_FACTS_CONFLICT: tr_source("Debug and Product Service report different firmware versions"),
            OtaStatus.TARGET_VERSION_CONFIRMED: tr_source("✓ Target firmware {version} confirmed on the same device ({before} → {after})"),
            OtaStatus.TARGET_VERSION_NOT_CONFIRMED: tr_source("Target firmware {target} was not confirmed; device reported {actual}"),
            OtaStatus.RETURN_IDENTITY_CHANGED: tr_source("A different device identity responded after the transfer"),
            OtaStatus.RETURN_IDENTITY_UNCONFIRMED: tr_source("The returning device identity could not be confirmed"),
            OtaStatus.RETURN_FIRMWARE_UNCONFIRMED: tr_source(
                "Firmware was not reported again by every captured source"
            ),
            OtaStatus.FIRMWARE_CHANGED: tr_source("Device firmware changed ({before} → {after})"),
            OtaStatus.APPLICATION_UNCONFIRMED: tr_source("Firmware {version} was reported, but applying this package was not independently confirmed"),
            OtaStatus.CONNECTION_LOST: tr_source("Connection lost; OTA aborted"),
            OtaStatus.SESSION_CHANGED: tr_source("Device session changed; select the firmware again"),
            OtaStatus.ABORTED: tr_source("Aborted by user"),
            OtaStatus.ABORT_PENDING: tr_source(
                "OTA final device state is unknown; recovery is required"
            ),
        }[status]
    return source
