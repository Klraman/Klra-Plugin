"""
Texture Exporter Plugin for Substance Painter 11.1.2
------------------------------------------------------
Block 1: Single Texture Export
- Opens via Python menu (can be re-opened any time)
- Choose export location via file dialog
- Choose file extension (PNG, JPG, TIFF, EXR, BMP, TGA)
- Window hides on close so state is preserved between opens
"""

import substance_painter.ui
import substance_painter.export
import substance_painter.textureset
import substance_painter.project
import substance_painter.logging

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QFileDialog, QGroupBox, QMessageBox,
    QSizePolicy
)
from PySide6.QtGui import QAction
from PySide6.QtCore import Qt

# ---------------------------------------------------------------------------
# Plugin state – keeps the window alive so it can be reshown without
# rebuilding from scratch.
# ---------------------------------------------------------------------------
_plugin_instance = None


# ===========================================================================
# UI Window
# ===========================================================================
class TextureExporterWindow(QWidget):
    """Main plugin window – single texture export (Block 1)."""

    # Supported export formats  (display name → SP format string)
    FORMATS = {
        "PNG":  "png",
        "JPG":  "jpeg",
        "TIFF": "tiff",
        "EXR":  "exr",
        "BMP":  "bmp",
        "TGA":  "tga",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(440)
        # Qt.Window  → own window frame; stays open alongside the main UI
        self.setWindowFlags(Qt.Window)

        self._last_dir = ""  # remembered across re-opens within the session

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Export Settings ────────────────────────────────────────────
        grp = QGroupBox("Export Settings")
        grp_layout = QVBoxLayout(grp)
        grp_layout.setSpacing(8)

        # Output path row
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Output Path:"))
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("Choose where to save the texture…")
        self._path_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._path_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(72)
        browse_btn.clicked.connect(self._browse_output)
        path_row.addWidget(browse_btn)
        grp_layout.addLayout(path_row)

        # Format / extension row
        ext_row = QHBoxLayout()
        ext_row.addWidget(QLabel("File Format:"))
        self._fmt_combo = QComboBox()
        for label in self.FORMATS:
            self._fmt_combo.addItem(label)
        self._fmt_combo.setFixedWidth(90)
        self._fmt_combo.currentIndexChanged.connect(self._sync_extension)
        ext_row.addWidget(self._fmt_combo)
        ext_row.addStretch()
        grp_layout.addLayout(ext_row)

        root.addWidget(grp)

        # ── Active Texture Set info ────────────────────────────────────
        info_grp = QGroupBox("Active Texture Set")
        info_layout = QVBoxLayout(info_grp)
        self._info_label = QLabel("No project open.")
        self._info_label.setWordWrap(True)
        info_layout.addWidget(self._info_label)
        root.addWidget(info_grp)

        # ── Buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh Info")
        refresh_btn.clicked.connect(self._refresh_info)
        btn_row.addWidget(refresh_btn)

        self._export_btn = QPushButton("Export Texture")
        self._export_btn.setFixedHeight(32)
        self._export_btn.setStyleSheet(
            "QPushButton           { background:#2d6a9f; color:white; font-weight:bold; }"
            "QPushButton:hover     { background:#3a82c4; }"
            "QPushButton:pressed   { background:#1e4f7a; }"
        )
        self._export_btn.clicked.connect(self._do_export)
        btn_row.addWidget(self._export_btn)
        root.addLayout(btn_row)

        # Status line
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignCenter)
        root.addWidget(self._status_label)

        self._refresh_info()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _current_label(self) -> str:
        """Return the selected format label (e.g. 'PNG')."""
        return self._fmt_combo.currentText()

    def _current_ext(self) -> str:
        """Return the file extension for the selected format (lowercase)."""
        label = self._current_label()
        # EXT = lowercase of label, except JPG → jpg
        ext_map = {"JPG": "jpg", "TIFF": "tiff", "EXR": "exr",
                   "BMP": "bmp", "TGA": "tga", "PNG": "png"}
        return ext_map.get(label, label.lower())

    def _current_sp_format(self) -> str:
        """Return the Substance Painter format string for the selection."""
        return self.FORMATS[self._current_label()]

    def _sync_extension(self):
        """When format changes, update the extension in the path field."""
        path = self._path_edit.text().strip()
        if not path:
            return
        stem = path.rsplit(".", 1)[0] if "." in path.split("/")[-1] else path
        self._path_edit.setText(f"{stem}.{self._current_ext()}")

    def _browse_output(self):
        """Open a Save-As dialog so the user picks the output file path."""
        ext  = self._current_ext()
        filt = f"{self._current_label()} Files (*.{ext});;All Files (*)"
        start = self._path_edit.text().strip() or self._last_dir

        path, _ = QFileDialog.getSaveFileName(self, "Save Texture As", start, filt)
        if not path:
            return

        # Guarantee the right extension
        if not path.lower().endswith(f".{ext}"):
            path = f"{path}.{ext}"

        self._path_edit.setText(path)
        self._last_dir = "/".join(path.replace("\\", "/").split("/")[:-1])

    def _refresh_info(self):
        """Populate the texture-set info label from the open project."""
        if not substance_painter.project.is_open():
            self._info_label.setText("⚠  No project is currently open.")
            return

        try:
            ts_list = substance_painter.textureset.all_texture_sets()
            if not ts_list:
                self._info_label.setText("No texture sets found in this project.")
                return

            lines = []
            for ts in ts_list:
                try:
                    channels = ts.get_stack().all_channels()
                    ch_names = ", ".join(str(c).split(".")[-1] for c in channels)
                    lines.append(f"• {ts.name()}  –  {ch_names}")
                except Exception:
                    lines.append(f"• {ts.name()}")

            self._info_label.setText("\n".join(lines))
        except Exception as e:
            self._info_label.setText(f"Error reading project:\n{e}")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _do_export(self):
        """Validate inputs then call Substance Painter's export API."""

        # ── Guards ────────────────────────────────────────────────────
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Please open a project first.")
            return

        out_path = self._path_edit.text().strip().replace("\\", "/")
        if not out_path:
            QMessageBox.warning(self, "No Path",
                                "Please choose an output path via Browse…")
            return

        ext = self._current_ext()
        if not out_path.lower().endswith(f".{ext}"):
            out_path = f"{out_path}.{ext}"
            self._path_edit.setText(out_path)

        # ── Export ────────────────────────────────────────────────────
        try:
            ts_list = substance_painter.textureset.all_texture_sets()
            if not ts_list:
                QMessageBox.warning(self, "No Texture Sets",
                                    "No texture sets found in this project.")
                return

            # Block 1: always export the first (active) texture set
            ts         = ts_list[0]
            output_dir = "/".join(out_path.split("/")[:-1])

            # Bit depth: EXR → 16f, everything else → 8
            bit_depth  = "16f" if self._current_sp_format() == "exr" else "8"

            export_config = {
                "exportPath": output_dir,
                "defaultExportPreset": "DocumentChannels",
                "exportList": [
                    {"rootPath": ts.name()}
                ],
                "exportParameters": [
                    {
                        "parameters": {
                            "fileFormat":       self._current_sp_format(),
                            "bitDepth":         bit_depth,
                            "dithering":        True,
                            "paddingAlgorithm": "infinite",
                            "sizeLog2":         11,   # 2048 × 2048
                        }
                    }
                ]
            }

            result = substance_painter.export.export_project_textures(export_config)

            # ── Feedback ──────────────────────────────────────────────
            if result.status == substance_painter.export.ExportStatus.Success:
                exported = [p for paths in result.textures.values() for p in paths]
                self._status_label.setText(
                    f"✅ Exported {len(exported)} file(s) to: {output_dir}"
                )
                details = "\n".join(f"  {p}" for p in exported)
                QMessageBox.information(
                    self, "Export Complete",
                    f"Texture set : {ts.name()}\n"
                    f"Format      : {self._current_label()}\n"
                    f"Destination : {output_dir}\n\n"
                    f"Files written:\n{details}"
                )
                substance_painter.logging.log(
                    substance_painter.logging.INFO, "TextureExporter",
                    f"Exported {len(exported)} file(s) to {output_dir}"
                )
            else:
                err = str(result.message)
                self._status_label.setText(f"❌ Export failed: {err}")
                QMessageBox.critical(self, "Export Failed", f"Export error:\n{err}")

        except Exception as e:
            self._status_label.setText(f"❌ Error: {e}")
            QMessageBox.critical(self, "Error",
                                 f"An unexpected error occurred:\n{e}")
            substance_painter.logging.log(
                substance_painter.logging.ERROR, "TextureExporter", str(e)
            )

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """
        Hide instead of destroying.
        This preserves the user's path / format selections for the next open.
        """
        event.ignore()
        self.hide()


# ===========================================================================
# Plugin class – manages the menu action and window lifetime
# ===========================================================================
class TextureExporterPlugin:
    """
    Registers the plugin with Substance Painter.

    The window is created once and toggled (show/hide) so that the
    user's settings are kept between re-opens in the same session.
    """

    def __init__(self):
        main_win      = substance_painter.ui.get_main_window()
        self._window  = TextureExporterWindow(main_win)
        self._action  = None
        self._register_menu()

    # ------------------------------------------------------------------
    # Menu registration
    # ------------------------------------------------------------------
    def _register_menu(self):
        """
        Add a checkable 'Texture Exporter' entry to the Python menu.
        The tick mark reflects whether the window is currently visible.
        """
        self._action = QAction("Texture Exporter",
                               substance_painter.ui.get_main_window())
        self._action.setCheckable(True)
        self._action.triggered.connect(self._toggle_window)

        substance_painter.ui.add_action(
            substance_painter.ui.ApplicationMenu.Python,
            self._action
        )

    def _toggle_window(self):
        """Show the window if hidden, hide it if visible."""
        if self._window.isVisible():
            self._window.hide()
            self._action.setChecked(False)
        else:
            self.show_window()

    def show_window(self):
        """Unconditionally bring the window to the front."""
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()
        self._action.setChecked(True)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def unload(self):
        """Remove the menu entry and destroy the window."""
        if self._action:
            substance_painter.ui.delete_ui_element(self._action)
            self._action = None
        if self._window:
            self._window.deleteLater()
            self._window = None


# ===========================================================================
# Substance Painter plugin hooks  (called automatically by SP)
# ===========================================================================
def start_plugin():
    global _plugin_instance
    _plugin_instance = TextureExporterPlugin()
    substance_painter.logging.log(
        substance_painter.logging.INFO,
        "TextureExporter",
        "Plugin loaded – open via Python ▸ Texture Exporter."
    )


def close_plugin():
    global _plugin_instance
    if _plugin_instance:
        _plugin_instance.unload()
        _plugin_instance = None
    substance_painter.logging.log(
        substance_painter.logging.INFO,
        "TextureExporter",
        "Plugin unloaded."
    )


# Development helper – run this file directly from SP's Python console:
#   exec(open("texture_exporter.py").read())
if __name__ == "__main__":
    start_plugin()