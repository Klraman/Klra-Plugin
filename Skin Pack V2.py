"""
Texture Exporter – Block 1
Substance Painter 11.1.2  |  PySide6

API sources (all from uploaded docs):
  ui.html          → ApplicationMenu.Window, add_action, get_main_window
  export.html      → export_project_textures, list_predefined_export_presets,
                     list_resource_export_presets, ExportStatus, exportShaderParams,
                     exportPresets (inline preset definition), maps/channels/fileName
  textureset.html  → TextureSet.name (property), get_active_stack
  navigation.html  → get_root_layer_nodes, Node.get_name(), GroupLayerNode.sub_layers()

Install: Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
Open:    Window ▶ Texture Exporter…
"""

import substance_painter.ui
import substance_painter.export
import substance_painter.textureset
import substance_painter.layerstack
import substance_painter.project
import substance_painter.logging

from PySide6.QtGui     import QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QFileDialog, QGroupBox, QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt

_plugin_widgets = []
_window         = None


# ─────────────────────────────────────────────────────────────────────────────
# Inline "Base Color" preset definition
# Defined entirely in the JSON config – no external resource needed.
# Channel structure confirmed from export.html "Full json_config dict" section.
# ─────────────────────────────────────────────────────────────────────────────
BASE_COLOR_PRESET_NAME = "Base Color"

BASE_COLOR_PRESET = {
    "name": BASE_COLOR_PRESET_NAME,
    "maps": [
        {
            # $textureSet wildcard confirmed in export.html fileName comment.
            # Layer name is injected at runtime as the suffix.
            "fileName": "$textureSet_BaseColor",
            "channels": [
                {
                    "destChannel": "R",
                    "srcChannel":  "R",
                    "srcMapType":  "documentMap",   # confirmed: export.html srcMapType values
                    "srcMapName":  "baseColor"
                },
                {
                    "destChannel": "G",
                    "srcChannel":  "G",
                    "srcMapType":  "documentMap",
                    "srcMapName":  "baseColor"
                },
                {
                    "destChannel": "B",
                    "srcChannel":  "B",
                    "srcMapType":  "documentMap",
                    "srcMapName":  "baseColor"
                },
            ]
        }
    ]
}


# ─────────────────────────────────────────────────────────────────────────────
# Helper – recursively collect all layer nodes from a stack
# get_root_layer_nodes → navigation.html line 973
# GroupLayerNode.sub_layers() → navigation.html line 839
# Node.get_name() → navigation.html line 1082
# ─────────────────────────────────────────────────────────────────────────────
def _collect_layers(stack):
    """
    Return a flat list of (display_name, node) for every layer in the stack.
    Groups are shown with indentation so the user can tell them apart.
    """
    results = []

    def _walk(nodes, depth=0):
        indent = "  " * depth
        for node in nodes:
            name = node.get_name()          # Node.get_name() – navigation.html
            results.append((f"{indent}{name}", node))
            # Recurse into groups
            if isinstance(node, substance_painter.layerstack.GroupLayerNode):
                _walk(node.sub_layers(), depth + 1)   # sub_layers() – navigation.html

    root_nodes = substance_painter.layerstack.get_root_layer_nodes(stack)
    _walk(root_nodes)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Window
# ─────────────────────────────────────────────────────────────────────────────
class TextureExporterWindow(QWidget):

    # File format: display label → (extension, SP fileFormat string)
    # SP format strings confirmed from export.html examples
    FORMATS = {
        "PNG":  ("png",  "png"),
        "JPG":  ("jpg",  "jpeg"),
        "TIFF": ("tiff", "tiff"),
        "EXR":  ("exr",  "exr"),
        "TGA":  ("tga",  "tga"),
        "BMP":  ("bmp",  "bmp"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(480)
        self.setWindowFlags(Qt.Window)
        self._last_dir = ""
        # Maps combo index → preset info dict: {"type": "inline"|"predefined"|"resource", ...}
        self._preset_data = []
        self._build_ui()

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Preset selector ───────────────────────────────────────────────────
        preset_grp = QGroupBox("Output Template Preset")
        preset_gl  = QVBoxLayout(preset_grp)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self._preset_combo = QComboBox()
        self._preset_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        preset_row.addWidget(self._preset_combo)
        btn_refresh_presets = QPushButton("↺")
        btn_refresh_presets.setFixedWidth(28)
        btn_refresh_presets.setToolTip("Reload preset list from Substance Painter")
        btn_refresh_presets.clicked.connect(self._refresh_presets)
        preset_row.addWidget(btn_refresh_presets)
        preset_gl.addLayout(preset_row)

        root.addWidget(preset_grp)

        # ── Layer selector ────────────────────────────────────────────────────
        layer_grp = QGroupBox("Layer (used as output filename)")
        layer_gl  = QVBoxLayout(layer_grp)

        layer_row = QHBoxLayout()
        layer_row.addWidget(QLabel("Layer:"))
        self._layer_combo = QComboBox()
        self._layer_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layer_row.addWidget(self._layer_combo)
        btn_refresh_layers = QPushButton("↺")
        btn_refresh_layers.setFixedWidth(28)
        btn_refresh_layers.setToolTip("Reload layer list from the active texture set")
        btn_refresh_layers.clicked.connect(self._refresh_layers)
        layer_row.addWidget(btn_refresh_layers)
        layer_gl.addLayout(layer_row)

        root.addWidget(layer_grp)

        # ── Export settings ───────────────────────────────────────────────────
        export_grp = QGroupBox("Export Settings")
        export_gl  = QVBoxLayout(export_grp)
        export_gl.setSpacing(8)

        # Output folder
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Output Folder:"))
        self._path = QLineEdit()
        self._path.setPlaceholderText("Click Browse… to choose an output folder")
        self._path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._path)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(72)
        btn_browse.clicked.connect(self._browse)
        path_row.addWidget(btn_browse)
        export_gl.addLayout(path_row)

        # File format
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format:"))
        self._fmt = QComboBox()
        for label in self.FORMATS:
            self._fmt.addItem(label)
        self._fmt.setFixedWidth(80)
        fmt_row.addWidget(self._fmt)
        fmt_row.addStretch()
        export_gl.addLayout(fmt_row)

        root.addWidget(export_grp)

        # ── Export button ─────────────────────────────────────────────────────
        self._btn_export = QPushButton("Export Texture")
        self._btn_export.setFixedHeight(30)
        self._btn_export.clicked.connect(self._export)
        root.addWidget(self._btn_export)

        # ── Status ────────────────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignCenter)
        root.addWidget(self._status)

        # Populate on first build
        self._refresh_presets()
        self._refresh_layers()

    # ── Presets ───────────────────────────────────────────────────────────────
    def _refresh_presets(self):
        """
        Populate the preset combo with:
          1. Built-in 'Base Color' inline preset (always first / default)
          2. SP's predefined presets  (list_predefined_export_presets – export.html)
          3. User/shelf resource presets (list_resource_export_presets – export.html)
        """
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_data.clear()

        # 1. Our custom inline preset – always the default
        self._preset_combo.addItem(f"★ {BASE_COLOR_PRESET_NAME}  [custom]")
        self._preset_data.append({"type": "inline"})

        if substance_painter.project.is_open():
            # 2. Predefined (built-in) presets
            # list_predefined_export_presets() → export.html line 1065
            # PredefinedExportPreset.name / .url are plain attributes
            try:
                for p in substance_painter.export.list_predefined_export_presets():
                    self._preset_combo.addItem(f"{p.name}  [predefined]")
                    self._preset_data.append({"type": "predefined", "url": p.url})
            except Exception as e:
                substance_painter.logging.log(
                    substance_painter.logging.WARNING,
                    "TextureExporter", f"Could not load predefined presets: {e}"
                )

            # 3. Resource (shelf) presets
            # list_resource_export_presets() → export.html
            # ResourceExportPreset.resource_id.name / .resource_id.url() – export.html line 1158
            try:
                for p in substance_painter.export.list_resource_export_presets():
                    self._preset_combo.addItem(f"{p.resource_id.name}  [shelf]")
                    self._preset_data.append({"type": "resource", "url": p.resource_id.url()})
            except Exception as e:
                substance_painter.logging.log(
                    substance_painter.logging.WARNING,
                    "TextureExporter", f"Could not load resource presets: {e}"
                )

        self._preset_combo.blockSignals(False)
        self._preset_combo.setCurrentIndex(0)   # default = Base Color

    # ── Layers ────────────────────────────────────────────────────────────────
    def _refresh_layers(self):
        """
        Populate the layer combo from the active texture set's stack.
        get_root_layer_nodes + Node.get_name() – navigation.html
        """
        self._layer_combo.clear()

        if not substance_painter.project.is_open():
            self._layer_combo.addItem("(no project open)")
            return

        try:
            stack = substance_painter.textureset.get_active_stack()
            layers = _collect_layers(stack)   # list of (display_name, node)
            if not layers:
                self._layer_combo.addItem("(no layers found)")
                return
            for display_name, _ in layers:
                self._layer_combo.addItem(display_name)
            # Store node references as item data
            for i, (_, node) in enumerate(layers):
                self._layer_combo.setItemData(i, node)
        except Exception as e:
            self._layer_combo.addItem(f"Error: {e}")
            substance_painter.logging.log(
                substance_painter.logging.ERROR, "TextureExporter", str(e)
            )

    def _selected_layer_name(self):
        """Return the raw layer name (strip leading indent) of the selected item."""
        raw = self._layer_combo.currentText().strip()
        return raw if raw else "export"

    # ── Browse ────────────────────────────────────────────────────────────────
    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Output Folder",
            self._path.text().strip() or self._last_dir
        )
        if folder:
            self._path.setText(folder.replace("\\", "/"))
            self._last_dir = folder.replace("\\", "/")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _ext(self):
        return self.FORMATS[self._fmt.currentText()][0]

    def _sp_format(self):
        return self.FORMATS[self._fmt.currentText()][1]

    # ── Export ────────────────────────────────────────────────────────────────
    def _export(self):
        # Guards
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Please open a project first.")
            return

        out_dir = self._path.text().strip().replace("\\", "/")
        if not out_dir:
            QMessageBox.warning(self, "No Folder",
                                "Please choose an output folder via Browse…")
            return

        # Texture set
        try:
            ts_list = substance_painter.textureset.all_texture_sets()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return
        if not ts_list:
            QMessageBox.warning(self, "No Texture Sets",
                                "No texture sets found in this project.")
            return
        ts    = ts_list[0]
        stack = ts.get_stack()    # Stack.get_stack confirmed in textureset docs

        # Preset selection
        preset_idx  = self._preset_combo.currentIndex()
        preset_info = self._preset_data[preset_idx] if preset_idx < len(self._preset_data) else {"type": "inline"}

        # Layer name used as output filename
        layer_name  = self._selected_layer_name()
        sp_fmt      = self._sp_format()
        bit_depth   = "16f" if sp_fmt == "exr" else "8"

        # ── Build config ──────────────────────────────────────────────────────
        # exportShaderParams required bool – export.html line 965
        # exportPresets / defaultExportPreset – export.html "Full json_config" section
        config = {
            "exportShaderParams": False,
            "exportPath":         out_dir,
            "exportList": [
                {"rootPath": ts.name}   # TextureSet.name is a property – textureset.html
            ],
            "exportParameters": [
                {
                    "parameters": {
                        "fileFormat": sp_fmt,
                        "bitDepth":   bit_depth,
                    }
                }
            ]
        }

        if preset_info["type"] == "inline":
            # Define the preset entirely in JSON and reference it by name.
            # The fileName wildcard uses the selected layer name as suffix.
            inline = dict(BASE_COLOR_PRESET)  # shallow copy
            inline["maps"] = [
                {
                    "fileName": layer_name,   # layer name becomes the output filename
                    "channels": BASE_COLOR_PRESET["maps"][0]["channels"]
                }
            ]
            config["exportPresets"]       = [inline]
            config["defaultExportPreset"] = BASE_COLOR_PRESET_NAME
        else:
            # Use SP's own preset via its URL – no exportPresets block needed.
            # The layer name is used for the output path only; the preset controls
            # the actual channel mapping and filename patterns.
            config["defaultExportPreset"] = preset_info["url"]

        # ── Call API ──────────────────────────────────────────────────────────
        try:
            result = substance_painter.export.export_project_textures(config)
        except Exception as e:
            msg = str(e)
            self._status.setText(f"❌ {msg}")
            QMessageBox.critical(self, "Export Error", msg)
            substance_painter.logging.log(
                substance_painter.logging.ERROR, "TextureExporter", msg)
            return

        # ── Result ────────────────────────────────────────────────────────────
        if result.status == substance_painter.export.ExportStatus.Success:
            files = [p for paths in result.textures.values() for p in paths]
            self._status.setText(f"✅ Exported {len(files)} file(s) → {out_dir}")
            QMessageBox.information(
                self, "Export Complete",
                f"Texture Set : {ts.name}\n"
                f"Layer       : {layer_name}\n"
                f"Format      : {self._fmt.currentText()}\n"
                f"Output      : {out_dir}\n\n" +
                "\n".join(files)
            )
            substance_painter.logging.log(
                substance_painter.logging.INFO, "TextureExporter",
                f"Exported {len(files)} file(s) to {out_dir}"
            )
        else:
            self._status.setText(f"❌ {result.message}")
            QMessageBox.critical(self, "Export Failed", result.message)

    # ── Window close → hide, preserve state ───────────────────────────────────
    def closeEvent(self, event):
        event.ignore()
        self.hide()


# ─────────────────────────────────────────────────────────────────────────────
# Plugin hooks
# ─────────────────────────────────────────────────────────────────────────────
def start_plugin():
    global _window

    _window = TextureExporterWindow(substance_painter.ui.get_main_window())

    action = QAction("Texture Exporter…", substance_painter.ui.get_main_window())
    action.triggered.connect(_show_window)

    # ApplicationMenu.Window – confirmed valid in ui.html
    substance_painter.ui.add_action(
        substance_painter.ui.ApplicationMenu.Window,
        action
    )
    _plugin_widgets.append(action)

    substance_painter.logging.log(
        substance_painter.logging.INFO,
        "TextureExporter",
        "Loaded – Window ▶ Texture Exporter…"
    )


def close_plugin():
    global _window
    for w in _plugin_widgets:
        substance_painter.ui.delete_ui_element(w)
    _plugin_widgets.clear()
    if _window:
        _window.deleteLater()
        _window = None
    substance_painter.logging.log(
        substance_painter.logging.INFO, "TextureExporter", "Unloaded."
    )


def _show_window():
    if _window:
        _window.show()
        _window.raise_()
        _window.activateWindow()
        # Refresh both lists every time the window is opened
        _window._refresh_presets()
        _window._refresh_layers()


if __name__ == "__main__":
    start_plugin()