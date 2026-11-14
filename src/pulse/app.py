# Készítette Péter Noé

from pulse.ui.main_window import MainWindow
from pulse.ui.qt import QtWidgets


def main() -> int:
    app = QtWidgets.QApplication([])
    app.setApplicationName("Pulse")
    window = MainWindow()
    window.show()
    return app.exec()
