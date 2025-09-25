import sys, os, re, datetime
import serial
import serial.tools.list_ports
import threading

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QCheckBox, QTextEdit, QSplitter, QFrame,
    QFileDialog, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QTextCursor

ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

ANSI_COLOR = {
    30: "#000000", 31: "#ff4d4d", 32: "#00ff00", 33: "#ffff00",
    34: "#4da3ff", 35: "#ff66ff", 36: "#00ffff", 37: "#e0e0e0",
    90: "#808080", 91: "#ff8080", 92: "#80ff80", 93: "#ffff80",
    94: "#a0c8ff", 95: "#ff9cff", 96: "#a0ffff", 97: "#ffffff"
}

LOG_COLORS = {
    'error': "#FF0000",
    'warning': "#FFFF00",
    'info': "#00FF00",
    'debug': "#00FFFF",
    'verbose': "#808080",
    'default': "#e0e0e0"
}

def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

def classify_line(s: str) -> str:
    t = s.strip()
    # ESP-IDF log detection
    m = re.match(r'^([IWEVD])\s*\(\d+\)', t)
    if m:
        level = m.group(1)
        return {
            'I': 'info',
            'W': 'warning',
            'E': 'error',
            'D': 'debug',
            'V': 'verbose'
        }.get(level, 'debug')
    # ANSI fallback
    if "\x1b[" in t:
        return 'ansi'
    t_lower = t.lower()
    if any(x in t_lower for x in ("error","fatal","fail","exception","assert")):
        return 'error'
    if any(x in t_lower for x in ("warn","warning")):
        return 'warning'
    if any(x in t_lower for x in ("info",)):
        return 'info'
    if any(x in t_lower for x in ("debug","dbg")):
        return 'debug'
    if any(x in t_lower for x in ("verb","trace")):
        return 'verbose'
    return 'debug'

def ansi_to_html(s: str) -> str:
    out = []
    last = 0
    open_span = False
    current_style = {}

    def style_to_html(style_dict):
        parts = []
        if 'color' in style_dict:
            parts.append(f"color:{style_dict['color']}")
        if style_dict.get('bold'):
            parts.append("font-weight:600")
        return ";".join(parts)

    for m in ANSI_RE.finditer(s):
        if m.start() > last:
            chunk = html_escape(s[last:m.start()])
            out.append(chunk)
        codes = m.group(1)
        last = m.end()
        if codes == "" or codes == "0":
            if open_span:
                out.append("</span>")
                open_span = False
            current_style.clear()
            continue
        for c in codes.split(";"):
            if not c:
                continue
            try:
                n = int(c)
            except ValueError:
                continue
            if n == 0:
                if open_span:
                    out.append("</span>")
                    open_span = False
                current_style.clear()
            elif n == 1:
                current_style['bold'] = True
            elif 30 <= n <= 37 or 90 <= n <= 97:
                current_style['color'] = ANSI_COLOR.get(n, LOG_COLORS['default'])
        if open_span:
            out.append("</span>")
            open_span = False
        st = style_to_html(current_style)
        if st:
            out.append(f'<span style="{st}">')
            open_span = True
    if last < len(s):
        out.append(html_escape(s[last:]))
    if open_span:
        out.append("</span>")
    return "".join(out)

class SerialMonitor(QMainWindow):
    append_log = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.serial_connection = None
        self.running = False
        self.read_thread = None
        self.debug_buffer = []
        self.esp_buffer = []

        self.setWindowTitle("ESP32 Debug & Logs Monitor")
        self.setGeometry(100, 100, 1100, 680)
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #121212; color: #e0e0e0; font-family: 'Segoe UI'; font-size: 14px; }
            QHeaderView::section { background-color: #1e1e1e; color: #ffffff; }
            QPushButton { background-color: #333; color: #e0e0e0; border: none; border-radius: 8px; padding: 6px 12px; }
            QPushButton:hover { background-color: #444; }
            QComboBox, QCheckBox { background-color: #333; padding: 4px 8px; border-radius: 6px; }
            QTextEdit { background-color: #1c1c1c; border-radius: 10px; padding: 10px; color: #e0e0e0; }
            QFrame#panel { background-color: #1c1c1c; border-radius: 12px; }
            QLabel#panelTitle { font-size:16px; color:white; border-bottom: 2px solid #333; padding: 6px 2px; }
        """)

        self.append_log.connect(self.on_append_log)
        self._setup_ui()

    def _setup_ui(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        save_action = QAction("Save Logs…", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save_logs)
        file_menu.addAction(save_action)

        tools_menu = menubar.addMenu("&Tools")
        refresh_action = QAction("Refresh Ports", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.refresh_ports)
        tools_menu.addAction(refresh_action)
        clear_action = QAction("Clear Logs", self)
        clear_action.setShortcut("Ctrl+L")
        clear_action.triggered.connect(self.clear_logs)
        tools_menu.addAction(clear_action)

        help_menu = menubar.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.setShortcut("F1")
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(10)
        controls_layout.addWidget(QLabel("Port:"))
        self.port_combo = QComboBox()
        self.refresh_ports()
        controls_layout.addWidget(self.port_combo)

        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setToolTip("Refresh connected USB devices")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        controls_layout.addWidget(self.refresh_btn)

        controls_layout.addWidget(QLabel("Baud Rate:"))
        self.baud_combo = QComboBox()
        for b in ["300","600","1200","2400","4800","9600","14400","19200","38400","57600",
                  "115200","230400","250000","500000","1000000","2000000","3000000","4000000"]:
            self.baud_combo.addItem(b)
        self.baud_combo.setCurrentText("115200")
        controls_layout.addWidget(self.baud_combo)

        self.start_stop_btn = QPushButton("Start")
        self.start_stop_btn.clicked.connect(self.toggle_monitoring)
        controls_layout.addWidget(self.start_stop_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear both terminals")
        self.clear_btn.clicked.connect(self.clear_logs)
        controls_layout.addWidget(self.clear_btn)

        self.autoscroll_chk = QCheckBox("Autoscroll")
        self.autoscroll_chk.setChecked(True)
        controls_layout.addWidget(self.autoscroll_chk)

        self.timestamp_chk = QCheckBox("Timestamp")
        self.timestamp_chk.setChecked(True)
        controls_layout.addWidget(self.timestamp_chk)
        controls_layout.addStretch()

        self.debug_terminal = QTextEdit()
        self.debug_terminal.setReadOnly(True)
        self.debug_terminal.setAcceptRichText(True)
        self.esp_terminal = QTextEdit()
        self.esp_terminal.setReadOnly(True)
        self.esp_terminal.setAcceptRichText(True)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._wrap_terminal("Debug", self.debug_terminal))
        splitter.addWidget(self._wrap_terminal("ESP Logs", self.esp_terminal))
        splitter.setSizes([1,1])

        footer = QLabel()
        footer.setTextFormat(Qt.TextFormat.RichText)
        footer.setOpenExternalLinks(True)
        footer.setText(
            'Made with <span style="color: red; font-size:13px;">♥️</span> by '
            '<a href="https://github.com/developer-srj/" style="color: #9e9e9e; text-decoration: underline dashed;">developer_SRJ</a>'
        )
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_container = QFrame()
        f_layout = QHBoxLayout(footer_container)
        f_layout.setContentsMargins(0,6,0,8)
        f_layout.addWidget(footer)

        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10,10,10,10)
        main_layout.addLayout(controls_layout)
        main_layout.addWidget(splitter,1)
        main_layout.addWidget(footer_container,0)
        self.setCentralWidget(central)

    def _wrap_terminal(self, title, widget):
        frame = QFrame()
        frame.setObjectName("panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12,10,12,12)
        label = QLabel(title)
        label.setObjectName("panelTitle")
        layout.addWidget(label)
        layout.addWidget(widget)
        return frame

    def refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        for p in serial.tools.list_ports.comports():
            self.port_combo.addItem(p.device)
        if current:
            idx = self.port_combo.findText(current)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

    def toggle_monitoring(self):
        if not self.running:
            port = self.port_combo.currentText()
            if not port:
                QMessageBox.warning(self, "No Port", "No serial port selected.")
                return
            baud = int(self.baud_combo.currentText())
            try:
                self.serial_connection = serial.Serial(port, baud, timeout=1)
                self.running = True
                self.start_stop_btn.setText("Stop")
                self.read_thread = threading.Thread(target=self.read_serial, daemon=True)
                self.read_thread.start()
            except Exception as e:
                QMessageBox.critical(self, "Serial Error", str(e))
        else:
            self.stop_serial()

    def stop_serial(self):
        self.running = False
        try:
            if self.serial_connection and self.serial_connection.is_open:
                self.serial_connection.close()
        except Exception:
            pass
        self.start_stop_btn.setText("Start")

    def read_serial(self):
        while self.running and self.serial_connection:
            try:
                raw = self.serial_connection.readline().decode(errors="ignore").rstrip("\r\n")
                if not raw:
                    continue
                if re.match(r'^[IWEVD]\s*\(\d+\)', raw) or re.match(r'^[IWEVD]/', raw) or "\x1b[" in raw:
                    target = "esp"
                else:
                    target = "debug"
                self.append_log.emit(target, raw)
            except Exception as e:
                self.append_log.emit("debug", f"Serial Error: {e}")
                break

    def on_append_log(self, target, raw_line):
        ts = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] " if self.timestamp_chk.isChecked() else ""
        kind = classify_line(raw_line)
        if kind == 'ansi':
            html_body = ansi_to_html(raw_line)
        else:
            color = LOG_COLORS.get(kind, LOG_COLORS['default'])
            html_body = f'<span style="color:{color}">{html_escape(raw_line)}</span>'
        html = f"{html_escape(ts)}{html_body}"

        if target == "esp":
            self._append_html(self.esp_terminal, html)
            self.esp_buffer.append(ts + raw_line)
        else:
            self._append_html(self.debug_terminal, html)
            self.debug_buffer.append(ts + raw_line)

    def _append_html(self, edit: QTextEdit, html: str):
        cur = edit.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertHtml(html + "<br>")
        edit.setTextCursor(cur)
        if self.autoscroll_chk.isChecked():
            edit.ensureCursorVisible()

    def save_logs(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder to save logs")
        if not folder:
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = os.path.join(folder, f"debug_{ts}.log")
        esp_path = os.path.join(folder, f"esp_{ts}.log")
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.debug_buffer))
            with open(esp_path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.esp_buffer))
            QMessageBox.information(self, "Saved", f"Saved:\n{debug_path}\n{esp_path}")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def clear_logs(self):
        self.debug_terminal.clear()
        self.esp_terminal.clear()
        self.debug_buffer.clear()
        self.esp_buffer.clear()

    def show_about(self):
        QMessageBox.about(self, "About ESP32 Serial Monitor",
            ("<b>ESP32 Debug & Logs Monitor</b><br>"
             "Native Python (PyQt6 + pyserial) tool with dark UI, log coloring, and dual panes.<br><br>"
             'Made with <span style="color:red;">♥️</span> by '
             '<a href="https://github.com/developer-srj/">developer_SRJ</a>'))

    def closeEvent(self, event):
        self.stop_serial()
        return super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SerialMonitor()
    win.show()
    sys.exit(app.exec())
