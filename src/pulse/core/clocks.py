import math
import re
from dataclasses import dataclass
from pathlib import Path

from pulse.core.hardware import Hardware, sys_path, sys_text


@dataclass(frozen=True, slots=True)
class Clock:
    device: str
    channel: str
    mhz: float
    source: str
    category: str = "CPU"


def mhz(raw: str, scale: float = 1) -> float | None:
    try:
        value = float(raw) / scale
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def linux_clocks(hardware: Hardware, root: Path = Path("/sys")) -> tuple[Clock, ...]:
    clocks = []
    for cpu in sorted((root / "devices/system/cpu").glob("cpu[0-9]*"),
                      key=lambda path: int(path.name[3:])):
        frequency = cpu / "cpufreq"
        for filename, source in (
            ("cpuinfo_cur_freq", "Hardware reading"),
            ("scaling_cur_freq", "OS reported"),
        ):
            value = mhz(sys_text(frequency / filename), 1000)
            if value is not None and value > 0:
                clocks.append(Clock(hardware.cpu_model or "CPU", f"Logical CPU {cpu.name[3:]}", value, source))
                break

    models = {device.identifier: device.model for device in hardware.devices if device.category == "GPU"}
    for card in sorted((root / "class/drm").glob("card*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = sys_path(card / "device")
        address = device.name
        model = models.get(address) or card.name
        source = f"{card.name} · {address}"
        tiles = sorted((card / "gt").glob("gt[0-9]*"))
        reported = False
        for tile in tiles:
            value = mhz(sys_text(tile / "rps_act_freq_mhz"))
            if value is not None:
                clocks.append(Clock(model, f"Graphics · {tile.name}", value, source, "GPU"))
                reported = True
        if not reported:
            value = mhz(sys_text(card / "gt_act_freq_mhz"))
            if value is not None:
                clocks.append(Clock(model, "Graphics", value, source, "GPU"))

        for tile in sorted(device.glob("tile*/gt*")):
            value = mhz(sys_text(tile / "freq0/act_freq"))
            if value is not None:
                clocks.append(Clock(model, f"Graphics · {tile.parent.name}/{tile.name}", value, source, "GPU"))

        for filename, channel in (("pp_dpm_sclk", "Graphics"), ("pp_dpm_mclk", "VRAM")):
            match = re.search(r"(?mi)^\s*\d+:\s*(\d+(?:\.\d+)?)\s*MHz\s*\*\s*$",
                              sys_text(device / filename))
            if match:
                clocks.append(Clock(model, channel, float(match[1]), f"{source} · active DPM state", "GPU"))
    return tuple(clocks)
