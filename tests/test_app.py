import unittest
from time import monotonic, sleep

from pulse.ui.main_window import HARDWARE_PAGE, MainWindow
from pulse.ui.qt import QtWidgets


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait_for(self, condition, message, timeout=15):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            sleep(0.02)
        self.fail(message)

    def test_live_dashboard_navigation_and_shutdown(self):
        window = MainWindow()
        window.show()
        try:
            self.wait_for(
                lambda: window.snapshot is not None and window.snapshot.uptime_seconds is not None,
                "System uptime did not load",
            )
            first_uptime = window.snapshot.uptime_seconds
            self.wait_for(
                lambda: window.snapshot.uptime_seconds is not None
                and window.snapshot.uptime_seconds > first_uptime,
                "System uptime did not advance",
            )
            self.assertRegex(window.uptime_label.text(), r"^Uptime · (?:\d+d )?\d{2}:\d{2}:\d{2}$")

            window.navigate(1)
            self.wait_for(
                lambda: window.tables[0].model().rowCount() > 0,
                "The CPU process list did not load",
            )

            window.open_clocks()
            self.assertEqual(window.stack.currentIndex(), HARDWARE_PAGE)
            self.assertIs(window.hardware_tabs.currentWidget(), window.clock_table.parentWidget())
            self.wait_for(
                lambda: window.latest_clocks is not None and window.clock_table.model().rowCount() > 0,
                "The clock table did not load",
            )
        finally:
            window.close()
            self.wait_for(
                lambda: not window.isVisible() and not any(worker.isRunning() for worker in window.workers),
                "Pulse did not shut down cleanly",
            )
