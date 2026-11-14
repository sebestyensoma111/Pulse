import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pulse.core.hardware import Hardware, linux_drives, linux_hardware
from pulse.core.temperatures import LinuxThermals


class HardwareTests(unittest.TestCase):
    def test_drive_scan_distinguishes_missing_data_from_no_drives(self):
        with patch("pulse.core.hardware.command", side_effect=[
            '{"blockdevices":null}', '{"blockdevices":[]}',
            '{"blockdevices":[null,{},{"name":"sda","type":"disk","size":4096}]}',
        ]):
            self.assertIsNone(linux_drives())
            self.assertEqual(linux_drives(), ())
            drive, = linux_drives()
            self.assertEqual((drive.name, drive.capacity), ("sda", 4096))

    def test_dmi_permission_error_keeps_the_hardware_inventory(self):
        with (
            patch("pulse.core.hardware.run_command", return_value=subprocess.CompletedProcess(
                [], 1, "", "Permission denied",
            )),
            patch("pulse.core.hardware.linux_drives", return_value=()),
            patch("pulse.core.hardware.linux_devices", return_value=()),
        ):
            hardware = linux_hardware()
        self.assertEqual(hardware.memory, ())
        self.assertIn("permission denied", hardware.memory_note)

    def test_cached_sensor_disappears_and_new_sensor_is_discovered(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            sensor = root / "class/hwmon/hwmon0"
            sensor.mkdir(parents=True)
            sample = sensor / "temp1_input"
            sample.write_text("41000")
            hardware = Hardware()
            thermals = LinuxThermals(root)
            with patch("pulse.core.temperatures.monotonic", side_effect=[0, 1, 2, 16]):
                self.assertEqual(thermals.read(hardware)[0].celsius, 41)
                sample.write_text("43000")
                self.assertEqual(thermals.read(hardware)[0].celsius, 43)
                sample.unlink()
                self.assertEqual(thermals.read(hardware), ())
                (sensor / "temp2_input").write_text("44000")
                self.assertEqual(thermals.read(hardware)[0].celsius, 44)
