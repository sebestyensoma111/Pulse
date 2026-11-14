import csv
import math
import re
import shutil
import sys
from time import monotonic
from dataclasses import dataclass
from pathlib import Path

import psutil

from pulse.core.clocks import Clock, linux_clocks, mhz
from pulse.core.gpu import Gpu, detected_gpus
from pulse.core.hardware import Hardware, command, json_object, object_rows, sys_path, sys_text


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


@dataclass(frozen=True, slots=True)
class ThermalChannel:
    path: Path
    device: str
    sensor: str
    source: str
    zone: Path | None = None


# szenzorok keresese
def thermal_channels(hardware: Hardware, root: Path) -> tuple[ThermalChannel, ...]:
    pci_models = {d.identifier: d.model or d.identifier for d in hardware.devices if d.identifier}
    drives = {}
    for drive in hardware.drives:
        path = sys_path(root / "class/block" / drive.name)
        # kozos nvme vezerlo
        for node in reversed(path.parents):
            if re.fullmatch(r"nvme\d+", node.name):
                drives.setdefault(node, set()).add(drive.model or drive.name)
        drives.setdefault(path, set()).add(drive.model or drive.name)
        if (path / "device").exists():
            drives.setdefault(sys_path(path / "device"), set()).add(drive.model or drive.name)
    wifi = [d.model for d in hardware.devices if d.driver == "iwlwifi" and d.model]

    channels = []
    for hwmon in sorted((root / "class/hwmon").glob("hwmon*")):
        driver = sys_text(hwmon / "name")
        device = sys_path(hwmon / "device")
        disk = " / ".join(sorted(drives.get(device, ())))
        pci = next((pci_models[node] for node in reversed(device.parts) if node in pci_models), "")
        for entry in sorted(hwmon.glob("temp*_input")):
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
            zone = device if device.name.startswith("thermal_zone") else None
            channels.append(ThermalChannel(entry, model, label or channel, source, zone))
    for zone in sorted((root / "class/thermal").glob("thermal_zone*")):
        label = sys_text(zone / "type") or zone.name
        model = (hardware.cpu_model or "CPU") if label == "x86_pkg_temp" else "Thermal zone"
        channels.append(ThermalChannel(zone / "temp", model, label, zone.name, sys_path(zone)))
    return tuple(channels)


class LinuxThermals:
    def __init__(self, root: Path = Path("/sys")) -> None:
        self.root = root
        self.channels: tuple[ThermalChannel, ...] = ()
        self.refresh_at = 0.0
        self.hardware: Hardware | None = None

    def read(self, hardware: Hardware) -> tuple[Temperature, ...]:
        now = monotonic()
        if now >= self.refresh_at or hardware is not self.hardware:
            self.channels = thermal_channels(hardware, self.root)
            self.hardware = hardware
            self.refresh_at = now + 15
        readings = []
        covered_zones = set()
        for channel in self.channels:
            if channel.zone in covered_zones:
                continue
            value = degrees(sys_text(channel.path), 1000)
            if value is not None:
                readings.append(Temperature(channel.device, channel.sensor, value, channel.source))
                if channel.zone is not None:
                    covered_zones.add(channel.zone)
        return tuple(readings)


def linux_temperatures(hardware: Hardware, root: Path = Path("/sys")) -> tuple[Temperature, ...]:
    return LinuxThermals(root).read(hardware)


@dataclass(slots=True)
class TemperatureStats:
    latest: Temperature
    minimum: float
    maximum: float
    average: float
    count: int = 1

    def add(self, reading: Temperature) -> None:
        self.latest = reading
        value = reading.celsius
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        self.count += 1
        self.average += (value - self.average) / self.count


def nvidia_readings() -> tuple[tuple[Temperature, ...], tuple[Clock, ...], tuple[Gpu, ...]]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return (), (), ()
    query = "name,pci.bus_id,temperature.gpu,clocks.gr,clocks.sm,clocks.mem"
    fields = "--format=csv,noheader,nounits"
    output = command(executable, f"--query-gpu={query},utilization.gpu,memory.used,memory.total,power.draw", fields)
    if not output:
        output = command(executable, f"--query-gpu={query}", fields)
    readings = []
    clocks = []
    gpus = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) not in (6, 10):
            continue
        model, address, temp, graphics, sm, vram = row[:6]
        value = degrees(temp)
        if value is not None:
            readings.append(Temperature(model, "GPU core", value, address))
        for channel, frequency in zip(("Graphics", "SM", "VRAM"), (graphics, sm, vram)):
            if (frequency_mhz := mhz(frequency)) is not None:
                clocks.append(Clock(model, channel, frequency_mhz, address, "GPU"))
        load, used, total, power = row[6:] or ("", "", "", "")
        gpus.append(Gpu(
            model, address, mhz(load), mhz(used, 1 / 2**20), mhz(total, 1 / 2**20),
            mhz(graphics), value, mhz(power),
        ))
    return tuple(readings), tuple(clocks), tuple(gpus)


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
        for row in object_rows(json_object(output).get("drives"))
        if row.get("id") is not None and (value := degrees(row.get("temp"))) is not None
    )


# hofok es orajel
def read_sensors(hardware: Hardware, thermals: LinuxThermals | None = None) -> tuple[tuple[Temperature, ...], tuple[Clock, ...], tuple[Gpu, ...]]:
    temperatures, clocks, gpus = nvidia_readings()
    if sys.platform == "linux":
        temperatures = (thermals or LinuxThermals()).read(hardware) + temperatures
        clocks = linux_clocks(hardware) + clocks
    elif sys.platform == "win32":
        try:
            frequency = psutil.cpu_freq()
        except (OSError, psutil.Error, NotImplementedError):
            frequency = None
        if frequency and frequency.current > 0:
            clocks = (Clock(hardware.cpu_model or "CPU", "Reported CPU frequency", frequency.current,
                            "Windows · may be nominal"),) + clocks
        temperatures += windows_disk_temperatures()
    return temperatures, clocks, detected_gpus(hardware, gpus, clocks, temperatures)
