"""A Qt window that puts English straight on the screen."""

from PyQt6.QtWidgets import QLabel, QMainWindow, QMessageBox, QPushButton


class Window(QMainWindow):
    def __init__(self, station):
        super().__init__()
        self.setWindowTitle("Radio")  # expect-finding
        self.status = QLabel()
        self.status.setText("Transmit blocked")  # expect-finding
        self.go = QPushButton("Tune")  # expect-finding
        self.go.setToolTip(f"Tune {station} now")  # expect-finding
        self.status.setText("Heard " + station)  # expect-finding
        self.mode.addItems(["Upper sideband", "usb"])  # expect-finding
        self.status.setText("Busy" if station else "Idle")  # expect-finding (two literals, one line)
        self.status.setText("%d stations heard" % 3)  # expect-finding
        self.status.setText("{} is on air".format(station))  # expect-finding
        self.status.setText(self.tr("Ready") + " and waiting")  # expect-finding (the fragment, not the catalog call)

    def warn(self):
        QMessageBox.warning(self, "Careful", "The PA is hot.")  # expect-finding
        self.statusBar().showMessage("Ünïcödé text")  # expect-finding


def build_menu(menu):
    menu.addAction("Quit")  # expect-finding
