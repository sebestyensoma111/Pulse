# Pulse
<!-- made by Péter Noé -->

[Magyar](README.hu.md) · English

Pulse is a desktop system monitor for Windows and Linux. The dashboard shows CPU and RAM usage, disk activity, and network traffic, with charts covering the last minute.

## Run from source

Use Python 3.12 or newer. Run these commands from the project folder.

**Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pulse
```

**Windows**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pulse
```

### Linux with system PyQt5

If PyQt5 and psutil are already installed for your system Python, you can run Pulse without downloading PySide6.

```bash
python3 -m venv --system-site-packages .venv
PYTHONPATH=src .venv/bin/python -m pulse
```

## What you can see

| View | Details |
| --- | --- |
| **CPU** | Processor model, load per logical CPU, and a process list you can sort or filter by name or PID. Process CPU usage is shown as a share of total CPU capacity. |
| **Memory** | RAM and swap usage, process memory (RSS), and available module details including model, DDR type, capacity, and transfer rate. |
| **Storage** | Read and write speeds, drive models and capacities in GB or TB, and space usage on mounted volumes. |
| **Network** | Download and upload speeds, plus the total bytes sent and received per interface. |
| **Hardware** | Detected devices such as the GPU, motherboard, BIOS, and network adapters. The **Temperatures** tab labels readings by device and sensor. |
| **Clocks** | CPU and GPU frequencies in MHz. RAM transfer rates in MT/s come from firmware and may be configured or rated values. |

## Reading availability

- Disk and network rates need two samples. A dash or **Unavailable** means the app has no reading for that field.
- On Linux, Pulse uses `lsblk` for drive details and `dmidecode` for RAM modules. The system may restrict access to module details. CAS latency (CL) is unavailable.
- Linux CPU frequencies are shown per logical CPU where available. Windows may report a nominal CPU frequency. GPU readings depend on the driver, and NVIDIA readings require `nvidia-smi`.
