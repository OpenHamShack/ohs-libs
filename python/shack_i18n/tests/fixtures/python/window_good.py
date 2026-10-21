"""The same window done properly: nothing here is a finding."""

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QLabel, QMainWindow


class Window(QMainWindow):
    def __init__(self, station, n):
        super().__init__()
        self.setWindowTitle(self.tr("Radio"))
        self.status = QLabel(self.tr("Transmit blocked"))
        self.status.setText(self.tr("%n stations heard", "", n))
        self.status.setText(QCoreApplication.translate("Window", "Ready"))
        self.status.setText(station)  # data, not a literal
        self.status.setText("—")  # no run of two letters
        self.status.setText("0.0")
        self.status.setText("%s")
        self.status.setText("icons/tx.png")  # a path
        self.status.setText("https://openhamshack.org/Help?page=1")  # a URL
        self.setObjectName("statusLabel")  # not a sink
        print("Transmit blocked")  # not a sink: log and console text are machine-facing
        label = {"title": "Radio"}  # a dict key and value, not a sink


def describe(n):
    # A docstring-like string outside any sink is never looked at.
    return "Transmit blocked"
