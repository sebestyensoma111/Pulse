# Készítette Péter Noé

import time
from collections.abc import Callable
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from sys import platform
from time import monotonic

import psutil

log = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Memory:
    used: int
    total: int
    percent: float
    swap_used: int
    swap_total: int


@dataclass(frozen=True, slots=True)
class Volume:
    mount: str
    filesystem: str
    used: int | None
    total: int | None


@dataclass(frozen=True, slots=True)
class Interface:
    name: str
    received: int
    sent: int


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    name: str
    cpu: float | None
    memory: int | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    cpu: float | None
    cores: tuple[float, ...] | None
    memory: Memory | None
    disk: tuple[float | None, float | None]
    network: tuple[float | None, float | None]
    volumes: tuple[Volume, ...] | None
    interfaces: tuple[Interface, ...] | None
    processes: tuple[Process, ...] | None
    uptime_seconds: int | None = None


# sebesseg a szamlalokbol
class Rates:
    def __init__(self) -> None:
        self.previous: dict[str, tuple[int, int]] = {}
        self.timestamp: float | None = None

    def sample(
        self, counters: dict[str, tuple[int, int]] | None, now: float
    ) -> tuple[float | None, float | None]:
        elapsed = None if self.timestamp is None else now - self.timestamp
        previous = self.previous
        self.previous = counters or {}
        self.timestamp = now if counters is not None else None
        if not elapsed or elapsed < 0 or not counters or not previous:
            return None, None

        common = counters.keys() & previous.keys()
        result = []
        for index in range(2):
            deltas = [counters[key][index] - previous[key][index] for key in common]
            valid = [delta for delta in deltas if delta >= 0]
            result.append(sum(valid) / elapsed if valid else None)
        return result[0], result[1]


def read[T](label: str, collect: Callable[[], T]) -> T | None:
    try:
        return collect()
    except (psutil.Error, OSError, NotImplementedError) as exc:
        log.debug("%s unavailable: %s", label, exc)
        return None


# rendszeradatok lekerese
class Monitor:
    def __init__(self) -> None:
        self.disk_rates = Rates()
        self.network_rates = Rates()
        self.processes: dict[tuple[int, float], psutil.Process] = {}
        self.logical_count = psutil.cpu_count() or 1
        self.first_cpu = True

    def sample(
        self, *, include_processes: bool = True, include_volumes: bool = True,
        include_interfaces: bool = True,
    ) -> Snapshot:
        cpu = read("CPU usage", psutil.cpu_percent)
        cores = read("per-core CPU usage", lambda: tuple(psutil.cpu_percent(percpu=True)))
        if self.first_cpu:
            cpu, cores = None, None
            self.first_cpu = False

        disk_rates = self.disk_rates.sample(read("disk I/O", self.disk_counters), monotonic())
        network = read("network I/O", lambda: psutil.net_io_counters(pernic=True, nowrap=False))
        network_rates = self.network_rates.sample(
            {name: (item.bytes_recv, item.bytes_sent) for name, item in network.items()}
            if network else None,
            monotonic(),
        )
        interfaces = (
            tuple(Interface(name, item.bytes_recv, item.bytes_sent)
                  for name, item in sorted(network.items()))
            if include_interfaces and network is not None else None
        )
        if not include_processes:
            self.processes.clear()
        return Snapshot(
            cpu, cores, read("memory", self.memory), disk_rates, network_rates,
            read("volumes", self.volumes) if include_volumes else None,
            interfaces,
            read("processes", self.process_list) if include_processes else None,
            read("system uptime", self.uptime),
        )

    @staticmethod
    def uptime() -> int:
        if hasattr(time, "CLOCK_BOOTTIME"):
            try:
                return int(time.clock_gettime(time.CLOCK_BOOTTIME))
            except OSError:
                pass
        return max(0, int(time.time() - psutil.boot_time()))

    @staticmethod
    def disk_counters() -> dict[str, tuple[int, int]]:
        disks = psutil.disk_io_counters(perdisk=True, nowrap=False) or {}
        devices = disks.keys()
        if platform == "linux" and Path("/sys/block").is_dir():
            devices = devices & {path.name for path in Path("/sys/block").iterdir()}
        return {
            name: (disks[name].read_bytes, disks[name].write_bytes)
            for name in devices
        }

    @staticmethod
    def memory() -> Memory:
        ram, swap = psutil.virtual_memory(), psutil.swap_memory()
        return Memory(ram.total - ram.available, ram.total, ram.percent, swap.used, swap.total)

    @staticmethod
    def volumes() -> tuple[Volume, ...]:
        volumes = []
        for item in psutil.disk_partitions():
            usage = read(item.mountpoint, lambda: psutil.disk_usage(item.mountpoint))
            volumes.append(Volume(item.mountpoint, item.fstype,
                                  usage.used if usage else None, usage.total if usage else None))
        return tuple(volumes)

    def process_list(self) -> tuple[Process, ...]:
        active = {}
        rows = []
        for pid in psutil.pids():
            try:
                proc = psutil.Process(pid)
                key = (pid, proc.create_time())
                previous = self.processes.get(key)
                tracked = previous if previous is not None else proc
                active[key] = tracked
                with tracked.oneshot():
                    name = read("process name", tracked.name)
                    memory = read("process memory", tracked.memory_info)
                    cpu = read("process CPU", tracked.cpu_percent)
                rows.append(Process(
                    tracked.pid, name or "—",
                    min(100.0, cpu / self.logical_count) if previous is not None and cpu is not None else None,
                    memory.rss if memory is not None else None,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.processes = active
        return tuple(rows)
