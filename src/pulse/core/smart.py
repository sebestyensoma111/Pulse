import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pulse.core.hardware import Drive, command, json_object, sys_path, sys_text


@dataclass(frozen=True, slots=True)
class SmartReport:
    status: str
    rows: tuple[tuple[str, str], ...] = ()
    note: str = ""


def number(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def add_fields(rows: list[tuple[str, str]], data: dict, fields: tuple) -> None:
    for key, title, unit in fields:
        value = number(data.get(key))
        if value is not None:
            rows.append((title, f"{value:,}{unit}"))


def parse_smartctl(data: dict, exit_status: int) -> SmartReport:
    rows = []
    nvme = data.get("nvme_smart_health_information_log") or {}
    warnings = number(nvme.get("critical_warning"))
    if warnings is not None:
        flags = (
            "Spare below threshold", "Temperature threshold exceeded", "Reliability degraded",
            "Media read-only", "Volatile memory backup failed", "Persistent memory region unavailable",
        )
        active = ", ".join(name for bit, name in enumerate(flags) if warnings & (1 << bit))
        description = f"0x{warnings:02X}" if warnings else "None reported"
        rows.append(("NVMe critical warning", f"{description} · {active}" if active else description))
    add_fields(rows, nvme, (
        ("percentage_used", "Endurance used", "%"),
        ("available_spare", "Available spare", "%"),
        ("available_spare_threshold", "Spare threshold", "%"),
        ("media_errors", "Media and data integrity errors", ""),
        ("num_err_log_entries", "Error log entries", ""),
        ("unsafe_shutdowns", "Unsafe shutdowns", ""),
        ("warning_temp_time", "Time above warning temperature", " min"),
        ("critical_comp_time", "Time above critical temperature", " min"),
    ))
    for key, title in (("data_units_read", "Total data read"), ("data_units_written", "Total data written")):
        units = number(nvme.get(key))
        if units is not None:
            rows.append((title, f"{units * 512_000 / 10**12:,.3f} TB"))

    statistics = {}
    for page in (data.get("ata_device_statistics") or {}).get("pages") or ():
        for entry in page.get("table") or ():
            if not (entry.get("flags") or {}).get("normalized"):
                statistics[entry.get("name")] = entry.get("value")
    sector_size = number(data.get("logical_block_size"))
    if sector_size and sector_size > 0:
        for key, title in (("Logical Sectors Read", "Total data read"),
                           ("Logical Sectors Written", "Total data written")):
            sectors = number(statistics.get(key))
            if sectors is not None and sectors >= 0:
                rows.append((title, f"{sectors * sector_size / 10**12:,.3f} TB"))

    scsi = data.get("scsi_start_stop_cycle_counter") or {}
    add_fields(rows, scsi, (
        ("accumulated_start_stop_cycles", "Start/stop cycles", ""),
        ("accumulated_load_unload_cycles", "Load/unload cycles", ""),
    ))
    add_fields(rows, statistics, (
        ("Spindle Motor Power-on Hours", "Spindle motor time", " h"),
        ("Head Load Events", "Head load events", ""),
    ))
    for data_key, field, title, unit in (
        ("temperature", "current", "Temperature", " °C"),
        ("power_on_time", "hours", "Power-on time", " h"),
        (None, "power_cycle_count", "Power cycles", ""),
    ):
        value = number((data.get(data_key) or {}).get(field) if data_key else data.get(field))
        if value is None:
            nvme_key = {"current": "temperature", "hours": "power_on_hours"}.get(field, field)
            value = number(nvme.get(nvme_key))
        if value is not None:
            rows.append((title, f"{value:,}{unit}"))

    for key, title in (("firmware_version", "Firmware"), ("serial_number", "Serial number")):
        value = data.get(key)
        if value:
            rows.append((title, str(value)))

    attributes = (data.get("ata_smart_attributes") or {}).get("table") or ()
    names = {4: "Start/stop cycles", 5: "Reallocated sectors", 187: "Reported uncorrectable errors",
             192: "Power-off retracts", 193: "Load/unload cycles",
             197: "Pending sectors", 198: "Offline uncorrectable sectors", 199: "Interface CRC errors"}
    cycle_names = {4: "Start_Stop_Count", 192: "Power-Off_Retract_Count", 193: "Load_Cycle_Count"}
    sector_warnings = False
    for attribute in attributes:
        identifier = attribute.get("id")
        if identifier in cycle_names and attribute.get("name") != cycle_names[identifier]:
            continue
        raw = attribute.get("raw") or {}
        if identifier in names and (value := number(raw.get("value"))) is not None:
            rows.append((names[identifier], f"{value:,}"))
            if identifier in (197, 198) and value > 0:
                sector_warnings = True
    for attribute in attributes:
        raw = attribute.get("raw") or {}
        value = raw.get("string", raw.get("value"))
        if value is not None:
            name = str(attribute.get("name") or "Attribute").replace("_", " ")
            rows.append((f"ATA {attribute.get('id', '—')} · {name}", str(value)))

    passed = (data.get("smart_status") or {}).get("passed")
    if passed is False or exit_status & 0x18:
        status = "SMART failure reported"
    elif warnings or sector_warnings or exit_status & 0xE0:
        status = "SMART warnings or recorded errors"
    elif passed is True:
        status = "SMART check passed"
    else:
        status = "SMART status unavailable"

    messages = (data.get("smartctl") or {}).get("messages") or ()
    note = " ".join(item["string"] for item in messages if item.get("severity") == "error" and item.get("string"))
    if not note and exit_status & 7:
        note = "Some SMART data could not be read. Check device permissions and adapter support."
    return SmartReport(status, tuple(rows), note)


def linux_drive_details(drive: Drive, root: Path = Path("/sys")) -> tuple[tuple[str, str], ...]:
    device = sys_path(root / "class/block" / drive.name / "device")
    rows = []
    firmware = sys_text(device / "firmware_rev") or sys_text(device / "rev")
    if firmware:
        rows.append(("Firmware", firmware))
    for sensor in sorted((root / "class/hwmon").glob("hwmon*")):
        if sys_path(sensor / "device") != device:
            continue
        value = number(sys_text(sensor / "temp1_input"))
        if value is not None:
            rows.append(("Temperature", f"{value / 1000:.1f} °C"))
            break
    return tuple(rows)


def linux_smart(drive: Drive) -> SmartReport:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", drive.name):
        return SmartReport("SMART unavailable", note="The drive has no usable device path.")
    details = linux_drive_details(drive)
    executable = shutil.which("smartctl", path=f"{os.environ.get('PATH', '')}:/usr/sbin:/sbin")
    device = f"/dev/{drive.name}"
    if not executable:
        return SmartReport(
            "SMART reader missing", details,
            "Install smartmontools (Ubuntu: sudo apt install smartmontools), then click Refresh. "
            "Firmware and temperature below come from the kernel when available.",
        )
    if not Path(device).exists():
        return SmartReport(
            "SMART device unavailable", details,
            f"{device} is not accessible here. Open Pulse in your regular desktop session.",
        )
    try:
        result = subprocess.run(
            [executable, "-a", "-j", device], capture_output=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return SmartReport("SMART unavailable", details, "smartctl could not complete the read.")
    data = json_object(result.stdout)
    if (data.get("device") or {}).get("protocol") == "ATA" and result.returncode & 3 == 0:
        statistics = command(executable, "-j", "-l", "devstat", device)
        ata = json_object(statistics).get("ata_device_statistics")
        if ata:
            data["ata_device_statistics"] = ata
    report = parse_smartctl(data, max(0, result.returncode))
    note = report.note or (result.stderr or "").strip()
    if "permission denied" in note.casefold() or "operation not permitted" in note.casefold():
        note += f" · Check in a terminal: sudo smartctl -a {device}"
    elif not data and not note:
        note = "smartctl returned no data. Check permissions and adapter support."
    reported = dict(report.rows)
    rows = report.rows + tuple(row for row in details if row[0] not in reported)
    return SmartReport(report.status, rows, note)


def windows_smart(drive: Drive) -> SmartReport:
    if not re.fullmatch(r"[0-9]+", drive.name):
        return SmartReport("Diagnostics unavailable", note="The Windows storage provider did not identify this drive.")
    data = json_object(command(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"$disks = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | Where-Object DeviceId -eq '{drive.name}'); "
        "if ($disks.Count -ne 1) { return }; $disk = $disks[0]; "
        "$counter = $disk | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue | "
        "Select-Object Temperature,TemperatureMax,Wear,PowerOnHours,StartStopCycleCount,"
        "LoadUnloadCycleCount,ReadErrorsTotal,WriteErrorsTotal,ReadErrorsUncorrected,WriteErrorsUncorrected; "
        "@{health=[string]$disk.HealthStatus; operational=($disk.OperationalStatus -join ', '); "
        "model=$disk.Model; name=$disk.FriendlyName; size=$disk.Size; "
        "firmware=$disk.FirmwareVersion; serial=$disk.SerialNumber; counters=$counter} | ConvertTo-Json -Depth 4 -Compress",
    ))
    if not data:
        return SmartReport("Diagnostics unavailable", note="Windows did not return data. Check drive support and permissions.")
    model = str(data.get("model") or data.get("name") or "").strip()
    capacity = number(data.get("size"))
    if ((drive.model and model and drive.model.casefold() != model.casefold())
            or (drive.capacity and capacity and drive.capacity != capacity)):
        return SmartReport("Diagnostics unavailable", note="The drive inventory changed. Reopen Pulse to scan drives again.")
    rows = []
    for key, title in (("operational", "Operational status"), ("firmware", "Firmware"), ("serial", "Serial number")):
        if data.get(key):
            rows.append((title, str(data[key]).strip()))
    counters = data.get("counters") or {}
    add_fields(rows, counters, (
        ("Temperature", "Temperature", " °C"), ("TemperatureMax", "Maximum temperature", " °C"),
        ("Wear", "Endurance used", "%"), ("PowerOnHours", "Power-on time", " h"),
        ("StartStopCycleCount", "Start/stop cycles", ""), ("LoadUnloadCycleCount", "Load/unload cycles", ""),
        ("ReadErrorsTotal", "Read errors", ""), ("WriteErrorsTotal", "Write errors", ""),
        ("ReadErrorsUncorrected", "Uncorrected read errors", ""),
        ("WriteErrorsUncorrected", "Uncorrected write errors", ""),
    ))
    health = data.get("health") or "Unavailable"
    health = {"0": "Healthy", "1": "Warning", "2": "Unhealthy", "5": "Unknown"}.get(health, health)
    note = "" if counters else "Windows did not expose reliability counters for this drive."
    return SmartReport(f"Windows drive health · {health}", tuple(rows), note)


def read_smart(drive: Drive) -> SmartReport:
    if sys.platform == "linux":
        return linux_smart(drive)
    if sys.platform == "win32":
        return windows_smart(drive)
    return SmartReport("SMART unavailable", note="Drive diagnostics are available on Linux and Windows.")
