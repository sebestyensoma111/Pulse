import json
import subprocess
import unittest
from unittest.mock import patch

from pulse.core.hardware import Drive
from pulse.core.smart import linux_smart, parse_smartctl


class SmartTests(unittest.TestCase):
    def test_ata_totals_use_the_reported_logical_sector_size(self):
        data = {"logical_block_size": 4096, "ata_device_statistics": {"pages": [
            {"table": [{"name": "Logical Sectors Written", "value": 250_000_000},
                       {"name": "Logical Sectors Read", "value": 0}]},
        ]}}
        rows = dict(parse_smartctl(data, 0).rows)
        self.assertEqual(rows["Total data written"], "1.024 TB")
        self.assertEqual(rows["Total data read"], "0.000 TB")
        del data["logical_block_size"]
        self.assertNotIn("Total data written", dict(parse_smartctl(data, 0).rows))

    def test_ata_mechanical_cycles_are_distinct_from_power_cycles(self):
        report = parse_smartctl({
            "power_cycle_count": 22,
            "ata_smart_attributes": {"table": [
                {"id": 4, "name": "Start_Stop_Count", "raw": {"value": 200}},
                {"id": 193, "name": "Load_Cycle_Count", "raw": {"value": 500}},
                {"id": 192, "name": "Vendor_Specific", "raw": {"value": 9}},
            ]},
        }, 0)
        rows = dict(report.rows)
        self.assertEqual(rows["Power cycles"], "22")
        self.assertEqual(rows["Start/stop cycles"], "200")
        self.assertEqual(rows["Load/unload cycles"], "500")
        self.assertNotIn("Power-off retracts", rows)

    def test_scsi_cycles(self):
        report = parse_smartctl({"scsi_start_stop_cycle_counter": {
            "accumulated_start_stop_cycles": 0, "accumulated_load_unload_cycles": 120,
        }}, 0)
        self.assertEqual(dict(report.rows), {"Start/stop cycles": "0", "Load/unload cycles": "120"})

    def test_drive_failure_keeps_available_counters(self):
        output = json.dumps({
            "smart_status": {"passed": False},
            "nvme_smart_health_information_log": {
                "critical_warning": 4,
                "media_errors": 0,
                "data_units_written": 1_953_125,
            },
        })
        with (
            patch("pulse.core.smart.shutil.which", return_value="/usr/sbin/smartctl"),
            patch("pulse.core.smart.Path.exists", return_value=True),
            patch("pulse.core.smart.subprocess.run",
                  return_value=subprocess.CompletedProcess([], 8, output)),
        ):
            report = linux_smart(Drive("nvme0n1", "Example SSD", "NVMe", None))
        self.assertEqual(report.status, "SMART failure reported")
        rows = dict(report.rows)
        self.assertEqual(rows["Total data written"], "1.000 TB")
        self.assertEqual(rows["Media and data integrity errors"], "0")
        self.assertIn("Reliability degraded", rows["NVMe critical warning"])

    def test_pending_ata_sectors_show_a_warning(self):
        report = parse_smartctl({
            "smart_status": {"passed": True},
            "ata_smart_attributes": {"table": [{"id": 197, "raw": {"value": 2}}]},
        }, 0)
        self.assertEqual(report.status, "SMART warnings or recorded errors")
        self.assertEqual(dict(report.rows)["Pending sectors"], "2")

    def test_open_error_does_not_report_a_healthy_drive(self):
        report = parse_smartctl({
            "smartctl": {"messages": [{"severity": "error", "string": "Permission denied"}]},
        }, 2)
        self.assertEqual(report.status, "SMART status unavailable")
        self.assertEqual(report.note, "Permission denied")
