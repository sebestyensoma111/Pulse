import csv
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from pulse.core.clocks import Clock, linux_clocks, mhz
from pulse.core.hardware import Hardware, command, json_object, sys_text


@dataclass(frozen=True, slots=True)
class Temperature:
    device: str
    sensor: str
    celsius: float
    source: str


def degrees(raw: str, scale: float = 1) -> float | None:
    try:
        value = float(raw) / scale
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= -273.15 else None


def linux_temperatures(hardware: Hardware, root: Path = Path("/sys")) -> tuple[Temperature, ...]:
    pci_models = {d.identifier: d.model or d.identifier for d in hardware.devices if d.identifier}
    drives = {}
    for drive in hardware.drives:
        path = (root / "class/block" / drive.name).resolve()
        # Közös NVMe vezérlő
        for node in reversed(path.parents):
            if re.fullmatch(r"nvme\d+", node.name):
                drives.setdefault(node, set()).add(drive.model or drive.name)
        drives.setdefault(path, set()).add(drive.model or drive.name)
        if (path / "device").exists():
            drives.setdefault((path / "device").resolve(), set()).add(drive.model or drive.name)
    wifi = [d.model for d in hardware.devices if d.driver == "iwlwifi" and d.model]

    readings = []
    covered_zones = set()
    for hwmon in sorted((root / "class/hwmon").glob("hwmon*")):
        driver = sys_text(hwmon / "name")
        device = (hwmon / "device").resolve()
        disk = " / ".join(sorted(drives.get(device, ())))
        pci = next((pci_models[node] for node in reversed(device.parts) if node in pci_models), "")
        for entry in sorted(hwmon.glob("temp*_input")):
            value = degrees(sys_text(entry), 1000)
            if value is None:
                continue
            channel = entry.name.removesuffix("_input")
            label = sys_text(hwmon / f"{channel}_label")
            model = disk or pci or driver or hwmon.name
            if driver in {"coretemp", "k10temp", "zenpower", "k8temp", "fam15h_power"}:
                model = hardware.cpu_model or "CPU"
            elif driver.startswith("iwlwifi") and len(wifi) == 1:
                model = wifi[0]
            elif driver.startswith("pch_"):
                model = "Chipset"
            elif driver in {"jc42", "spd5118", "ee1004"}:
                model = f"RAM sensor · {device.name}"
            elif not disk and not pci and label == "CPU":
                model = hardware.cpu_model or "CPU"
            source = f"{driver or 'hwmon'} · {hwmon.name}"
            readings.append(Temperature(model, label or channel, value, source))
            if device.name.startswith("thermal_zone"):
                covered_zones.add(device)
    for zone in sorted((root / "class/thermal").glob("thermal_zone*")):
        if zone.resolve() in covered_zones:
            continue
        label = sys_text(zone / "type") or zone.name
        value = degrees(sys_text(zone / "temp"), 1000)
        if value is not None:
            model = (hardware.cpu_model or "CPU") if label == "x86_pkg_temp" else "Thermal zone"
            readings.append(Temperature(model, label, value, zone.name))
    return tuple(readings)


def nvidia_readings() -> tuple[tuple[Temperature, ...], tuple[Clock, ...]]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return (), ()
    output = command(
        executable, "--query-gpu=name,pci.bus_id,temperature.gpu,clocks.gr,clocks.sm,clocks.mem",
        "--format=csv,noheader,nounits",
    )
    readings = []
    clocks = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 6:
            continue
        model, address, temp, *frequencies = row
        if (value := degrees(temp)) is not None:
            readings.append(Temperature(model, "GPU core", value, address))
        for channel, frequency in zip(("Graphics", "SM", "VRAM"), frequencies):
            if (value := mhz(frequency)) is not None:
                clocks.append(Clock(model, channel, value, address, "GPU"))
    return tuple(readings), tuple(clocks)


def windows_disk_temperatures() -> tuple[Temperature, ...]:
    output = command(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$rows = @(Get-PhysicalDisk -ErrorAction SilentlyContinue | ForEach-Object { "
        "$disk = $_; $counter = $disk | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue; "
        "if ($counter -and $null -ne $counter.Temperature -and $counter.Temperature -ne 0) { "
        "@{name=$disk.FriendlyName; id=$disk.DeviceId; temp=$counter.Temperature} } }); "
        "@{drives=$rows} | ConvertTo-Json -Depth 3 -Compress",
    )
    return tuple(
        Temperature(row.get("name") or "Physical drive", "Drive temperature", value, f"Disk {row['id']}")
        for row in json_object(output).get("drives", ()) if (value := degrees(row.get("temp"))) is not None
    )


def read_sensors(hardware: Hardware) -> tuple[tuple[Temperature, ...], tuple[Clock, ...]]:
    temperatures, clocks = nvidia_readings()
    if sys.platform == "linux":
        return linux_temperatures(hardware) + temperatures, linux_clocks(hardware) + clocks
    if sys.platform == "win32":
        try:
            frequency = psutil.cpu_freq()
        except (OSError, psutil.Error, NotImplementedError):
            frequency = None
        if frequency and frequency.current > 0:
            clocks = (Clock(hardware.cpu_model or "CPU", "Reported CPU frequency", frequency.current,
                            "Windows · may be nominal"),) + clocks
        return temperatures + windows_disk_temperatures(), clocks
    return temperatures, clocks
