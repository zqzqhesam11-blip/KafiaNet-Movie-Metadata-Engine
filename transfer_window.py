from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QProgressBar,
    QPushButton,
    QFrame
)

from PySide6.QtCore import Qt


class TransferWindow(QWidget):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("KAFIA NET TRANSFER")
        self.resize(760, 520)

        self.setMinimumSize(680, 460)

        self.setStyleSheet("""
            QWidget {
                background-color: #10141b;
                color: #f2f4f7;
                font-family: Segoe UI;
            }

            QFrame#card {
                background-color: #181e27;
                border: 1px solid #293241;
                border-radius: 16px;
            }

            QLabel#title {
                font-size: 24px;
                font-weight: bold;
            }

            QLabel#status {
                font-size: 16px;
            }

            QLabel#percent {
                font-size: 34px;
                font-weight: bold;
            }

            QLabel#info {
                font-size: 14px;
                color: #b8c0cc;
            }

            QProgressBar {
                background-color: #252c37;
                border: none;
                border-radius: 8px;
                height: 18px;
                text-align: center;
            }

            QProgressBar::chunk {
                background-color: #20c997;
                border-radius: 8px;
            }

            QTextEdit {
                background-color: #0b0f14;
                border: 1px solid #293241;
                border-radius: 10px;
                padding: 8px;
                color: #d9dee7;
                font-family: Consolas;
                font-size: 12px;
            }

            QPushButton {
                background-color: #252c37;
                border: 1px solid #3a4352;
                border-radius: 10px;
                padding: 10px 24px;
                font-size: 14px;
            }

            QPushButton:hover {
                background-color: #303948;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        header = QFrame()
        header.setObjectName("card")

        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 16)

        self.title = QLabel(" KAFIA NET  نقل الألعاب")
        self.title.setObjectName("title")
        self.title.setAlignment(Qt.AlignCenter)

        self.status = QLabel("الحالة: جاهز")
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignCenter)

        header_layout.addWidget(self.title)
        header_layout.addWidget(self.status)

        root.addWidget(header)

        progress_card = QFrame()
        progress_card.setObjectName("card")

        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(20, 20, 20, 20)
        progress_layout.setSpacing(12)

        self.percent = QLabel("0%")
        self.percent.setObjectName("percent")
        self.percent.setAlignment(Qt.AlignCenter)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)

        info_layout = QHBoxLayout()

        self.speed = QLabel("السرعة: --")
        self.speed.setObjectName("info")

        self.eta = QLabel("الوقت المتبقي: --")
        self.eta.setObjectName("info")
        self.eta.setAlignment(Qt.AlignRight)

        info_layout.addWidget(self.speed)
        info_layout.addStretch()
        info_layout.addWidget(self.eta)

        progress_layout.addWidget(self.percent)
        progress_layout.addWidget(self.progress)
        progress_layout.addLayout(info_layout)

        root.addWidget(progress_card)

        log_card = QFrame()
        log_card.setObjectName("card")

        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(12, 12, 12, 12)

        log_title = QLabel("سجل النقل")
        log_title.setObjectName("info")

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        log_layout.addWidget(log_title)
        log_layout.addWidget(self.log)

        root.addWidget(log_card, 1)

        buttons = QHBoxLayout()

        self.close_btn = QPushButton("إغلاق")
        self.close_btn.clicked.connect(self.close)

        buttons.addStretch()
        buttons.addWidget(self.close_btn)

        root.addLayout(buttons)

    def set_status(self, text):
        self.status.setText(
            "الحالة: " + str(text)
        )

    def add_log(self, text):
        text = str(text)

        self.log.append(text)

        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_progress(self, value):
        try:
            value = int(value)
        except Exception:
            value = 0

        value = max(0, min(100, value))

        self.progress.setValue(value)
        self.percent.setText(f"{value}%")

    def set_speed(self, speed):
        self.speed.setText(
            "السرعة: " + str(speed)
        )

    def set_eta(self, eta):
        self.eta.setText(
            "الوقت المتبقي: " + str(eta)
        )

    def update_transfer(self, percent, speed, eta):
        self.set_progress(percent)
        self.set_speed(speed)
        self.set_eta(eta)

    def reset(self):
        self.set_progress(0)
        self.set_speed("--")
        self.set_eta("--")
        self.set_status("جاهز")
        self.log.clear()
