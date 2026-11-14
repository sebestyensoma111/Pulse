from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pulse.core.clocks import Clock, mhz
from pulse.core.hardware import Hardware, sys_text

if TYPE_CHECKING:
    from pulse.core.temperatures import Temperature


@dataclass(frozen=True, slots=True)
class Gpu:
    name: str
    address: str
    load: float | None = None
    used: float | None = None
    total: float | None = None
    clock: float | None = None
    temperature: float | None = None
    power: float | None = None


def pci_address(address: str) -> str:
    domain, separator, slot = address.lower().partition(":")
    return f"{domain[-4:]}:{slot}" if separator else address


def detected_gpus(hardware: Hardware, measured: tuple[Gpu, ...], clocks: tuple[Clock, ...],
                  temperatures: tuple["Temperature", ...], root: Path = Path("/sys")) -> tuple[Gpu, ...]:
    gpus = {pci_address(gpu.address): gpu for gpu in measured}
    measured_names = [gpu.name.casefold() for gpu in measured]
    for device in hardware.devices:
        if device.category != "GPU" or pci_address(device.identifier) in gpus:
            continue
        name = device.model.casefold()
        if device.identifier.startswith("PCI\\") and name in measured_names:
            measured_names.remove(name)
            continue
        path = root / "bus/pci/devices" / device.identifier
        sensors = sorted((path / "hwmon").glob("hwmon*"))
        power = None
        for sensor in sensors:
            for filename in ("power1_average", "power1_input"):
                power = mhz(sys_text(sensor / filename), 1_000_000)
                if power is not None:
                    break
            if power is not None:
                break
        graphics = [
            clock.mhz for clock in clocks
            if clock.category == "GPU" and clock.channel.startswith("Graphics")
            and device.identifier in clock.source.split(" · ")
        ]
        source_names = {f"{sys_text(sensor / 'name') or 'hwmon'} · {sensor.name}" for sensor in sensors}
        readings = [reading for reading in temperatures if reading.source in source_names]
        edge = next((reading for reading in readings if reading.sensor.casefold() in {"edge", "temp1"}), None)
        temperature = edge or next(iter(readings), None)
        gpus[device.identifier] = Gpu(
            device.model or "GPU", device.identifier,
            mhz(sys_text(path / "gpu_busy_percent")),
            mhz(sys_text(path / "mem_info_vram_used")),
            mhz(sys_text(path / "mem_info_vram_total")),
            max(graphics, default=None), temperature.celsius if temperature else None, power,
        )
    return tuple(gpus.values())
