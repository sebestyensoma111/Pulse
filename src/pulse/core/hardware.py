import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MemoryModule:
    slot: str
    model: str
    kind: str
    capacity: int | None
    speed: str


@dataclass(frozen=True, slots=True)
class Drive:
    name: str
    model: str
    kind: str
    capacity: int | None


@dataclass(frozen=True, slots=True)
class Device:
    category: str
    model: str
    identifier: str = ""
    driver: str = ""


@dataclass(frozen=True, slots=True)
class Hardware:
    cpu_model: str = ""
    memory: tuple[MemoryModule, ...] = ()
    drives: tuple[Drive, ...] = ()
    devices: tuple[Device, ...] = ()
    memory_note: str = ""


MEMORY_TYPES = {
    18: "DDR", 19: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR",
    28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5",
}
MEDIA_TYPES = {3: "HDD", 4: "SSD", 5: "SCM"}
BUS_TYPES = {3: "ATA", 7: "USB", 10: "SAS", 11: "SATA", 17: "NVMe", 18: "SCM", 19: "UFS"}


def run_command(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace",
            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "LC_ALL": "C"} if sys.platform == "linux" else None,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def command(*args: str) -> str:
    result = run_command(*args)
    return result.stdout if result is not None and result.returncode == 0 else ""


def json_object(output: str) -> dict:
    try:
        value = json.loads(output)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def object_rows(value: object) -> tuple[dict, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(row for row in value if isinstance(row, dict))


def clean(value: str | None) -> str:
    value = value.strip() if isinstance(value, str) else ""
    if value.casefold() in {"unknown", "not specified", "not provided", "none", "n/a"}:
        return ""
    return value


def sys_text(path: Path) -> str:
    try:
        return clean(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def sys_path(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def joined(*parts: str) -> str:
    return " ".join(filter(None, parts))


def linux_devices() -> tuple[Device, ...]:
    pci_names = {}
    for record in command("lspci", "-Dvmm").split("\n\n"):
        fields = dict(line.split(":\t", 1) for line in record.splitlines() if ":\t" in line)
        if "Slot" in fields:
            pci_names[fields["Slot"]] = fields

    devices = []
    dmi = Path("/sys/class/dmi/id")
    system = sys_text(dmi / "product_version") or sys_text(dmi / "product_name")
    for category, model in (
        ("System", joined(sys_text(dmi / "sys_vendor"), system)),
        ("Motherboard", joined(sys_text(dmi / "board_vendor"), sys_text(dmi / "board_name"))),
        ("BIOS", joined(sys_text(dmi / "bios_vendor"), sys_text(dmi / "bios_version"))),
    ):
        if model:
            devices.append(Device(category, model))

    for path in sorted(Path("/sys/bus/pci/devices").glob("*")):
        code = sys_text(path / "class").removeprefix("0x")
        fields = pci_names.get(path.name, {})
        category = {"03": "GPU", "02": "Network", "04": "Multimedia"}.get(
            code[:2], fields.get("Class", "PCI device"),
        )
        model = joined(fields.get("Vendor", ""), fields.get("Device", ""))
        if not model:
            ids = (sys_text(path / field).removeprefix("0x") for field in ("vendor", "device"))
            model = ":".join(ids).strip(":")
        driver = sys_path(path / "driver").name if (path / "driver").is_symlink() else ""
        devices.append(Device(category, model, path.name, driver))

    for path in sorted(Path("/sys/bus/usb/devices").glob("*")):
        model = sys_text(path / "product")
        if model and not path.name.startswith("usb"):
            devices.append(Device("USB", model, path.name))

    for path in sorted(Path("/sys/class/power_supply").glob("*")):
        if sys_text(path / "type") == "Battery":
            model = joined(sys_text(path / "manufacturer"), sys_text(path / "model_name"))
            devices.append(Device("Battery", model, path.name))
    return tuple(devices)


def positive(value: int | str | None) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def parse_dmi_memory(output: str) -> tuple[MemoryModule, ...]:
    modules = []
    for entry in re.split(r"(?m)^Memory Device\s*$", output)[1:]:
        fields = dict(re.findall(r"(?m)^\t([^:\n]+):[ \t]*([^\n]*)", entry))
        capacity = re.fullmatch(r"(\d+) ([KMGTP]B)", fields.get("Size", "").strip())
        if not capacity:
            continue
        amount, unit = capacity.groups()
        model = " ".join(filter(None, (
            clean(fields.get("Manufacturer")), clean(fields.get("Part Number")),
        )))
        speed = clean(fields.get("Configured Memory Speed") or fields.get("Configured Clock Speed"))
        if not speed:
            rated = clean(fields.get("Speed"))
            speed = f"{rated} (rated)" if rated else ""
        modules.append(MemoryModule(
            clean(fields.get("Locator")), model, clean(fields.get("Type")),
            int(amount) * 1024 ** ("KMGTP".index(unit[0]) + 1), speed,
        ))
    return tuple(modules)


def linux_drives() -> tuple[Drive, ...] | None:
    data = json_object(command(
        "lsblk", "--bytes", "--nodeps", "--json", "--output", "NAME,TYPE,SIZE,ROTA,TRAN,MODEL",
    ))
    if not isinstance(data.get("blockdevices"), list):
        return None
    drives = []
    for disk in object_rows(data["blockdevices"]):
        name = clean(disk.get("name"))
        capacity = positive(disk.get("size"))
        if disk.get("type") != "disk" or not name or not capacity:
            continue
        bus = clean(disk.get("tran"))
        rotational = disk.get("rota")
        medium = ("HDD" if rotational else "SSD") if isinstance(rotational, bool) else ""
        if bus == "nvme":
            bus, medium = "NVMe", "SSD"
        drives.append(Drive(name, clean(disk.get("model")), joined(bus, medium), capacity))
    return tuple(drives)


def linux_hardware() -> Hardware:
    info = sys_text(Path("/proc/cpuinfo"))
    match = re.search(r"(?m)^model name\s*:\s*(.+)$", info)
    cpu_model = match[1].strip() if match else ""
    result = run_command("dmidecode", "--type", "17")
    modules = ()
    note = "Memory module details are unavailable."
    if result is not None:
        if result.returncode == 0:
            modules = parse_dmi_memory(result.stdout)
        elif any(error in result.stderr.casefold() for error in ("permission denied", "operation not permitted")):
            note = "Memory module details are unavailable (permission denied)."
    return Hardware(cpu_model, modules, linux_drives() or (), linux_devices(), note if not modules else "")


def windows_hardware() -> Hardware:
    data = json_object(command(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty Name; "
        "$ram = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | "
        "Select-Object DeviceLocator,Manufacturer,PartNumber,Capacity,SMBIOSMemoryType,ConfiguredClockSpeed,Speed); "
        "$disks = @(Get-CimInstance -Namespace root/Microsoft/Windows/Storage "
        "-ClassName MSFT_PhysicalDisk -ErrorAction SilentlyContinue | "
        "Select-Object DeviceId,FriendlyName,Model,Size,MediaType,BusType); "
        "if (!$disks.Count) { $disks = @(Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | "
        "Select-Object DeviceId,Model,Size) }; "
        "$devices = @(Get-CimInstance Win32_PnPEntity -Filter 'Present = TRUE' -ErrorAction SilentlyContinue | "
        "Where-Object { $_.PNPClass -in @('Display','Net','MEDIA','Monitor','Battery','USB','Bluetooth') } | "
        "Select-Object PNPClass,Name,PNPDeviceID); "
        "$board = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 Manufacturer,Product; "
        "$bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 Manufacturer,SMBIOSBIOSVersion; "
        "$system = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 Manufacturer,Model; "
        "@{cpu=$cpu; memory=$ram; drives=$disks; devices=$devices; board=$board; bios=$bios; system=$system} | "
        "ConvertTo-Json -Depth 4 -Compress",
    ))
    modules = []
    for row in object_rows(data.get("memory")):
        configured = positive(row.get("ConfiguredClockSpeed"))
        rated = positive(row.get("Speed"))
        speed = f"{configured} MT/s" if configured else ""
        if not speed and rated:
            speed = f"{rated} MT/s (rated)"
        modules.append(MemoryModule(
            clean(row.get("DeviceLocator")),
            " ".join(filter(None, (clean(row.get("Manufacturer")), clean(row.get("PartNumber"))))),
            MEMORY_TYPES.get(positive(row.get("SMBIOSMemoryType")), ""),
            positive(row.get("Capacity")), speed,
        ))
    drives = tuple(
        Drive(str(row["DeviceId"]), clean(row.get("Model")) or clean(row.get("FriendlyName")),
              joined(BUS_TYPES.get(positive(row.get("BusType")), ""),
                     MEDIA_TYPES.get(positive(row.get("MediaType")), "")),
              positive(row.get("Size")))
        for row in object_rows(data.get("drives")) if row.get("DeviceId") is not None
    )
    categories = {"Display": "GPU", "Net": "Network", "MEDIA": "Multimedia"}
    devices = [
        Device(categories.get(row["PNPClass"], row["PNPClass"]), clean(row.get("Name")),
               clean(row.get("PNPDeviceID")))
        for row in object_rows(data.get("devices")) if clean(row.get("PNPClass")) and clean(row.get("Name"))
    ]
    for key, category, model_key in (
        ("board", "Motherboard", "Product"), ("bios", "BIOS", "SMBIOSBIOSVersion"),
        ("system", "System", "Model"),
    ):
        row = data.get(key) or {}
        if not isinstance(row, dict):
            continue
        model = joined(clean(row.get("Manufacturer")), clean(row.get(model_key)))
        if model:
            devices.append(Device(category, model))
    return Hardware(clean(data.get("cpu")), tuple(modules), drives, tuple(devices))


def read_hardware() -> Hardware:
    if sys.platform == "linux":
        return linux_hardware()
    if sys.platform == "win32":
        return windows_hardware()
    return Hardware()
