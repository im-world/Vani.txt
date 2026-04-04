import sys
import os
import threading
import re

# Important: To bypass FileNotFoundError for FFmpeg, we inject imageio-ffmpeg's path into os.environ["PATH"]
# This guarantees whisper can find the ffmpeg binary when bundled or run fresh, since we fetch it automatically.
try:
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["PATH"] = os.path.dirname(ffmpeg_exe) + os.pathsep + os.environ["PATH"]
except ImportError:
    pass

import whisper
import torch
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QTextEdit, QFileDialog, 
                               QLabel, QProgressBar, QMessageBox, QComboBox,
                               QGraphicsOpacityEffect, QSizePolicy, QSpacerItem)
from PySide6.QtCore import QThread, Signal, Qt, QSize
from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor

class StreamInterceptor:
    def __init__(self, original_stdout, progress_signal, text_signal, total_duration):
        self.original_stdout = original_stdout
        self.progress_signal = progress_signal
        self.text_signal = text_signal
        self.total_duration = total_duration
        self.regex = re.compile(r"\[([\d:.]+) --> ([\d:.]+)\]\s*(.*)")
        self.buffer = ""

    def write(self, text):
        if self.original_stdout:
            try:
                self.original_stdout.write(text)  # Keep original output for terminal
            except AttributeError:
                pass
        self.buffer += text
        if '\n' in self.buffer:
            lines = self.buffer.split('\n')
            for line in lines[:-1]:
                self.process_line(line)
            self.buffer = lines[-1]

    def process_line(self, line):
        match = self.regex.search(line)
        if match:
            _, end_str, segment_text = match.groups()
            parts = end_str.split(':')
            s = 0
            if len(parts) == 3:
                s = int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
            elif len(parts) == 2:
                s = int(parts[0])*60 + float(parts[1])
            
            if self.total_duration > 0:
                percent = min(100, int((s / self.total_duration) * 100))
                self.progress_signal.emit(percent)
            
            if segment_text.strip():
                self.text_signal.emit(segment_text.strip() + " ")

    def flush(self):
        if self.original_stdout:
            try:
                self.original_stdout.flush()
            except AttributeError:
                pass

class TranscriptionThread(QThread):
    finished = Signal(str)
    error = Signal(str)
    progress = Signal(str)
    progress_percent = Signal(int)
    text_segment = Signal(str)

    def __init__(self, audio_path, model_name="base", language=None):
        super().__init__()
        self.audio_path = audio_path
        self.model_name = model_name
        self.language = language

    def run(self):
        try:
            self.progress.emit("Detecting hardware acceleration (CPU/GPU)...")
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.progress.emit(f"Loading '{self.model_name}' multilingual model on {device.upper()}...")
            
            # The models 'base', 'small', 'medium', 'large' are inherently multilingual.
            # 'base.en' would be english only.
            model = whisper.load_model(self.model_name, device=device)
            
            self.progress.emit("Reading audio to calculate duration...")
            audio = whisper.load_audio(self.audio_path)
            duration = audio.shape[0] / whisper.audio.SAMPLE_RATE
            
            self.progress.emit("Transcribing audio... (Streaming text live)")
            
            original_stdout = sys.stdout
            interceptor = StreamInterceptor(original_stdout, self.progress_percent, self.text_segment, duration)
            sys.stdout = interceptor
            
            try:
                transcribe_kwargs = {"verbose": True}
                if self.language:
                    transcribe_kwargs["language"] = self.language
                result = model.transcribe(self.audio_path, **transcribe_kwargs)
            finally:
                sys.stdout = original_stdout
            
            self.finished.emit(result["text"])
        except Exception as e:
            if 'original_stdout' in locals():
                sys.stdout = original_stdout
            self.error.emit(str(e))


class WatermarkWidget(QWidget):
    """Custom central widget that paints the logo as a subtle watermark behind the text editor only."""
    def __init__(self, logo_path, parent=None):
        super().__init__(parent)
        self.watermark = None
        self._editor_ref = None  # will be set after init_ui
        if os.path.exists(logo_path):
            self.watermark = QPixmap(logo_path)
    
    def set_editor(self, editor_widget):
        self._editor_ref = editor_widget
    
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.watermark and self._editor_ref:
            painter = QPainter(self)
            painter.setOpacity(0.05)
            # Paint only within the text editor's geometry
            r = self._editor_ref.geometry()
            wm_size = min(r.width(), r.height()) - 40
            if wm_size > 40:
                scaled = self.watermark.scaled(wm_size, wm_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                x = r.x() + (r.width() - scaled.width()) // 2
                y = r.y() + (r.height() - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
            painter.end()


class AppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("vani.txt")
        
        # Load the custom logo as window icon
        self.logo_path = os.path.join(os.path.dirname(__file__), "vani_logo.png")
        if os.path.exists(self.logo_path):
            self.setWindowIcon(QIcon(self.logo_path))
            
        self.resize(780, 620)
        self.setMinimumSize(600, 500)
        self.audio_path = None
        self.thread = None
        
        self.apply_styles()
        self.init_ui()

    def init_ui(self):
        # Use our watermark widget as the central widget
        central_widget = WatermarkWidget(self.logo_path)
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(40, 28, 40, 16)
        layout.setSpacing(0)

        # ── Header ──
        header_layout = QHBoxLayout()
        header_layout.setAlignment(Qt.AlignCenter)
        header_layout.setSpacing(10)

        # Small inline logo
        logo_path = os.path.join(os.path.dirname(__file__), "vani_logo.png")
        if os.path.exists(logo_path):
            logo_icon = QLabel()
            pixmap = QPixmap(logo_path).scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_icon.setPixmap(pixmap)
            logo_icon.setStyleSheet("background: transparent;")
            header_layout.addWidget(logo_icon)

        title_label = QLabel("vani.txt")
        title_label.setFont(QFont("Segoe UI", 22, QFont.Bold))
        title_label.setStyleSheet("color: #ffffff; background: transparent; margin: 0; padding: 0;")
        header_layout.addWidget(title_label)
        layout.addLayout(header_layout)

        sub_label = QLabel("Local · Private · Offline Speech-to-Text")
        sub_label.setAlignment(Qt.AlignCenter)
        sub_label.setStyleSheet("color: #4ade80; font-size: 13px; font-weight: 600; background: transparent; margin: 0 0 12px 0;")
        layout.addWidget(sub_label)

        # ── Settings Row (Model + Language) ──
        settings_frame = QWidget()
        settings_frame.setObjectName("settingsFrame")
        settings_frame.setStyleSheet("""
            #settingsFrame {
                background-color: rgba(15, 15, 30, 0.6);
                border: 1px solid #2a2a48;
                border-radius: 10px;
                padding: 10px;
            }
        """)
        settings_layout = QHBoxLayout(settings_frame)
        settings_layout.setContentsMargins(16, 10, 16, 4)
        settings_layout.setSpacing(14)

        model_label = QLabel("Model")
        model_label.setStyleSheet("color: #8888aa; font-size: 12px; font-weight: bold; background: transparent; border: none;")
        self.model_combo = QComboBox()
        self.model_combo.addItems(["Tiny", "Base", "Small", "Medium", "Large"])
        self.model_combo.setCurrentText("Base")
        self.model_combo.currentTextChanged.connect(self.update_model_description)

        lang_label = QLabel("Language")
        lang_label.setStyleSheet("color: #8888aa; font-size: 12px; font-weight: bold; background: transparent; border: none;")
        self.lang_combo = QComboBox()
        languages = ["Bengali", "English", "French", "German", "Gujarati",
                     "Hindi", "Kannada", "Malayalam", "Marathi", "Spanish",
                     "Tamil", "Telugu", "Urdu"]
        self.lang_combo.addItems(["Auto-Detect"] + languages)
        self.lang_combo.setCurrentText("Auto-Detect")

        settings_layout.addWidget(model_label)
        settings_layout.addWidget(self.model_combo, 1)
        settings_layout.addSpacing(10)
        settings_layout.addWidget(lang_label)
        settings_layout.addWidget(self.lang_combo, 1)
        layout.addWidget(settings_frame)

        # Model description hint
        self.model_desc_label = QLabel("Balanced speed and accuracy. Good for most use cases.")
        self.model_desc_label.setStyleSheet("color: #666680; font-size: 11px; font-style: italic; background: transparent; margin: 2px 0 10px 4px;")
        layout.addWidget(self.model_desc_label)

        # ── Primary Actions Row ──
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)

        self.upload_btn = QPushButton("  Select Audio File")
        self.upload_btn.setObjectName("uploadBtn")
        self.upload_btn.setMinimumHeight(44)
        self.upload_btn.setCursor(Qt.PointingHandCursor)
        self.upload_btn.clicked.connect(self.upload_audio)

        self.transcribe_btn = QPushButton("  Transcribe")
        self.transcribe_btn.setObjectName("transcribeBtn")
        self.transcribe_btn.setMinimumHeight(44)
        self.transcribe_btn.setCursor(Qt.PointingHandCursor)
        self.transcribe_btn.clicked.connect(self.start_transcription)
        self.transcribe_btn.setEnabled(False)

        self.stop_btn = QPushButton("  Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.stop_transcription)
        self.stop_btn.setVisible(False)

        action_layout.addWidget(self.upload_btn, 3)
        action_layout.addWidget(self.transcribe_btn, 2)
        action_layout.addWidget(self.stop_btn, 1)
        layout.addLayout(action_layout)

        # File name label
        self.file_label = QLabel("")
        self.file_label.setStyleSheet("color: #8888aa; font-size: 12px; background: transparent; margin: 2px 0 0 4px;")
        layout.addWidget(self.file_label)

        # ── Progress elements ──
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #4ade80; font-size: 13px; font-weight: 600; background: transparent; margin-top: 4px;")
        layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(14)
        layout.addWidget(self.progress_bar)

        # ── Spacer before editor ──
        layout.addSpacing(6)

        # ── Transcript Editor ──
        self.text_editor = QTextEdit()
        self.text_editor.setPlaceholderText("Your transcript will appear here.\nYou can edit the text before saving.")
        self.text_editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.text_editor, 1)

        # Register editor with watermark widget so it only paints behind it
        central_widget.set_editor(self.text_editor)

        # ── Save Button ──
        layout.addSpacing(8)
        self.save_btn = QPushButton("  Save as .txt")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.setMinimumHeight(38)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.save_transcript)
        self.save_btn.setEnabled(False)
        layout.addWidget(self.save_btn)

        # ── Footer ──
        footer_layout = QHBoxLayout()
        copyright_label = QLabel("© Vani.txt 2026")
        copyright_label.setStyleSheet("color: #3a3a50; font-size: 10px; background: transparent; margin-top: 4px;")
        footer_layout.addWidget(copyright_label)
        footer_layout.addStretch()
        footer_label = QLabel("Powered by OpenAI Whisper")
        footer_label.setStyleSheet("color: #3a3a50; font-size: 10px; background: transparent; margin-top: 4px;")
        footer_layout.addWidget(footer_label)
        layout.addLayout(footer_layout)

    def update_model_description(self, model_name):
        descriptions = {
            "Tiny": "Fastest, lowest accuracy. Good for quick tests only.",
            "Base": "Balanced speed and accuracy. Good for most use cases.",
            "Small": "Better accuracy, especially for Indian languages. Moderately slow.",
            "Medium": "High accuracy, noise-robust. Requires decent hardware.",
            "Large": "Best quality across all languages. Very slow, high memory."
        }
        self.model_desc_label.setText(descriptions.get(model_name, ""))

    def apply_styles(self):
        self.setStyleSheet("""
            /* ── Base ── */
            QMainWindow, QWidget {
                background-color: #12121e;
                color: #d8d8e8;
                font-family: 'Segoe UI', sans-serif;
                font-size: 14px;
            }

            /* ── Combo Boxes ── */
            QComboBox {
                background-color: #1c1c32;
                border: 1px solid #2a2a48;
                border-radius: 6px;
                padding: 7px 12px;
                min-width: 110px;
                color: #d8d8e8;
                outline: 0;
            }
            QComboBox:hover {
                border: 1px solid #4a4a6a;
            }
            QComboBox:focus {
                border: 1px solid #6c63ff;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 20px;
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #1c1c32;
                border: 1px solid #3a3a58;
                color: #d8d8e8;
                outline: 0;
                selection-background-color: #2a2a4a;
                selection-color: #ffffff;
                padding: 4px;
            }

            /* ── Upload Button ── */
            #uploadBtn {
                background-color: #1c1c32;
                border: 1px solid #3a3a58;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: 600;
                font-size: 14px;
                color: #d8d8e8;
            }
            #uploadBtn:hover {
                background-color: #252548;
                border: 1px solid #6c63ff;
                color: #ffffff;
            }

            /* ── Transcribe Button (Primary CTA) ── */
            #transcribeBtn {
                background-color: #6c63ff;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: 700;
                font-size: 15px;
                color: #ffffff;
            }
            #transcribeBtn:hover {
                background-color: #7b73ff;
            }
            #transcribeBtn:disabled {
                background-color: #2a2a3e;
                color: #555;
            }

            /* ── Save Button ── */
            #saveBtn {
                background-color: transparent;
                border: 1px solid #3a3a58;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
                font-size: 13px;
                color: #aaaacc;
            }
            #saveBtn:hover {
                background-color: #1c1c32;
                border-color: #6c63ff;
                color: #ffffff;
            }
            #saveBtn:disabled {
                background-color: transparent;
                border-color: #222238;
                color: #444;
            }

            /* ── Text Editor ── */
            QTextEdit {
                background-color: #16162a;
                border: 1px solid #2a2a48;
                border-radius: 8px;
                padding: 14px;
                font-size: 15px;
                color: #d8d8e8;
                selection-background-color: #3a3a6a;
            }
            QTextEdit:focus {
                border: 1px solid #6c63ff;
            }

            /* ── Progress Bar ── */
            QProgressBar {
                border: none;
                border-radius: 7px;
                background-color: #1c1c32;
                color: #d8d8e8;
                font-size: 11px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #6c63ff;
                border-radius: 7px;
            }

            /* ── Scrollbar ── */
            QScrollBar:vertical {
                background: #12121e;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #3a3a58;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }

            /* ── Stop Button ── */
            #stopBtn {
                background-color: #cc3344;
                border: none;
                border-radius: 8px;
                padding: 10px 16px;
                font-weight: 700;
                font-size: 14px;
                color: #ffffff;
            }
            #stopBtn:hover {
                background-color: #dd4455;
            }
        """)

    def upload_audio(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Audio File", "", 
            "Audio Files (*.mp3 *.wav *.m4a *.flac *.ogg *.aac)"
        )
        if file_path:
            self.audio_path = file_path
            self.file_label.setText(os.path.basename(file_path))
            self.transcribe_btn.setEnabled(True)
            self.text_editor.clear()
            self.save_btn.setEnabled(False)
            self.status_label.setText("")

    def start_transcription(self):
        if not self.audio_path:
            return
            
        model_name = self.model_combo.currentText().lower()
        selected_lang = self.lang_combo.currentText()
        language = None if selected_lang == "Auto-Detect" else selected_lang.lower()
        
        self.thread = TranscriptionThread(self.audio_path, model_name, language)
        self.thread.progress.connect(self.update_status)
        self.thread.progress_percent.connect(self.update_progress)
        self.thread.text_segment.connect(self.append_text)
        self.thread.finished.connect(self.on_transcription_finished)
        self.thread.error.connect(self.on_transcription_error)
        
        self.upload_btn.setEnabled(False)
        self.transcribe_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self.model_combo.setEnabled(False)
        self.lang_combo.setEnabled(False)
        
        # Set progress bar to percentage mode (0)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.text_editor.clear()
        
        self.thread.start()

    def stop_transcription(self):
        if self.thread and self.thread.isRunning():
            self.thread.terminate()
            self.thread.wait(2000)
        self.status_label.setText("Transcription stopped by user.")
        self.status_label.setStyleSheet("color: #ffaa44; font-size: 13px; font-weight: 600; background: transparent;")
        self.progress_bar.setVisible(False)
        self.upload_btn.setEnabled(True)
        self.transcribe_btn.setVisible(True)
        self.transcribe_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.model_combo.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.save_btn.setEnabled(len(self.text_editor.toPlainText().strip()) > 0)

    def update_status(self, msg):
        self.status_label.setText(msg)
        
    def update_progress(self, percent):
        self.progress_bar.setValue(percent)

    def append_text(self, text):
        self.text_editor.insertPlainText(text)
        # Scroll to bottom
        scrollbar = self.text_editor.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_transcription_finished(self, text):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText("Transcription completed successfully!")
        self.status_label.setStyleSheet("color: #4ade80; font-weight: bold;")
        
        # The final result text shouldn't overwrite if we streamed, but maybe we just ensure it's there
        # We can just clean trailing spaces. The real-time stream already added it.
        # But `result["text"]` is the full string from whisper. We can just replace it to correct any missing pieces.
        self.text_editor.setPlainText(text.strip())
        
        self.upload_btn.setEnabled(True)
        self.transcribe_btn.setVisible(True)
        self.transcribe_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.model_combo.setEnabled(True)
        self.lang_combo.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

    def on_transcription_error(self, err_msg):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.status_label.setText("An error occurred.")
        self.status_label.setStyleSheet("color: #ff4d4d; font-weight: bold;")
        
        QMessageBox.critical(self, "Error", f"Failed to transcribe audio:\\n{err_msg}")
        
        self.upload_btn.setEnabled(True)
        self.transcribe_btn.setVisible(True)
        self.transcribe_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.model_combo.setEnabled(True)
        self.lang_combo.setEnabled(True)

    def save_transcript(self):
        text = self.text_editor.toPlainText()
        if not text:
            return
            
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Transcript", "transcript.txt", "Text Files (*.txt)"
        )
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(text)
                QMessageBox.information(self, "Success", "Transcript saved successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not save file:\\n{str(e)}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AppWindow()
    window.show()
    sys.exit(app.exec())
