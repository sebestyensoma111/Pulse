try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
    except ModuleNotFoundError as fallback_error:
        if fallback_error.name != "PyQt5":
            raise
        raise SystemExit("Pulse requires PySide6. See the installation steps in README.md.") from error
    Signal, Slot = QtCore.pyqtSignal, QtCore.pyqtSlot
else:
    Signal, Slot = QtCore.Signal, QtCore.Slot
