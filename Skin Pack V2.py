"""
Texture Exporter – Phase 1 (Single) + Phase 2 (Batch) + Phase 3 (Contextual Layers)
Substance Painter 11.1.2  |  PySide6

API sources (uploaded docs only):
  ui.html         → ApplicationMenu.Window, add_action, get_main_window
  export.html     → export_project_textures, exportShaderParams, exportPresets,
                    exportList, exportParameters, ExportStatus,
                    list_predefined_export_presets, list_resource_export_presets
  textureset.html → TextureSet.name (property), get_active_stack, all_texture_sets
  navigation.html → get_root_layer_nodes, GroupLayerNode.sub_layers(),
                    Node.get_name(), Node.is_visible(), Node.set_visible(),
                    LayerNode.content_effects(), LayerNode.mask_effects()
  filter.html     → FilterEffectNode (inherits Node → is_visible/set_visible)
  fill.html       → FillLayerNode (inherits LayerNode)

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
    QSizePolicy, QProgressBar, QTextEdit, QTabWidget, QScrollArea,
)
from PySide6.QtCore import Qt, QCoreApplication

# ─────────────────────────────────────────────────────────────────────────────
_plugin_widgets = []
_window         = None

sp_ls = substance_painter.layerstack   # shorthand


# =============================================================================
# ── Helpers ───────────────────────────────────────────────────────────────────
# =============================================================================

def clean_skin_name(raw: str) -> str:
    """
    Convert a raw SP layer name to a clean export filename.
      1. Strip leading '!' characters
      2. Strip leading 'Skin ' prefix (case-insensitive)
      3. Strip trailing parenthetical e.g. '(Any Mat)', '(Metal)', '(Plastic)'
      4. Replace 'Black' → 'Default'
    """
    s = raw.lstrip('!')
    s = re.sub(r'^[Ss]kin\s+', '', s).strip()
    s = re.sub(r'\s*\(.*', '', s).strip()
    s = re.sub(r'\bBlack\b', 'Default', s)
    return s.strip()


# ── Layer-type predicates ─────────────────────────────────────────────────────
def _is_group(node):
    return isinstance(node, sp_ls.GroupLayerNode)

def _is_fill(node):
    return isinstance(node, sp_ls.FillLayerNode)

def _is_paint_effect(node):
    return isinstance(node, sp_ls.PaintEffectNode)

def _is_filter_effect(node):
    return isinstance(node, sp_ls.FilterEffectNode)

def _is_proper_layer(node):
    """True for layer nodes that make sense as export targets (not effect nodes)."""
    return isinstance(node, (
        sp_ls.GroupLayerNode,
        sp_ls.PaintLayerNode,
        sp_ls.FillLayerNode,
        sp_ls.InstanceLayerNode,
    ))


def _group_children(node):
    """Direct GroupLayerNode children of a group."""
    return [c for c in node.sub_layers() if _is_group(c)]


# ── Full-stack walks ──────────────────────────────────────────────────────────
def _walk_all_nodes(stack):
    """
    Flat list of ALL proper layer nodes (no effects) in the stack.
    Returns [(depth, node), ...]
    """
    results = []

    def _recurse(nodes, depth):
        for node in nodes:
            if _is_proper_layer(node):
                results.append((depth, node))
            if _is_group(node):
                _recurse(node.sub_layers(), depth + 1)

    _recurse(sp_ls.get_root_layer_nodes(stack), 0)
    return results


def _walk_fill_layers(stack):
    return [(d, n) for d, n in _walk_all_nodes(stack) if _is_fill(n)]


def _walk_group_layers(stack):
    return [(d, n) for d, n in _walk_all_nodes(stack) if _is_group(n)]


# ── Skin-pack structure ────────────────────────────────────────────────────────
def find_skin_pack_root(stack):
    """
    Detect the 3-level skin pack root group purely by structure:
      Level 1: any GroupLayerNode at root
      Level 2: >= 3 children that are ALL GroupLayerNodes
               (minimum: Normal + Worn Plastic + Worn Metal)
      Level 3: each Level-2 child has at least one GroupLayerNode child

    Requiring >= 3 Level-2 children prevents false matches on smaller utility
    groups like a 'Black Parts' folder that only has 2 children.
    """
    for node in sp_ls.get_root_layer_nodes(stack):
        if not _is_group(node):
            continue
        level2 = _group_children(node)
        if len(level2) < 3:
            continue
        if all(len(_group_children(cat)) > 0 for cat in level2):
            return node
    return None


def categorize_level2(skin_pack_node):
    """
    Separate Level-2 groups into:
      • worn_pair  – exactly two groups with matching Level-3 child counts
      • normal_cat – the remaining standalone with the MOST Level-3 children
      • bright_cats – all other standalone groups

    Returns: (normal_cat, bright_cats, worn_plastic_node, worn_metal_node)
    Any may be None if not detected.
    """
    level2 = _group_children(skin_pack_node)

    by_count = {}
    for cat in level2:
        n = len(_group_children(cat))
        by_count.setdefault(n, []).append(cat)

    worn_plastic = None
    worn_metal   = None
    standalone   = []

    for n, cats in by_count.items():
        if len(cats) == 2:
            a, b   = cats[0], cats[1]
            a_name = a.get_name().lower()
            b_name = b.get_name().lower()
            if 'plastic' in a_name or 'metal' in b_name:
                worn_plastic, worn_metal = a, b
            elif 'metal' in a_name or 'plastic' in b_name:
                worn_plastic, worn_metal = b, a
            else:
                idx_a = level2.index(a)
                idx_b = level2.index(b)
                worn_plastic, worn_metal = (a, b) if idx_a < idx_b else (b, a)
        else:
            standalone.extend(cats)

    order = {cat: i for i, cat in enumerate(level2)}
    standalone.sort(key=lambda c: order[c])

    normal_cat  = None
    bright_cats = []
    if standalone:
        normal_cat  = max(standalone, key=lambda c: len(_group_children(c)))
        bright_cats = [c for c in standalone if c is not normal_cat]

    return normal_cat, bright_cats, worn_plastic, worn_metal


def build_export_jobs(skin_pack_node):
    """
    Build ordered list of export jobs.
    Each job dict:
      {
        "filename":        str,
        "show":            [node, ...],
        "all_in_category": [node, ...],
        "cat_type":        "normal" | "worn" | "bright",
      }
    """
    normal_cat, bright_cats, worn_plastic, worn_metal = categorize_level2(skin_pack_node)

    jobs               = []
    all_skin_nodes     = []
    all_category_nodes = []

    def _add_standalone(cat, cat_type):
        all_category_nodes.append(cat)
        skins = _group_children(cat)
        all_skin_nodes.extend(skins)
        for skin in skins:
            jobs.append({
                "filename":        clean_skin_name(skin.get_name()),
                "show":            [skin],
                "all_in_category": skins,
                "cat_type":        cat_type,
            })

    if normal_cat:
        _add_standalone(normal_cat, "normal")

    if worn_plastic and worn_metal:
        all_category_nodes.extend([worn_plastic, worn_metal])
        plastic_skins = _group_children(worn_plastic)
        metal_skins   = _group_children(worn_metal)
        all_skin_nodes.extend(plastic_skins)
        all_skin_nodes.extend(metal_skins)
        count    = min(len(plastic_skins), len(metal_skins))
        all_worn = plastic_skins + metal_skins
        for i in range(count):
            jobs.append({
                "filename":        clean_skin_name(plastic_skins[i].get_name()),
                "show":            [plastic_skins[i], metal_skins[i]],
                "all_in_category": all_worn,
                "cat_type":        "worn",
            })

    for cat in bright_cats:
        _add_standalone(cat, "bright")

    return jobs, all_skin_nodes, all_category_nodes


# ── Visibility save/restore ───────────────────────────────────────────────────
def _save_vis(nodes):
    return {n: n.is_visible() for n in nodes if n is not None}

def _restore_vis(state: dict):
    for node, vis in state.items():
        try:
            node.set_visible(vis)
        except Exception:
            pass


# ── Export config ─────────────────────────────────────────────────────────────
def build_export_config(ts_name, out_dir, filename, sp_format):
    bit_depth = "16f" if sp_format == "exr" else "8"
    return {
        "exportShaderParams": False,
        "exportPath":         out_dir,
        "exportPresets": [
            {
                "name": "BatchPreset",
                "maps": [
                    {
                        "fileName": filename,
                        "channels": [
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
        "exportList": [{"rootPath": ts_name}],
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
        self.setMinimumWidth(520)
        self.setWindowFlags(Qt.Window)
        self._last_dir      = ""
        self._preset_data   = []
        self._batch_running = False
        self._build_ui()

    # ── Top-level ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        tabs = QTabWidget()
        tabs.addTab(self._build_single_tab(), "Single Export")
        tabs.addTab(self._build_batch_tab(),  "Batch Export")
        root.addWidget(tabs)

    # =========================================================================
    # ── Tab 1: Single Export ──────────────────────────────────────────────────
    # =========================================================================
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

        # Layer picker — GroupLayerNode only (proper export targets)
        layer_grp = QGroupBox("Layer (output filename)")
        lg = QVBoxLayout(layer_grp)
        lr = QHBoxLayout()
        lr.addWidget(QLabel("Layer:"))
        self._s_layer = QComboBox()
        self._s_layer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lr.addWidget(self._s_layer)
        btn_rl = QPushButton("↺")
        btn_rl.setFixedWidth(28)
        btn_rl.clicked.connect(self._refresh_single_layers)
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
        btn_b = QPushButton("Browse…")
        btn_b.setFixedWidth(68)
        btn_b.clicked.connect(lambda: self._browse(self._s_path))
        path_row.addWidget(btn_b)
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
        self._refresh_single_layers()
        return w

    # =========================================================================
    # ── Tab 2: Batch Export ───────────────────────────────────────────────────
    # =========================================================================
    def _build_batch_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(8)
        lay.setContentsMargins(8, 8, 8, 8)

        # ── Export settings ───────────────────────────────────────────────────
        out_grp = QGroupBox("Export Settings")
        og = QVBoxLayout(out_grp)
        og.setSpacing(6)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Output Folder:"))
        self._b_path = QLineEdit()
        self._b_path.setPlaceholderText("Browse…")
        self._b_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._b_path)
        btn_b = QPushButton("Browse…")
        btn_b.setFixedWidth(68)
        btn_b.clicked.connect(lambda: self._browse(self._b_path))
        path_row.addWidget(btn_b)
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

        # ── Contextual Layers (Block 3) ───────────────────────────────────────
        ctx_grp = QGroupBox("Contextual Layers  (all optional)")
        cg = QVBoxLayout(ctx_grp)
        cg.setSpacing(6)

        note = QLabel(
            "Toggled automatically per category during batch export.\n"
            "Leave any picker on '— none —' to skip that override."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #aaa; font-size: 11px;")
        cg.addWidget(note)

        refresh_btn = QPushButton("↺  Refresh All Layer Pickers")
        refresh_btn.clicked.connect(self._refresh_ctx_pickers)
        cg.addWidget(refresh_btn)

        LBL_W = 200

        def _row(label_text, attr_name, tooltip=""):
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(LBL_W)
            row.addWidget(lbl)
            combo = QComboBox()
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if tooltip:
                combo.setToolTip(tooltip)
            setattr(self, attr_name, combo)
            row.addWidget(combo)
            cg.addLayout(row)

        _row("Text Fill Layer:",
             "_ctx_fill",
             "The FillLayerNode containing your text/markings.\n"
             "Its first FilterEffectNode in content_effects() will be toggled as the Invert filter.")

        _row("Worn Paint Layer:",
             "_ctx_worn_paint",
             "The PaintEffectNode inside the text fill layer's black mask.\n"
             "Populated automatically after selecting a Text Fill Layer.\n"
             "ON for Worn exports, OFF for Normal/Bright.")

        _row("Black Parts Group:",
             "_ctx_bp_group",
             "GroupLayerNode containing your metal parts layers.")

        _row("Black Parts — Non-Worn:",
             "_ctx_bp_nonworn",
             "Child layer to show during Normal and Bright exports.")

        _row("Black Parts — Worn:",
             "_ctx_bp_worn",
             "Child layer to show during Worn exports.")

        # Cascading refresh wires
        self._ctx_fill.currentIndexChanged.connect(self._on_fill_changed)
        self._ctx_bp_group.currentIndexChanged.connect(self._on_bp_changed)

        lay.addWidget(ctx_grp)

        # ── Structure ─────────────────────────────────────────────────────────
        struct_grp = QGroupBox("Detected Skin Pack Structure")
        sg = QVBoxLayout(struct_grp)
        self._b_struct_label = QLabel("Click 'Detect' to inspect the layer structure.")
        self._b_struct_label.setWordWrap(True)
        sg.addWidget(self._b_struct_label)
        btn_detect = QPushButton("Detect Skin Pack Structure")
        btn_detect.clicked.connect(self._detect_structure)
        sg.addWidget(btn_detect)
        lay.addWidget(struct_grp)

        # ── Progress ──────────────────────────────────────────────────────────
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

        # ── Log ───────────────────────────────────────────────────────────────
        log_grp = QGroupBox("Log")
        lg = QVBoxLayout(log_grp)
        self._b_log = QTextEdit()
        self._b_log.setReadOnly(True)
        self._b_log.setMinimumHeight(100)
        self._b_log.setMaximumHeight(180)
        lg.addWidget(self._b_log)
        btn_clear = QPushButton("Clear Log")
        btn_clear.setFixedWidth(80)
        btn_clear.clicked.connect(self._b_log.clear)
        lg.addWidget(btn_clear, alignment=Qt.AlignRight)
        lay.addWidget(log_grp)

        # ── Run ───────────────────────────────────────────────────────────────
        self._b_run_btn = QPushButton("▶  Run Batch Export")
        self._b_run_btn.setFixedHeight(32)
        self._b_run_btn.setStyleSheet(
            "QPushButton          { background:#2d6a9f; color:white; font-weight:bold; }"
            "QPushButton:hover    { background:#3a82c4; }"
            "QPushButton:disabled { background:#444;    color:#888;  }"
        )
        self._b_run_btn.clicked.connect(self._run_batch)
        lay.addWidget(self._b_run_btn)
        lay.addStretch()

        scroll.setWidget(w)
        return scroll

    # =========================================================================
    # ── Single export helpers ─────────────────────────────────────────────────
    # =========================================================================
    def _refresh_presets(self):
        self._s_preset.blockSignals(True)
        self._s_preset.clear()
        self._preset_data.clear()

        self._s_preset.addItem("★ Base Color  [custom]")
        self._preset_data.append({"type": "inline"})

        if substance_painter.project.is_open():
            try:
                for p in substance_painter.export.list_predefined_export_presets():
                    self._s_preset.addItem(f"{p.name}  [predefined]")
                    self._preset_data.append({"type": "predefined", "url": p.url})
            except Exception as e:
                self._sp_log(f"Could not load predefined presets: {e}", warn=True)
            try:
                for p in substance_painter.export.list_resource_export_presets():
                    self._s_preset.addItem(f"{p.resource_id.name}  [shelf]")
                    self._preset_data.append({"type": "resource",
                                               "url": p.resource_id.url()})
            except Exception as e:
                self._sp_log(f"Could not load resource presets: {e}", warn=True)

        self._s_preset.blockSignals(False)
        self._s_preset.setCurrentIndex(0)

    def _refresh_single_layers(self):
        """
        GroupLayerNode only — excludes fill layers, paint effects, and other
        utility nodes that are not meaningful export filename sources.
        """
        self._s_layer.clear()
        if not substance_painter.project.is_open():
            self._s_layer.addItem("(no project open)")
            return
        try:
            stack = substance_painter.textureset.get_active_stack()
            items = _walk_group_layers(stack)
            if not items:
                self._s_layer.addItem("(no group layers found)")
                return
            for depth, node in items:
                self._s_layer.addItem("  " * depth + node.get_name(), node)
        except Exception as e:
            self._s_layer.addItem(f"Error: {e}")

    # =========================================================================
    # ── Contextual layer pickers ──────────────────────────────────────────────
    # =========================================================================
    def _refresh_ctx_pickers(self):
        if not substance_painter.project.is_open():
            return
        try:
            stack = substance_painter.textureset.get_active_stack()
            self._populate_fill_picker(stack)
            self._populate_bp_group_picker(stack)
        except Exception as e:
            self._sp_log(f"Ctx picker refresh error: {e}", warn=True)

    def _populate_fill_picker(self, stack):
        self._ctx_fill.blockSignals(True)
        self._ctx_fill.clear()
        self._ctx_fill.addItem("— none —", None)
        try:
            for depth, node in _walk_fill_layers(stack):
                self._ctx_fill.addItem("  " * depth + node.get_name(), node)
        except Exception as e:
            self._sp_log(f"Fill picker error: {e}", warn=True)
        self._ctx_fill.blockSignals(False)
        self._ctx_fill.setCurrentIndex(0)
        self._populate_worn_paint_picker()

    def _populate_worn_paint_picker(self):
        """
        Show PaintEffectNode items from the selected fill layer's mask_effects().
        mask_effects() → navigation.html LayerNode.mask_effects()
        PaintEffectNode inherits Node → is_visible / set_visible
        """
        self._ctx_worn_paint.blockSignals(True)
        self._ctx_worn_paint.clear()
        self._ctx_worn_paint.addItem("— none —", None)
        fill_node = self._ctx_fill.currentData()
        if fill_node is not None:
            try:
                for effect in fill_node.mask_effects():
                    if _is_paint_effect(effect):
                        self._ctx_worn_paint.addItem(effect.get_name(), effect)
            except Exception as e:
                self._sp_log(f"Worn paint picker error: {e}", warn=True)
        self._ctx_worn_paint.blockSignals(False)
        self._ctx_worn_paint.setCurrentIndex(0)

    def _populate_bp_group_picker(self, stack):
        self._ctx_bp_group.blockSignals(True)
        self._ctx_bp_group.clear()
        self._ctx_bp_group.addItem("— none —", None)
        try:
            for depth, node in _walk_group_layers(stack):
                self._ctx_bp_group.addItem("  " * depth + node.get_name(), node)
        except Exception as e:
            self._sp_log(f"BP group picker error: {e}", warn=True)
        self._ctx_bp_group.blockSignals(False)
        self._ctx_bp_group.setCurrentIndex(0)
        self._populate_bp_child_pickers()

    def _populate_bp_child_pickers(self):
        """Direct proper-layer children of the selected Black Parts group."""
        for combo in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— none —", None)

        bp_group = self._ctx_bp_group.currentData()
        if bp_group is not None:
            try:
                children = [c for c in bp_group.sub_layers()
                            if _is_proper_layer(c)]
                for child in children:
                    for combo in (self._ctx_bp_nonworn, self._ctx_bp_worn):
                        combo.addItem(child.get_name(), child)
            except Exception as e:
                self._sp_log(f"BP child picker error: {e}", warn=True)

        for combo in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            combo.blockSignals(False)
            combo.setCurrentIndex(0)

    def _on_fill_changed(self, _):
        self._populate_worn_paint_picker()

    def _on_bp_changed(self, _):
        self._populate_bp_child_pickers()

    def _ctx_nodes(self):
        """
        Resolve all contextual node selections.
        Finds the first FilterEffectNode in fill_node.content_effects() — that
        is the Invert filter.  content_effects() → navigation.html.
        FilterEffectNode inherits Node.set_visible() → filter.html.
        """
        fill_node  = self._ctx_fill.currentData()
        worn_paint = self._ctx_worn_paint.currentData()
        bp_group   = self._ctx_bp_group.currentData()
        bp_nonworn = self._ctx_bp_nonworn.currentData()
        bp_worn    = self._ctx_bp_worn.currentData()

        invert_filter = None
        if fill_node is not None:
            try:
                for fx in fill_node.content_effects():
                    if _is_filter_effect(fx):
                        invert_filter = fx
                        break
            except Exception:
                pass

        return {
            "fill_node":     fill_node,
            "invert_filter": invert_filter,
            "worn_paint":    worn_paint,
            "bp_group":      bp_group,
            "bp_nonworn":    bp_nonworn,
            "bp_worn":       bp_worn,
        }

    def _apply_ctx_state(self, cat_type: str, ctx: dict):
        """
        Apply contextual layer state per category type.

        Normal : invert_filter=ON,  worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF
        Worn   : invert_filter=ON,  worn_paint=ON,  bp_nonworn=OFF, bp_worn=ON
        Bright : invert_filter=OFF, worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF

        Uses Node.set_visible() which is inherited by FilterEffectNode
        (filter.html) and PaintEffectNode (navigation.html).
        All nodes are optional — None entries are silently skipped.
        """
        filter_on     = cat_type in ("normal", "worn")
        worn_paint_on = (cat_type == "worn")
        nonworn_on    = cat_type in ("normal", "bright")

        def _safe(node, visible):
            if node is not None:
                try:
                    node.set_visible(visible)
                except Exception as e:
                    self._sp_log(
                        f"set_visible({visible}) failed on "
                        f"'{node.get_name()}': {e}", warn=True
                    )

        _safe(ctx["invert_filter"], filter_on)
        _safe(ctx["worn_paint"],    worn_paint_on)
        _safe(ctx["bp_nonworn"],    nonworn_on)
        _safe(ctx["bp_worn"],       not nonworn_on)

    # =========================================================================
    # ── Shared helpers ────────────────────────────────────────────────────────
    # =========================================================================
    def _browse(self, target: QLineEdit):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Output Folder",
            target.text().strip() or self._last_dir
        )
        if folder:
            folder = folder.replace("\\", "/")
            target.setText(folder)
            self._last_dir = folder

    def _sp_log(self, msg: str, warn: bool = False):
        level = substance_painter.logging.WARNING if warn \
                else substance_painter.logging.INFO
        substance_painter.logging.log(level, "TextureExporter", msg)

    # =========================================================================
    # ── Single export ─────────────────────────────────────────────────────────
    # =========================================================================
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

        idx         = self._s_preset.currentIndex()
        preset_info = self._preset_data[idx] if idx < len(self._preset_data) \
                      else {"type": "inline"}

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
            config["exportPresets"] = [{"name": "SinglePreset", "maps": [{
                "fileName": layer_name,
                "channels": [
                    {"destChannel": "R", "srcChannel": "R",
                     "srcMapType": "documentMap", "srcMapName": "baseColor"},
                    {"destChannel": "G", "srcChannel": "G",
                     "srcMapType": "documentMap", "srcMapName": "baseColor"},
                    {"destChannel": "B", "srcChannel": "B",
                     "srcMapType": "documentMap", "srcMapName": "baseColor"},
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

    # =========================================================================
    # ── Batch: detect ─────────────────────────────────────────────────────────
    # =========================================================================
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
                    "Ensure a group at root level whose children are category "
                    "groups each containing skin groups."
                )
                return

            normal_cat, bright_cats, worn_p, worn_m = categorize_level2(root)
            lines = [f"✅ Skin Pack Root: \"{root.get_name()}\"", ""]

            if normal_cat:
                lines.append(
                    f"  [Normal]  \"{normal_cat.get_name()}\"  "
                    f"→  {len(_group_children(normal_cat))} skins"
                )
            if worn_p and worn_m:
                pc, mc = _group_children(worn_p), _group_children(worn_m)
                lines.append(
                    f"  [Worn]    \"{worn_p.get_name()}\" + \"{worn_m.get_name()}\"  "
                    f"→  {min(len(pc), len(mc))} pairs"
                )
            for cat in bright_cats:
                lines.append(
                    f"  [Bright]  \"{cat.get_name()}\"  "
                    f"→  {len(_group_children(cat))} skins"
                )

            jobs, _, _ = build_export_jobs(root)
            lines += ["", f"Total exports: {len(jobs)}"]
            self._b_struct_label.setText("\n".join(lines))

        except Exception as e:
            self._b_struct_label.setText(f"❌ Error: {e}")

    # =========================================================================
    # ── Batch: run ────────────────────────────────────────────────────────────
    # =========================================================================
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
            QMessageBox.critical(self, "Structure Not Found",
                                 "Could not detect the 3-level skin pack structure.\n"
                                 "Run 'Detect Skin Pack Structure' for details.")
            return

        jobs, all_skin_nodes, all_cat_nodes = build_export_jobs(root)
        if not jobs:
            QMessageBox.warning(self, "No Jobs", "No export jobs found.")
            return

        sp_fmt = self.FORMATS[self._b_fmt.currentText()][1]
        ext    = self.FORMATS[self._b_fmt.currentText()][0]

        ctx = self._ctx_nodes()

        # Confirm dialog
        ctx_lines = []
        if ctx["fill_node"]:
            ctx_lines.append(f"  Text fill  : {ctx['fill_node'].get_name()}")
        if ctx["invert_filter"]:
            ctx_lines.append(f"  Inv. filter: found in content_effects()")
        if ctx["worn_paint"]:
            ctx_lines.append(f"  Worn paint : {ctx['worn_paint'].get_name()}")
        if ctx["bp_group"]:
            ctx_lines.append(f"  Black parts: {ctx['bp_group'].get_name()}")
        ctx_block = ("\n\nContextual layers:\n" + "\n".join(ctx_lines)) \
                    if ctx_lines else "\n\n(No contextual layers configured)"

        reply = QMessageBox.question(
            self, "Run Batch Export",
            f"Export {len(jobs)} textures as {ext.upper()} to:\n{out}"
            f"{ctx_block}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # ── Setup ─────────────────────────────────────────────────────────────
        self._batch_running = True
        self._b_run_btn.setEnabled(False)
        self._b_progress.setMaximum(len(jobs))
        self._b_progress.setValue(0)
        self._b_log.clear()
        self._b_current.setText("")

        def _log(msg):
            self._b_log.append(msg)
            self._sp_log(msg)

        def _tick(done, name):
            self._b_progress.setValue(done)
            self._b_current.setText(f"Exporting: {name}  ({done}/{len(jobs)})")
            QCoreApplication.processEvents()

        # ── Snapshot visibility of everything we'll touch ─────────────────────
        vis_nodes = (
            [root]
            + all_cat_nodes
            + all_skin_nodes
            + [n for n in [
                ctx["fill_node"],
                ctx["invert_filter"],
                ctx["worn_paint"],
                ctx["bp_group"],
                ctx["bp_nonworn"],
                ctx["bp_worn"],
               ] if n is not None]
        )
        vis_state = _save_vis(vis_nodes)

        errors = []
        try:
            # Root + all category groups must be visible
            root.set_visible(True)
            for n in all_cat_nodes:
                n.set_visible(True)
            # Fill layer and Black Parts group stay visible; we only toggle
            # their children/effects
            if ctx["fill_node"]:
                ctx["fill_node"].set_visible(True)
            if ctx["bp_group"]:
                ctx["bp_group"].set_visible(True)

            # Hide all skin nodes to start clean
            for n in all_skin_nodes:
                n.set_visible(False)

            # ── Job loop ──────────────────────────────────────────────────────
            for idx, job in enumerate(jobs):
                filename = job["filename"]
                cat_type = job["cat_type"]
                _tick(idx, filename)

                # Apply contextual states for this category type
                self._apply_ctx_state(cat_type, ctx)

                # Reveal only this job's skin nodes
                for n in job["show"]:
                    n.set_visible(True)

                config = build_export_config(ts.name, out, filename, sp_fmt)
                try:
                    result = substance_painter.export.export_project_textures(config)
                    if result.status == substance_painter.export.ExportStatus.Success:
                        _log(f"✅  [{cat_type:6}]  {filename}.{ext}")
                    else:
                        _log(f"❌  [{cat_type:6}]  {filename}: {result.message}")
                        errors.append(filename)
                except Exception as e:
                    _log(f"❌  [{cat_type:6}]  {filename}: {e}")
                    errors.append(filename)

                # Hide again before next job
                for n in job["show"]:
                    n.set_visible(False)

            _tick(len(jobs), "Done")

        finally:
            # Always restore original visibility
            _restore_vis(vis_state)
            self._batch_running = False
            self._b_run_btn.setEnabled(True)

        # ── Summary ───────────────────────────────────────────────────────────
        ok = len(jobs) - len(errors)
        self._b_current.setText(
            f"Complete — {ok}/{len(jobs)} succeeded"
            + (f", {len(errors)} failed" if errors else "")
        )
        if errors:
            QMessageBox.warning(self, "Batch Complete with Errors",
                                f"{ok} exported.\nFailed: {', '.join(errors)}")
        else:
            QMessageBox.information(self, "Batch Complete",
                                    f"All {ok} textures exported to:\n{out}")

    # ── Window close → hide ───────────────────────────────────────────────────
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
        _window._refresh_single_layers()
        _window._refresh_ctx_pickers()


if __name__ == "__main__":
    start_plugin()