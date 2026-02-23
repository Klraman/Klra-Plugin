"""
Texture Exporter – Phase 1 (Single) + Phase 2 (Batch)
Substance Painter 11.1.2  |  PySide6

API sources (uploaded docs only):
  ui.html         → ApplicationMenu.Window, add_action, get_main_window
  export.html     → export_project_textures, exportShaderParams, exportPresets,
                    exportList, exportParameters, ExportStatus, TextureExportResult,
                    list_predefined_export_presets, list_resource_export_presets
  textureset.html → TextureSet.name (property), get_active_stack, all_texture_sets
  navigation.html → get_root_layer_nodes, GroupLayerNode.sub_layers(),
                    Node.get_name(), Node.is_visible(), Node.set_visible()

Install: Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
Open:    Window ▶ Texture Exporter…
"""

import re

import substance_painter.ui
import substance_painter.export
import substance_painter.textureset
import substance_painter.layerstack
import substance_painter.project
import substance_painter.logging

from PySide6.QtGui     import QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QLineEdit, QFileDialog, QGroupBox, QMessageBox,
    QSizePolicy, QProgressBar, QTextEdit, QTabWidget, QFrame,
)
from PySide6.QtCore import Qt, QCoreApplication

# ─────────────────────────────────────────────────────────────────────────────
# Plugin-level state
# ─────────────────────────────────────────────────────────────────────────────
_plugin_widgets = []
_window         = None


# =============================================================================
# ── Shared helpers ────────────────────────────────────────────────────────────
# =============================================================================

# ── Name cleaning ─────────────────────────────────────────────────────────────
def clean_skin_name(raw: str) -> str:
    """
    Convert a raw SP layer name to a clean export filename.
    Rules (structure-driven, no hardcoded skin lists):
      1. Strip leading '!' characters  (e.g. '!!' or '!')
      2. Strip leading 'Skin ' prefix  (case-insensitive)
      3. Strip trailing parenthetical  '(Any Mat)', '(Metal)', '(Plastic)',
         or truncated variants like '(Any M...' — matched by opening paren
      4. Replace the word 'Black' → 'Default'
    """
    s = raw.lstrip('!')
    s = re.sub(r'^[Ss]kin\s+', '', s).strip()
    s = re.sub(r'\s*\(.*', '', s).strip()          # strip from first '(' onward
    s = re.sub(r'\bBlack\b', 'Default', s)
    return s.strip()


# ── Layer structure helpers ───────────────────────────────────────────────────
def _group_children(node):
    """Return only GroupLayerNode children of node.sub_layers()."""
    return [
        c for c in node.sub_layers()
        if isinstance(c, substance_painter.layerstack.GroupLayerNode)
    ]


def find_skin_pack_root(stack):
    """
    Structurally detect the 3-level skin pack group:
      Level 1: GroupLayerNode at stack root
      Level 2: its children are ALL GroupLayerNodes (categories)
      Level 3: each Level-2 child has GroupLayerNode children (individual skins)

    Returns the Level-1 node, or None if not found.
    No name matching — purely positional/structural.
    """
    roots = substance_painter.layerstack.get_root_layer_nodes(stack)
    for node in roots:
        if not isinstance(node, substance_painter.layerstack.GroupLayerNode):
            continue
        level2 = _group_children(node)
        if len(level2) < 2:
            continue
        # Every Level-2 group must itself have group children
        if all(len(_group_children(cat)) > 0 for cat in level2):
            return node
    return None


def categorize_level2(skin_pack_node):
    """
    Separate Level-2 groups into:
      • worn_pair  – exactly two groups sharing the same Level-3 child count
                     (Plastic + Metal worn categories)
      • standalone – all remaining categories (Normal, Bright, …)

    Detection is purely structural: two Level-2 groups with identical
    GroupLayerNode child counts are the worn pair.

    If the pair is ambiguous, a name hint ('plastic'/'metal') breaks the tie;
    otherwise positional order is used (first = plastic, second = metal).

    Returns: (standalone_list, worn_plastic_node, worn_metal_node)
      worn_* may be None if no pair is detected.
    """
    level2 = _group_children(skin_pack_node)

    # Group categories by their skin child count
    by_count = {}
    for cat in level2:
        n = len(_group_children(cat))
        by_count.setdefault(n, []).append(cat)

    worn_plastic = None
    worn_metal   = None
    standalone   = []

    for n, cats in by_count.items():
        if len(cats) == 2:
            # ── Worn pair detected ────────────────────────────────────────────
            a, b   = cats[0], cats[1]
            a_name = a.get_name().lower()
            b_name = b.get_name().lower()
            # Use name hint if available; fall back to position
            if 'plastic' in a_name or 'metal' in b_name:
                worn_plastic, worn_metal = a, b
            elif 'metal' in a_name or 'plastic' in b_name:
                worn_plastic, worn_metal = b, a
            else:
                # Positional fallback: preserve stack order
                idx_a = level2.index(a)
                idx_b = level2.index(b)
                worn_plastic, worn_metal = (a, b) if idx_a < idx_b else (b, a)
        else:
            standalone.extend(cats)

    # Preserve original stack order for standalone categories
    order = {cat: i for i, cat in enumerate(level2)}
    standalone.sort(key=lambda c: order[c])

    return standalone, worn_plastic, worn_metal


def build_export_jobs(skin_pack_node):
    """
    Build the full ordered list of export jobs.

    Each job is a dict:
      {
        "filename": str,          # clean output filename (no extension)
        "show":     [node, ...],  # nodes to make visible for this export
        "all_in_category": [...], # all sibling nodes in same category (for hiding)
      }

    Also returns:
      all_skin_nodes      – flat list of every Level-3 skin node
      all_category_nodes  – flat list of every Level-2 category node
    """
    standalone, worn_plastic, worn_metal = categorize_level2(skin_pack_node)

    jobs               = []
    all_skin_nodes     = []
    all_category_nodes = []

    # ── Standalone categories (Normal, Bright, …) ─────────────────────────────
    for cat in standalone:
        all_category_nodes.append(cat)
        skins = _group_children(cat)
        all_skin_nodes.extend(skins)
        for skin in skins:
            jobs.append({
                "filename":         clean_skin_name(skin.get_name()),
                "show":             [skin],
                "all_in_category":  skins,   # used to hide siblings
            })

    # ── Worn pair ──────────────────────────────────────────────────────────────
    if worn_plastic and worn_metal:
        all_category_nodes.extend([worn_plastic, worn_metal])
        plastic_skins = _group_children(worn_plastic)
        metal_skins   = _group_children(worn_metal)
        all_skin_nodes.extend(plastic_skins)
        all_skin_nodes.extend(metal_skins)

        count = min(len(plastic_skins), len(metal_skins))
        all_worn = plastic_skins + metal_skins

        for i in range(count):
            p    = plastic_skins[i]
            m    = metal_skins[i]
            name = clean_skin_name(p.get_name())   # e.g. "Default Worn"
            jobs.append({
                "filename":         name,
                "show":             [p, m],
                "all_in_category":  all_worn,
            })

    return jobs, all_skin_nodes, all_category_nodes


# ── Visibility save / restore ─────────────────────────────────────────────────
def _save_vis(nodes):
    """Return {node: bool} snapshot for a list of nodes."""
    return {n: n.is_visible() for n in nodes}


def _restore_vis(state: dict):
    """Restore visibility from a snapshot dict."""
    for node, vis in state.items():
        node.set_visible(vis)


# ── Export config builder ─────────────────────────────────────────────────────
def build_export_config(ts_name, out_dir, filename, sp_format):
    """
    Build an export_project_textures config dict.
    Uses an inline preset (export.html 'exportPresets' section) so no
    external preset resource is required.
    Exports the base colour channel only.
    """
    bit_depth = "16f" if sp_format == "exr" else "8"
    return {
        "exportShaderParams": False,           # required bool – export.html
        "exportPath":         out_dir,
        "exportPresets": [
            {
                "name": "BatchPreset",
                "maps": [
                    {
                        "fileName": filename,  # static name → output file
                        "channels": [
                            # documentMap + baseColor – export.html channel spec
                            {"destChannel": "R", "srcChannel": "R",
                             "srcMapType": "documentMap", "srcMapName": "baseColor"},
                            {"destChannel": "G", "srcChannel": "G",
                             "srcMapType": "documentMap", "srcMapName": "baseColor"},
                            {"destChannel": "B", "srcChannel": "B",
                             "srcMapType": "documentMap", "srcMapName": "baseColor"},
                        ]
                    }
                ]
            }
        ],
        "defaultExportPreset": "BatchPreset",
        "exportList": [{"rootPath": ts_name}],  # ts.name is a property
        "exportParameters": [
            {
                "parameters": {
                    "fileFormat":       sp_format,
                    "bitDepth":         bit_depth,
                    "dithering":        False,
                    "paddingAlgorithm": "infinite",
                    "dilationDistance": 16,
                }
            }
        ]
    }


# =============================================================================
# ── Window ────────────────────────────────────────────────────────────────────
# =============================================================================
class TextureExporterWindow(QWidget):

    FORMATS = {
        "PNG":  ("png",  "png"),
        "JPG":  ("jpg",  "jpeg"),
        "TIFF": ("tiff", "tiff"),
        "EXR":  ("exr",  "exr"),
        "TGA":  ("tga",  "tga"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(500)
        self.setWindowFlags(Qt.Window)
        self._last_dir      = ""
        self._preset_data   = []
        self._batch_running = False
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        tabs = QTabWidget()
        tabs.addTab(self._build_single_tab(), "Single Export")
        tabs.addTab(self._build_batch_tab(),  "Batch Export")
        root.addWidget(tabs)

    # ── Tab 1: Single export ──────────────────────────────────────────────────
    def _build_single_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)
        lay.setContentsMargins(8, 8, 8, 8)

        # Preset
        preset_grp = QGroupBox("Output Template Preset")
        pg = QVBoxLayout(preset_grp)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Preset:"))
        self._s_preset = QComboBox()
        self._s_preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        pr.addWidget(self._s_preset)
        btn_rp = QPushButton("↺")
        btn_rp.setFixedWidth(28)
        btn_rp.clicked.connect(self._refresh_presets)
        pr.addWidget(btn_rp)
        pg.addLayout(pr)
        lay.addWidget(preset_grp)

        # Layer picker
        layer_grp = QGroupBox("Layer (output filename)")
        lg = QVBoxLayout(layer_grp)
        lr = QHBoxLayout()
        lr.addWidget(QLabel("Layer:"))
        self._s_layer = QComboBox()
        self._s_layer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lr.addWidget(self._s_layer)
        btn_rl = QPushButton("↺")
        btn_rl.setFixedWidth(28)
        btn_rl.clicked.connect(self._refresh_layers)
        lr.addWidget(btn_rl)
        lg.addLayout(lr)
        lay.addWidget(layer_grp)

        # Output
        out_grp = QGroupBox("Export Settings")
        og = QVBoxLayout(out_grp)
        og.setSpacing(6)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Output Folder:"))
        self._s_path = QLineEdit()
        self._s_path.setPlaceholderText("Browse…")
        self._s_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._s_path)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(68)
        btn_browse.clicked.connect(lambda: self._browse(self._s_path))
        path_row.addWidget(btn_browse)
        og.addLayout(path_row)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format:"))
        self._s_fmt = QComboBox()
        for lbl in self.FORMATS:
            self._s_fmt.addItem(lbl)
        self._s_fmt.setFixedWidth(80)
        fmt_row.addWidget(self._s_fmt)
        fmt_row.addStretch()
        og.addLayout(fmt_row)
        lay.addWidget(out_grp)

        btn_export = QPushButton("Export Texture")
        btn_export.setFixedHeight(28)
        btn_export.clicked.connect(self._single_export)
        lay.addWidget(btn_export)

        self._s_status = QLabel("")
        self._s_status.setWordWrap(True)
        self._s_status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._s_status)
        lay.addStretch()

        self._refresh_presets()
        self._refresh_layers()
        return w

    # ── Tab 2: Batch export ───────────────────────────────────────────────────
    def _build_batch_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)
        lay.setContentsMargins(8, 8, 8, 8)

        # Output settings
        out_grp = QGroupBox("Export Settings")
        og = QVBoxLayout(out_grp)
        og.setSpacing(6)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Output Folder:"))
        self._b_path = QLineEdit()
        self._b_path.setPlaceholderText("Browse…")
        self._b_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._b_path)
        btn_browse = QPushButton("Browse…")
        btn_browse.setFixedWidth(68)
        btn_browse.clicked.connect(lambda: self._browse(self._b_path))
        path_row.addWidget(btn_browse)
        og.addLayout(path_row)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Format:"))
        self._b_fmt = QComboBox()
        for lbl in self.FORMATS:
            self._b_fmt.addItem(lbl)
        self._b_fmt.setFixedWidth(80)
        fmt_row.addWidget(self._b_fmt)
        fmt_row.addStretch()
        og.addLayout(fmt_row)
        lay.addWidget(out_grp)

        # Structure preview
        struct_grp = QGroupBox("Detected Structure")
        sg = QVBoxLayout(struct_grp)
        self._b_struct_label = QLabel("Click 'Detect' to inspect the layer structure.")
        self._b_struct_label.setWordWrap(True)
        sg.addWidget(self._b_struct_label)
        btn_detect = QPushButton("Detect Skin Pack Structure")
        btn_detect.clicked.connect(self._detect_structure)
        sg.addWidget(btn_detect)
        lay.addWidget(struct_grp)

        # Progress
        prog_grp = QGroupBox("Progress")
        pg = QVBoxLayout(prog_grp)
        self._b_progress = QProgressBar()
        self._b_progress.setValue(0)
        self._b_progress.setFormat("%v / %m")
        pg.addWidget(self._b_progress)
        self._b_current = QLabel("")
        self._b_current.setAlignment(Qt.AlignCenter)
        pg.addWidget(self._b_current)
        lay.addWidget(prog_grp)

        # Log
        log_grp = QGroupBox("Log")
        lg = QVBoxLayout(log_grp)
        self._b_log = QTextEdit()
        self._b_log.setReadOnly(True)
        self._b_log.setMinimumHeight(120)
        self._b_log.setMaximumHeight(200)
        lg.addWidget(self._b_log)
        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedWidth(80)
        btn_clear.clicked.connect(self._b_log.clear)
        lg.addWidget(btn_clear, alignment=Qt.AlignRight)
        lay.addWidget(log_grp)

        # Run button
        self._b_run_btn = QPushButton("▶  Run Batch Export")
        self._b_run_btn.setFixedHeight(32)
        self._b_run_btn.setStyleSheet(
            "QPushButton           { background:#2d6a9f; color:white; font-weight:bold; }"
            "QPushButton:hover     { background:#3a82c4; }"
            "QPushButton:disabled  { background:#444; color:#888; }"
        )
        self._b_run_btn.clicked.connect(self._run_batch)
        lay.addWidget(self._b_run_btn)

        lay.addStretch()
        return w

    # ── Single export helpers ─────────────────────────────────────────────────
    def _refresh_presets(self):
        self._s_preset.blockSignals(True)
        self._s_preset.clear()
        self._preset_data.clear()

        self._s_preset.addItem(f"★ Base Color  [custom]")
        self._preset_data.append({"type": "inline"})

        if substance_painter.project.is_open():
            try:
                for p in substance_painter.export.list_predefined_export_presets():
                    self._s_preset.addItem(f"{p.name}  [predefined]")
                    self._preset_data.append({"type": "predefined", "url": p.url})
            except Exception as e:
                self._log_sp(f"Could not load predefined presets: {e}", warn=True)
            try:
                for p in substance_painter.export.list_resource_export_presets():
                    self._s_preset.addItem(f"{p.resource_id.name}  [shelf]")
                    self._preset_data.append({"type": "resource", "url": p.resource_id.url()})
            except Exception as e:
                self._log_sp(f"Could not load resource presets: {e}", warn=True)

        self._s_preset.blockSignals(False)
        self._s_preset.setCurrentIndex(0)

    def _refresh_layers(self):
        self._s_layer.clear()
        if not substance_painter.project.is_open():
            self._s_layer.addItem("(no project open)")
            return
        try:
            stack  = substance_painter.textureset.get_active_stack()
            roots  = substance_painter.layerstack.get_root_layer_nodes(stack)
            self._walk_layers(roots, depth=0)
        except Exception as e:
            self._s_layer.addItem(f"Error: {e}")

    def _walk_layers(self, nodes, depth):
        indent = "  " * depth
        for node in nodes:
            self._s_layer.addItem(f"{indent}{node.get_name()}", node)
            if isinstance(node, substance_painter.layerstack.GroupLayerNode):
                self._walk_layers(node.sub_layers(), depth + 1)

    def _browse(self, target_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Output Folder",
            target_edit.text().strip() or self._last_dir
        )
        if folder:
            folder = folder.replace("\\", "/")
            target_edit.setText(folder)
            self._last_dir = folder

    # ── Single export ─────────────────────────────────────────────────────────
    def _single_export(self):
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Please open a project first.")
            return
        out = self._s_path.text().strip().replace("\\", "/")
        if not out:
            QMessageBox.warning(self, "No Folder", "Please choose an output folder.")
            return

        ts_list = substance_painter.textureset.all_texture_sets()
        if not ts_list:
            QMessageBox.warning(self, "No Texture Sets", "No texture sets found.")
            return
        ts = ts_list[0]

        layer_name = self._s_layer.currentText().strip()
        sp_fmt     = self.FORMATS[self._s_fmt.currentText()][1]

        preset_idx  = self._s_preset.currentIndex()
        preset_info = self._preset_data[preset_idx] if preset_idx < len(self._preset_data) else {"type": "inline"}

        config = {
            "exportShaderParams": False,
            "exportPath":         out,
            "exportList":         [{"rootPath": ts.name}],
            "exportParameters":   [{"parameters": {
                "fileFormat":       sp_fmt,
                "bitDepth":         "16f" if sp_fmt == "exr" else "8",
                "dithering":        False,
                "paddingAlgorithm": "infinite",
                "dilationDistance": 16,
            }}]
        }
        if preset_info["type"] == "inline":
            config["exportPresets"]       = [{"name": "SinglePreset", "maps": [{
                "fileName": layer_name,
                "channels": [
                    {"destChannel": "R", "srcChannel": "R", "srcMapType": "documentMap", "srcMapName": "baseColor"},
                    {"destChannel": "G", "srcChannel": "G", "srcMapType": "documentMap", "srcMapName": "baseColor"},
                    {"destChannel": "B", "srcChannel": "B", "srcMapType": "documentMap", "srcMapName": "baseColor"},
                ]
            }]}]
            config["defaultExportPreset"] = "SinglePreset"
        else:
            config["defaultExportPreset"] = preset_info["url"]

        try:
            result = substance_painter.export.export_project_textures(config)
            if result.status == substance_painter.export.ExportStatus.Success:
                files = [p for paths in result.textures.values() for p in paths]
                self._s_status.setText(f"✅ Exported {len(files)} file(s) → {out}")
            else:
                self._s_status.setText(f"❌ {result.message}")
                QMessageBox.critical(self, "Export Failed", result.message)
        except Exception as e:
            self._s_status.setText(f"❌ {e}")
            QMessageBox.critical(self, "Error", str(e))

    # ── Batch: detect structure ───────────────────────────────────────────────
    def _detect_structure(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open.")
            return
        try:
            stack = substance_painter.textureset.get_active_stack()
            root  = find_skin_pack_root(stack)
            if root is None:
                self._b_struct_label.setText(
                    "❌ Could not detect a 3-level skin pack structure.\n"
                    "Ensure a group exists at root level whose children are "
                    "category groups containing skin groups."
                )
                return

            standalone, worn_p, worn_m = categorize_level2(root)

            lines = [f"✅ Skin Pack Root: \"{root.get_name()}\"", ""]
            for cat in standalone:
                skins = _group_children(cat)
                lines.append(f"  [{cat.get_name()}]  →  {len(skins)} skins")
            if worn_p and worn_m:
                pc = _group_children(worn_p)
                mc = _group_children(worn_m)
                lines.append(f"  [{worn_p.get_name()}] + [{worn_m.get_name()}]  →  "
                             f"{min(len(pc), len(mc))} worn pairs")

            jobs, _, _ = build_export_jobs(root)
            lines.append("")
            lines.append(f"Total exports: {len(jobs)}")
            self._b_struct_label.setText("\n".join(lines))

        except Exception as e:
            self._b_struct_label.setText(f"❌ Error: {e}")

    # ── Batch: run ────────────────────────────────────────────────────────────
    def _run_batch(self):
        if self._batch_running:
            return

        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Please open a project first.")
            return

        out = self._b_path.text().strip().replace("\\", "/")
        if not out:
            QMessageBox.warning(self, "No Folder", "Please choose an output folder.")
            return

        ts_list = substance_painter.textureset.all_texture_sets()
        if not ts_list:
            QMessageBox.warning(self, "No Texture Sets", "No texture sets found.")
            return
        ts = ts_list[0]

        try:
            stack = substance_painter.textureset.get_active_stack()
            root  = find_skin_pack_root(stack)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            return

        if root is None:
            QMessageBox.critical(
                self, "Structure Not Found",
                "Could not detect the 3-level skin pack structure.\n"
                "Run 'Detect Skin Pack Structure' for details."
            )
            return

        jobs, all_skin_nodes, all_category_nodes = build_export_jobs(root)
        if not jobs:
            QMessageBox.warning(self, "No Jobs", "No export jobs were found.")
            return

        sp_fmt = self.FORMATS[self._b_fmt.currentText()][1]
        ext    = self.FORMATS[self._b_fmt.currentText()][0]

        # Confirm
        reply = QMessageBox.question(
            self, "Run Batch Export",
            f"Export {len(jobs)} textures as {ext.upper()} to:\n{out}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # ── Set up progress ───────────────────────────────────────────────────
        self._batch_running = True
        self._b_run_btn.setEnabled(False)
        self._b_progress.setMaximum(len(jobs))
        self._b_progress.setValue(0)
        self._b_log.clear()
        self._b_current.setText("")

        def _log(msg: str):
            self._b_log.append(msg)
            substance_painter.logging.log(
                substance_painter.logging.INFO, "TextureExporter", msg
            )

        def _tick(done: int, total: int, name: str):
            self._b_progress.setValue(done)
            self._b_current.setText(f"Exporting: {name}  ({done}/{total})")
            QCoreApplication.processEvents()   # keep UI live between exports

        # ── Save visibility of everything involved ────────────────────────────
        vis_state = {}
        vis_state[root] = root.is_visible()
        for n in all_category_nodes:
            vis_state[n] = n.is_visible()
        for n in all_skin_nodes:
            vis_state[n] = n.is_visible()

        errors = []
        try:
            # Ensure skin pack root + all categories are visible
            root.set_visible(True)
            for n in all_category_nodes:
                n.set_visible(True)

            # Hide every skin node to start from a clean slate
            for n in all_skin_nodes:
                n.set_visible(False)

            # ── Execute each job ──────────────────────────────────────────────
            for idx, job in enumerate(jobs):
                filename = job["filename"]
                _tick(idx, len(jobs), filename)

                # Show only this job's nodes
                for n in job["show"]:
                    n.set_visible(True)

                config = build_export_config(ts.name, out, filename, sp_fmt)

                try:
                    result = substance_painter.export.export_project_textures(config)
                    if result.status == substance_painter.export.ExportStatus.Success:
                        files = [p for paths in result.textures.values() for p in paths]
                        _log(f"✅  {filename}.{ext}")
                    else:
                        _log(f"❌  {filename}: {result.message}")
                        errors.append(filename)
                except Exception as e:
                    _log(f"❌  {filename}: {e}")
                    errors.append(filename)

                # Hide again before next job
                for n in job["show"]:
                    n.set_visible(False)

            _tick(len(jobs), len(jobs), "Done")

        finally:
            # ── Always restore original visibility ────────────────────────────
            _restore_vis(vis_state)
            self._batch_running = False
            self._b_run_btn.setEnabled(True)

        # ── Summary ───────────────────────────────────────────────────────────
        ok_count  = len(jobs) - len(errors)
        self._b_current.setText(
            f"Complete — {ok_count}/{len(jobs)} succeeded"
            + (f", {len(errors)} failed" if errors else "")
        )
        if errors:
            QMessageBox.warning(
                self, "Batch Complete with Errors",
                f"{ok_count} exported successfully.\n"
                f"Failed: {', '.join(errors)}"
            )
        else:
            QMessageBox.information(
                self, "Batch Complete",
                f"All {ok_count} textures exported to:\n{out}"
            )

    # ── SP logging helper ─────────────────────────────────────────────────────
    def _log_sp(self, msg: str, warn: bool = False):
        level = substance_painter.logging.WARNING if warn else substance_painter.logging.INFO
        substance_painter.logging.log(level, "TextureExporter", msg)

    # ── Window close → hide (preserves state) ─────────────────────────────────
    def closeEvent(self, event):
        event.ignore()
        self.hide()


# =============================================================================
# ── Plugin hooks ──────────────────────────────────────────────────────────────
# =============================================================================
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
        _window._refresh_presets()
        _window._refresh_layers()


if __name__ == "__main__":
    start_plugin()