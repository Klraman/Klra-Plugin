"""
Texture Exporter – v3
Substance Painter 11.1.2  |  PySide6

Features:
  Phase 1  Single texture export with preset / layer / format selection
  Phase 2  Batch export: 3-level skin pack detection, worn pair consolidation,
           visibility state save/restore, per-job progress
  Phase 3  Contextual layer toggling: text fill layer (invert filter), worn
           paint layer, black parts group (worn / non-worn children)

Improvements added in v3:
  1. Config persistence  – settings saved to JSON beside the plugin file
  2. Multi-channel       – per-channel checklist (Base Color, Roughness, …)
  3. Dry-run / preview   – shows every filename before running
  4. Cancel button       – aborts mid-batch, always restores visibility
  5. Multi-texture-set   – checklist of all TSes; all selected are exported
                           per skin in a single call using exportList + $textureSet
  6. Worn suffix field   – configurable string appended to worn skin filenames

API references (uploaded docs):
  ui.html         → ApplicationMenu.Window, add_action, get_main_window
  export.html     → export_project_textures, ExportStatus, exportShaderParams,
                    exportPresets, exportList (rootPath, $textureSet wildcard),
                    exportParameters, list_predefined_export_presets,
                    list_resource_export_presets; srcMapName values:
                    baseColor, roughness, metallic, normal, height, emissive,
                    opacity, ambientOcclusion, specular
  textureset.html → TextureSet.name, TextureSet.is_layered_material(),
                    TextureSet.get_stack(), TextureSet.all_stacks(),
                    get_active_stack(), all_texture_sets()
  navigation.html → get_root_layer_nodes, GroupLayerNode.sub_layers(),
                    Node.get_name(), Node.is_visible(), Node.set_visible(),
                    LayerNode.content_effects(), LayerNode.mask_effects()
  filter.html     → FilterEffectNode (inherits Node)
  fill.html       → FillLayerNode (inherits LayerNode)

Install: Documents/Adobe/Adobe Substance 3D Painter/python/plugins/
Open:    Window ▶ Texture Exporter…
"""

import json
import os
import re

import substance_painter.ui
import substance_painter.export
import substance_painter.textureset
import substance_painter.layerstack
import substance_painter.project
import substance_painter.logging

from PySide6.QtGui     import QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QFileDialog, QGroupBox, QMessageBox, QSizePolicy,
    QProgressBar, QTextEdit, QTabWidget, QScrollArea,
    QCheckBox, QFrame, QSplitter,
)
from PySide6.QtCore import Qt, QCoreApplication

# ─────────────────────────────────────────────────────────────────────────────
_plugin_widgets = []
_window         = None
sp_ls           = substance_painter.layerstack

# Path for persisting settings, stored alongside the plugin file
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "texture_exporter_config.json"
)

# ── Channel definitions ───────────────────────────────────────────────────────
# srcMapName values from export.html comments (lines 1350-1354)
# rgb=True  → export as R+G+B;  rgb=False → export as L (luminance/greyscale)
CHANNEL_DEFS = {
    "baseColor":        {"label": "Base Color",        "suffix": "BC",  "rgb": True},
    "roughness":        {"label": "Roughness",          "suffix": "R",   "rgb": False},
    "metallic":         {"label": "Metallic",           "suffix": "M",   "rgb": False},
    "normal":           {"label": "Normal",             "suffix": "N",   "rgb": True},
    "height":           {"label": "Height",             "suffix": "H",   "rgb": False},
    "emissive":         {"label": "Emissive",           "suffix": "E",   "rgb": True},
    "opacity":          {"label": "Opacity",            "suffix": "O",   "rgb": False},
    "ambientOcclusion": {"label": "Ambient Occlusion",  "suffix": "AO",  "rgb": False},
    "specular":         {"label": "Specular",           "suffix": "S",   "rgb": False},
}

def _channel_map_entry(map_name: str, src_map_name: str, rgb: bool) -> dict:
    """
    Build one exportPresets.maps entry for a single channel.
    RGB channels (baseColor, normal, emissive) use R/G/B dest+src.
    Grayscale channels use L dest+src (export.html line 1331-1337).
    """
    if rgb:
        channels = [
            {"destChannel": c, "srcChannel": c,
             "srcMapType": "documentMap", "srcMapName": src_map_name}
            for c in ("R", "G", "B")
        ]
    else:
        channels = [
            {"destChannel": "L", "srcChannel": "L",
             "srcMapType": "documentMap", "srcMapName": src_map_name}
        ]
    return {"fileName": map_name, "channels": channels}


# =============================================================================
# ── Layer-type helpers ────────────────────────────────────────────────────────
# =============================================================================
def _is_group(n):   return isinstance(n, sp_ls.GroupLayerNode)
def _is_fill(n):    return isinstance(n, sp_ls.FillLayerNode)
def _is_paint_fx(n):  return isinstance(n, sp_ls.PaintEffectNode)
def _is_filter_fx(n): return isinstance(n, sp_ls.FilterEffectNode)

def _is_proper_layer(n):
    return isinstance(n, (
        sp_ls.GroupLayerNode, sp_ls.PaintLayerNode,
        sp_ls.FillLayerNode,  sp_ls.InstanceLayerNode,
    ))

def _group_children(node):
    return [c for c in node.sub_layers() if _is_group(c)]


def _walk_all(stack):
    """Flat [(depth, node)] of all proper layers in a stack."""
    out = []
    def _r(nodes, d):
        for n in nodes:
            if _is_proper_layer(n):
                out.append((d, n))
            if _is_group(n):
                _r(n.sub_layers(), d + 1)
    _r(sp_ls.get_root_layer_nodes(stack), 0)
    return out

def _walk_fills(stack):
    return [(d, n) for d, n in _walk_all(stack) if _is_fill(n)]

def _walk_groups(stack):
    return [(d, n) for d, n in _walk_all(stack) if _is_group(n)]


# =============================================================================
# ── Name cleaning ─────────────────────────────────────────────────────────────
# =============================================================================
def clean_skin_name(raw: str) -> str:
    s = raw.lstrip('!')
    s = re.sub(r'^[Ss]kin\s+', '', s).strip()
    s = re.sub(r'\s*\(.*', '', s).strip()
    s = re.sub(r'\bBlack\b', 'Default', s)
    return s.strip()


# =============================================================================
# ── Skin-pack structure ───────────────────────────────────────────────────────
# =============================================================================
def find_skin_pack_root(stack):
    """
    Detect the 3-level skin pack root (≥3 Level-2 category groups each having
    ≥1 Level-3 skin group). Requiring ≥3 prevents matching smaller utility
    groups like Black Parts (which only has 2 children).
    """
    for node in sp_ls.get_root_layer_nodes(stack):
        if not _is_group(node):
            continue
        level2 = _group_children(node)
        if len(level2) < 3:
            continue
        if all(len(_group_children(c)) > 0 for c in level2):
            return node
    return None


def categorize_level2(skin_pack_node):
    """
    Returns (normal_cat, bright_cats, worn_plastic, worn_metal).
    Worn pair = exactly two Level-2 groups with matching child counts.
    Normal = largest remaining standalone; Bright = all others.
    """
    level2   = _group_children(skin_pack_node)
    by_count = {}
    for c in level2:
        by_count.setdefault(len(_group_children(c)), []).append(c)

    worn_p = worn_m = None
    standalone = []
    for cats in by_count.values():
        if len(cats) == 2:
            a, b = cats
            an, bn = a.get_name().lower(), b.get_name().lower()
            if 'plastic' in an or 'metal' in bn:
                worn_p, worn_m = a, b
            elif 'metal' in an or 'plastic' in bn:
                worn_p, worn_m = b, a
            else:
                order = {c: i for i, c in enumerate(level2)}
                worn_p, worn_m = (a, b) if order[a] < order[b] else (b, a)
        else:
            standalone.extend(cats)

    order = {c: i for i, c in enumerate(level2)}
    standalone.sort(key=lambda c: order[c])
    normal = max(standalone, key=lambda c: len(_group_children(c))) if standalone else None
    bright = [c for c in standalone if c is not normal]
    return normal, bright, worn_p, worn_m


def build_export_jobs(skin_pack_node):
    """
    Returns (jobs, all_skin_nodes, all_cat_nodes).
    Each job: {filename, show, all_in_category, cat_type}
    """
    normal, bright_cats, worn_p, worn_m = categorize_level2(skin_pack_node)
    jobs = []
    all_skins = []
    all_cats  = []

    def _add_standalone(cat, cat_type):
        all_cats.append(cat)
        skins = _group_children(cat)
        all_skins.extend(skins)
        for skin in skins:
            jobs.append({"filename": clean_skin_name(skin.get_name()),
                         "show": [skin], "all_in_category": skins,
                         "cat_type": cat_type})

    if normal:
        _add_standalone(normal, "normal")

    if worn_p and worn_m:
        all_cats.extend([worn_p, worn_m])
        ps = _group_children(worn_p)
        ms = _group_children(worn_m)
        all_skins.extend(ps + ms)
        all_worn = ps + ms
        for i in range(min(len(ps), len(ms))):
            jobs.append({"filename": clean_skin_name(ps[i].get_name()),
                         "show": [ps[i], ms[i]],
                         "all_in_category": all_worn,
                         "cat_type": "worn"})

    for cat in bright_cats:
        _add_standalone(cat, "bright")

    return jobs, all_skins, all_cats


# =============================================================================
# ── Visibility helpers ────────────────────────────────────────────────────────
# =============================================================================
def _save_vis(nodes):
    return {n: n.is_visible() for n in nodes if n is not None}

def _restore_vis(state):
    for n, v in state.items():
        try:
            n.set_visible(v)
        except Exception:
            pass


# =============================================================================
# ── TextureSet helpers ────────────────────────────────────────────────────────
# =============================================================================
def _ts_root_path(ts) -> str:
    """
    exportList rootPath per export.html:
      Non-layered TS  → ts.name  (e.g. "Body")
      Layered TS      → ts.name/stack.name  (e.g. "Body/Mask")
    """
    if ts.is_layered_material():
        return ts.name + "/" + ts.get_stack().name
    return ts.name


# =============================================================================
# ── Export config builder ─────────────────────────────────────────────────────
# =============================================================================
def build_export_config(
    ts_root_paths: list,
    out_dir: str,
    skin_name: str,
    sp_format: str,
    enabled_channels: list,   # list of srcMapName keys from CHANNEL_DEFS
    worn_suffix: str,         # e.g. " Worn" appended to worn skin names
    cat_type: str,
    multi_ts: bool,
) -> dict:
    """
    Build export_project_textures config for one skin.

    Filename scheme:
      single TS  + 1 channel  →  {skin}
      single TS  + N channels →  {skin}_{channel_suffix}
      multi TS   + 1 channel  →  $textureSet_{skin}
      multi TS   + N channels →  $textureSet_{skin}_{channel_suffix}

    The worn_suffix is already baked into skin_name for worn jobs.
    """
    bit_depth = "16f" if sp_format == "exr" else "8"
    multi_ch  = len(enabled_channels) > 1

    maps = []
    for ch_key in enabled_channels:
        ch = CHANNEL_DEFS[ch_key]
        # Build filename
        base = f"$textureSet_{skin_name}" if multi_ts else skin_name
        fname = f"{base}_{ch['suffix']}" if multi_ch else base
        maps.append(_channel_map_entry(fname, ch_key, ch["rgb"]))

    return {
        "exportShaderParams": False,
        "exportPath":         out_dir,
        "exportPresets": [{"name": "BatchPreset", "maps": maps}],
        "defaultExportPreset": "BatchPreset",
        "exportList": [{"rootPath": rp} for rp in ts_root_paths],
        "exportParameters": [{
            "parameters": {
                "fileFormat":       sp_format,
                "bitDepth":         bit_depth,
                "dithering":        False,
                "paddingAlgorithm": "infinite",
                "dilationDistance": 16,
            }
        }]
    }


# =============================================================================
# ── Auto-detect contextual layers ─────────────────────────────────────────────
# =============================================================================
def _auto_detect_ctx(stack, skin_root):
    """
    Structurally detect the best contextual layer candidates.
    Returns {fill, bp_group, bp_nonworn, bp_worn}; any may be None.

    fill:
      FillLayerNode with BOTH a FilterEffectNode in content_effects() AND
      a PaintEffectNode in mask_effects(). Falls back to any fill with just
      a FilterEffectNode.

    bp_group:
      Root-level (depth 0) GroupLayerNode that is NOT the skin pack root
      and has ≥2 proper-layer children.

    bp_nonworn / bp_worn:
      Child name hint: 'worn' in name → worn slot. Positional fallback.
    """
    result = {"fill": None, "bp_group": None, "bp_nonworn": None, "bp_worn": None}

    # -- Fill layer -----------------------------------------------------------
    best = fallback = None
    for _, node in _walk_fills(stack):
        try:
            has_f = any(_is_filter_fx(fx) for fx in node.content_effects())
            has_p = any(_is_paint_fx(fx)  for fx in node.mask_effects())
        except Exception:
            continue
        if has_f and has_p:
            best = node
            break
        if has_f and not fallback:
            fallback = node
    result["fill"] = best or fallback

    # -- Black Parts group ----------------------------------------------------
    try:
        for node in sp_ls.get_root_layer_nodes(stack):
            if not _is_group(node):
                continue
            if skin_root is not None and node is skin_root:
                continue
            children = [c for c in node.sub_layers() if _is_proper_layer(c)]
            if len(children) >= 2:
                result["bp_group"] = node
                worn_ch = nonworn_ch = None
                for ch in children:
                    if "worn" in ch.get_name().lower():
                        if not worn_ch:
                            worn_ch = ch
                    else:
                        if not nonworn_ch:
                            nonworn_ch = ch
                if not nonworn_ch and children:
                    nonworn_ch = children[0]
                if not worn_ch and len(children) >= 2:
                    worn_ch = children[1]
                result["bp_nonworn"] = nonworn_ch
                result["bp_worn"]    = worn_ch
                break
    except Exception:
        pass

    return result


# =============================================================================
# ── Window ────────────────────────────────────────────────────────────────────
# =============================================================================
class TextureExporterWindow(QWidget):

    FORMATS = {
        "PNG":  "png",
        "JPG":  "jpeg",
        "TIFF": "tiff",
        "EXR":  "exr",
        "TGA":  "tga",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(540)
        self.setMinimumHeight(600)
        self.setWindowFlags(Qt.Window)

        self._last_dir        = ""
        self._preset_data     = []
        self._batch_running   = False
        self._cancel_requested = False
        self._ts_checks       = {}    # {ts_name: QCheckBox}
        self._channel_checks  = {}    # {srcMapName: QCheckBox}

        self._build_ui()
        self._load_config()

    # =========================================================================
    # ── UI skeleton ───────────────────────────────────────────────────────────
    # =========================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
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
        grp = QGroupBox("Output Template Preset")
        g   = QVBoxLayout(grp)
        row = QHBoxLayout()
        row.addWidget(QLabel("Preset:"))
        self._s_preset = QComboBox()
        self._s_preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self._s_preset)
        btn = QPushButton("↺"); btn.setFixedWidth(28)
        btn.clicked.connect(self._refresh_presets)
        row.addWidget(btn)
        g.addLayout(row)
        lay.addWidget(grp)

        # Layer picker — GroupLayerNode only
        grp2 = QGroupBox("Layer (output filename)")
        g2   = QVBoxLayout(grp2)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Layer:"))
        self._s_layer = QComboBox()
        self._s_layer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row2.addWidget(self._s_layer)
        btn2 = QPushButton("↺"); btn2.setFixedWidth(28)
        btn2.clicked.connect(self._refresh_single_layers)
        row2.addWidget(btn2)
        g2.addLayout(row2)
        lay.addWidget(grp2)

        # Output settings
        grp3 = QGroupBox("Export Settings")
        g3   = QVBoxLayout(grp3)
        g3.setSpacing(6)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Output Folder:"))
        self._s_path = QLineEdit(); self._s_path.setPlaceholderText("Browse…")
        self._s_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        pr.addWidget(self._s_path)
        bb = QPushButton("Browse…"); bb.setFixedWidth(68)
        bb.clicked.connect(lambda: self._browse(self._s_path))
        pr.addWidget(bb)
        g3.addLayout(pr)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("Format:"))
        self._s_fmt = QComboBox()
        for lbl in self.FORMATS: self._s_fmt.addItem(lbl)
        self._s_fmt.setFixedWidth(80)
        fr.addWidget(self._s_fmt); fr.addStretch()
        g3.addLayout(fr)
        lay.addWidget(grp3)

        btn_exp = QPushButton("Export Texture"); btn_exp.setFixedHeight(28)
        btn_exp.clicked.connect(self._single_export)
        lay.addWidget(btn_exp)

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

        pr = QHBoxLayout()
        pr.addWidget(QLabel("Output Folder:"))
        self._b_path = QLineEdit(); self._b_path.setPlaceholderText("Browse…")
        self._b_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        pr.addWidget(self._b_path)
        bb = QPushButton("Browse…"); bb.setFixedWidth(68)
        bb.clicked.connect(lambda: self._browse(self._b_path))
        pr.addWidget(bb)
        og.addLayout(pr)

        fr = QHBoxLayout()
        fr.addWidget(QLabel("Format:"))
        self._b_fmt = QComboBox()
        for lbl in self.FORMATS: self._b_fmt.addItem(lbl)
        self._b_fmt.setFixedWidth(80)
        fr.addWidget(self._b_fmt)
        fr.addSpacing(20)
        fr.addWidget(QLabel("Worn Suffix:"))
        self._b_worn_suffix = QLineEdit(" Worn")
        self._b_worn_suffix.setFixedWidth(80)
        self._b_worn_suffix.setToolTip(
            "Appended to worn skin filenames when the layer name does not\n"
            "already contain the word 'worn' (case-insensitive)."
        )
        fr.addWidget(self._b_worn_suffix)
        fr.addStretch()
        og.addLayout(fr)
        lay.addWidget(out_grp)

        # ── Texture Sets ──────────────────────────────────────────────────────
        ts_grp = QGroupBox("Texture Sets")
        tg = QVBoxLayout(ts_grp)
        ts_note = QLabel("All selected Texture Sets are exported per skin in one call.")
        ts_note.setStyleSheet("color:#aaa; font-size:11px;")
        tg.addWidget(ts_note)
        self._ts_container = QWidget()
        self._ts_layout    = QVBoxLayout(self._ts_container)
        self._ts_layout.setContentsMargins(0, 0, 0, 0)
        self._ts_layout.setSpacing(2)
        tg.addWidget(self._ts_container)
        btn_ref_ts = QPushButton("↺  Refresh Texture Sets")
        btn_ref_ts.clicked.connect(self._refresh_ts_list)
        tg.addWidget(btn_ref_ts)
        lay.addWidget(ts_grp)

        # ── Channels ──────────────────────────────────────────────────────────
        ch_grp = QGroupBox("Export Channels")
        cg = QGridLayout(ch_grp)
        cg.setSpacing(4)
        ch_note = QLabel(
            "When only one channel is selected the filename has no suffix.\n"
            "Multiple channels → filename_{suffix} per channel."
        )
        ch_note.setStyleSheet("color:#aaa; font-size:11px;")
        ch_note.setWordWrap(True)
        cg.addWidget(ch_note, 0, 0, 1, 3)
        for col, (key, ch) in enumerate(CHANNEL_DEFS.items()):
            cb = QCheckBox(f"{ch['label']}  [{ch['suffix']}]")
            cb.setChecked(key == "baseColor")   # Base Color on by default
            self._channel_checks[key] = cb
            cg.addWidget(cb, 1 + col // 3, col % 3)
        lay.addWidget(ch_grp)

        # ── Contextual layers ─────────────────────────────────────────────────
        ctx_grp = QGroupBox("Contextual Layers  (all optional)")
        ctg = QVBoxLayout(ctx_grp)
        ctg.setSpacing(6)
        ctx_note = QLabel(
            "Toggled automatically per category. Leave any picker on '— none —' to skip.\n"
            "Auto-detect runs per Texture Set during batch; pickers are for preview only."
        )
        ctx_note.setWordWrap(True)
        ctx_note.setStyleSheet("color:#aaa; font-size:11px;")
        ctg.addWidget(ctx_note)
        btn_ref_ctx = QPushButton("↺  Refresh All Layer Pickers")
        btn_ref_ctx.clicked.connect(self._refresh_ctx_pickers)
        ctg.addWidget(btn_ref_ctx)

        LW = 200
        def _crow(label, attr, tip=""):
            r = QHBoxLayout()
            l = QLabel(label); l.setFixedWidth(LW)
            r.addWidget(l)
            c = QComboBox()
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if tip: c.setToolTip(tip)
            setattr(self, attr, c)
            r.addWidget(c)
            ctg.addLayout(r)

        _crow("Text Fill Layer:",       "_ctx_fill",
              "FillLayerNode with Invert filter in content_effects().")
        _crow("Worn Paint Layer:",      "_ctx_worn_paint",
              "PaintEffectNode in fill layer's mask_effects(). Auto-populated.")
        _crow("Black Parts Group:",     "_ctx_bp_group",
              "GroupLayerNode containing non-worn and worn black-part children.")
        _crow("Black Parts — Non-Worn:", "_ctx_bp_nonworn",
              "Visible for Normal and Bright exports.")
        _crow("Black Parts — Worn:",    "_ctx_bp_worn",
              "Visible for Worn exports.")

        self._ctx_fill.currentIndexChanged.connect(self._on_fill_changed)
        self._ctx_bp_group.currentIndexChanged.connect(self._on_bp_changed)
        lay.addWidget(ctx_grp)

        # ── Skin pack structure ───────────────────────────────────────────────
        sp_grp = QGroupBox("Skin Pack Structure")
        spg = QVBoxLayout(sp_grp)
        self._b_struct_label = QLabel("Click 'Detect' or 'Preview' to inspect.")
        self._b_struct_label.setWordWrap(True)
        spg.addWidget(self._b_struct_label)
        btn_row = QHBoxLayout()
        btn_detect  = QPushButton("Detect Structure")
        btn_preview = QPushButton("🔍 Preview All Jobs")
        btn_detect.clicked.connect(self._detect_structure)
        btn_preview.clicked.connect(self._preview_jobs)
        btn_row.addWidget(btn_detect)
        btn_row.addWidget(btn_preview)
        spg.addLayout(btn_row)
        lay.addWidget(sp_grp)

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
        btn_clear = QPushButton("Clear"); btn_clear.setFixedWidth(60)
        btn_clear.clicked.connect(self._b_log.clear)
        lg.addWidget(btn_clear, alignment=Qt.AlignRight)
        lay.addWidget(log_grp)

        # ── Run / Cancel ──────────────────────────────────────────────────────
        run_row = QHBoxLayout()
        self._b_run_btn = QPushButton("▶  Run Batch Export")
        self._b_run_btn.setFixedHeight(32)
        self._b_run_btn.setStyleSheet(
            "QPushButton          { background:#2d6a9f; color:white; font-weight:bold; }"
            "QPushButton:hover    { background:#3a82c4; }"
            "QPushButton:disabled { background:#444;    color:#888;  }"
        )
        self._b_run_btn.clicked.connect(self._run_batch)
        self._b_cancel_btn = QPushButton("⏹ Cancel")
        self._b_cancel_btn.setFixedHeight(32)
        self._b_cancel_btn.setFixedWidth(90)
        self._b_cancel_btn.setEnabled(False)
        self._b_cancel_btn.setStyleSheet(
            "QPushButton          { background:#8b2020; color:white; }"
            "QPushButton:hover    { background:#b03030; }"
            "QPushButton:disabled { background:#444;    color:#888;  }"
        )
        self._b_cancel_btn.clicked.connect(self._request_cancel)
        run_row.addWidget(self._b_run_btn)
        run_row.addWidget(self._b_cancel_btn)
        lay.addLayout(run_row)
        lay.addStretch()

        scroll.setWidget(w)
        return scroll

    # =========================================================================
    # ── Single-export helpers ─────────────────────────────────────────────────
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
                self._sp_log(f"Predefined presets: {e}", warn=True)
            try:
                for p in substance_painter.export.list_resource_export_presets():
                    self._s_preset.addItem(f"{p.resource_id.name}  [shelf]")
                    self._preset_data.append({"type": "resource", "url": p.resource_id.url()})
            except Exception as e:
                self._sp_log(f"Resource presets: {e}", warn=True)
        self._s_preset.blockSignals(False)
        self._s_preset.setCurrentIndex(0)

    def _refresh_single_layers(self):
        self._s_layer.clear()
        if not substance_painter.project.is_open():
            self._s_layer.addItem("(no project open)"); return
        try:
            stack = substance_painter.textureset.get_active_stack()
            items = _walk_groups(stack)
            if not items:
                self._s_layer.addItem("(no group layers found)"); return
            for d, n in items:
                self._s_layer.addItem("  " * d + n.get_name(), n)
        except Exception as e:
            self._s_layer.addItem(f"Error: {e}")

    # =========================================================================
    # ── Texture Set list ──────────────────────────────────────────────────────
    # =========================================================================
    def _refresh_ts_list(self):
        # Clear existing checkboxes
        while self._ts_layout.count():
            item = self._ts_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._ts_checks.clear()

        if not substance_painter.project.is_open():
            self._ts_layout.addWidget(QLabel("(no project open)"))
            return
        try:
            ts_list = substance_painter.textureset.all_texture_sets()
            for ts in ts_list:
                cb = QCheckBox(ts.name)
                cb.setChecked(True)   # all selected by default
                self._ts_layout.addWidget(cb)
                self._ts_checks[ts.name] = cb
            if not ts_list:
                self._ts_layout.addWidget(QLabel("(no texture sets found)"))
        except Exception as e:
            self._ts_layout.addWidget(QLabel(f"Error: {e}"))

    def _selected_ts(self):
        """Return list of TextureSet objects that are checked."""
        if not substance_painter.project.is_open():
            return []
        try:
            all_ts = substance_painter.textureset.all_texture_sets()
            selected = [ts for ts in all_ts
                        if ts.name in self._ts_checks
                        and self._ts_checks[ts.name].isChecked()]
            return selected or all_ts   # fallback to all if none checked
        except Exception:
            return []

    # =========================================================================
    # ── Contextual layer pickers ──────────────────────────────────────────────
    # =========================================================================
    def _refresh_ctx_pickers(self):
        if not substance_painter.project.is_open():
            return
        try:
            stack     = substance_painter.textureset.get_active_stack()
            skin_root = find_skin_pack_root(stack)
            auto      = _auto_detect_ctx(stack, skin_root)
            self._populate_fill_picker(stack, auto["fill"])
            self._populate_bp_group_picker(stack, skin_root, auto["bp_group"])
            self._auto_select_bp_children(auto["bp_nonworn"], auto["bp_worn"])
        except Exception as e:
            self._sp_log(f"Ctx picker refresh: {e}", warn=True)

    def _populate_fill_picker(self, stack, auto_select=None):
        self._ctx_fill.blockSignals(True)
        self._ctx_fill.clear()
        self._ctx_fill.addItem("— none —", None)
        best = 0
        for i, (d, n) in enumerate(_walk_fills(stack)):
            self._ctx_fill.addItem("  " * d + n.get_name(), n)
            if auto_select is n:
                best = i + 1
        self._ctx_fill.blockSignals(False)
        self._ctx_fill.setCurrentIndex(best)
        self._populate_worn_paint_picker(auto_first=(best > 0))

    def _populate_worn_paint_picker(self, auto_first=False):
        self._ctx_worn_paint.blockSignals(True)
        self._ctx_worn_paint.clear()
        self._ctx_worn_paint.addItem("— none —", None)
        best = 0
        fill = self._ctx_fill.currentData()
        if fill is not None:
            try:
                count = 0
                for fx in fill.mask_effects():
                    if _is_paint_fx(fx):
                        self._ctx_worn_paint.addItem(fx.get_name(), fx)
                        count += 1
                        if auto_first and count == 1:
                            best = 1
            except Exception as e:
                self._sp_log(f"Worn paint picker: {e}", warn=True)
        self._ctx_worn_paint.blockSignals(False)
        self._ctx_worn_paint.setCurrentIndex(best)

    def _populate_bp_group_picker(self, stack, skin_root=None, auto_select=None):
        self._ctx_bp_group.blockSignals(True)
        self._ctx_bp_group.clear()
        self._ctx_bp_group.addItem("— none —", None)
        best = 0
        for i, (d, n) in enumerate(_walk_groups(stack)):
            self._ctx_bp_group.addItem("  " * d + n.get_name(), n)
            if auto_select is n:
                best = i + 1
        self._ctx_bp_group.blockSignals(False)
        self._ctx_bp_group.setCurrentIndex(best)
        self._populate_bp_child_pickers()

    def _populate_bp_child_pickers(self):
        for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            cb.blockSignals(True); cb.clear(); cb.addItem("— none —", None)
        bp = self._ctx_bp_group.currentData()
        if bp is not None:
            try:
                children = [c for c in bp.sub_layers() if _is_proper_layer(c)]
                for ch in children:
                    for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
                        cb.addItem(ch.get_name(), ch)
            except Exception as e:
                self._sp_log(f"BP child picker: {e}", warn=True)
        for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            cb.blockSignals(False); cb.setCurrentIndex(0)

    def _auto_select_bp_children(self, nonworn, worn):
        for combo, target in ((self._ctx_bp_nonworn, nonworn),
                              (self._ctx_bp_worn, worn)):
            if target is None:
                continue
            for i in range(combo.count()):
                if combo.itemData(i) is target:
                    combo.setCurrentIndex(i); break

    def _on_fill_changed(self, _):
        self._populate_worn_paint_picker()

    def _on_bp_changed(self, _):
        self._populate_bp_child_pickers()

    def _ctx_nodes(self):
        """
        Resolve contextual nodes. Finds first FilterEffectNode in
        fill.content_effects() → the Invert filter (navigation.html,
        filter.html). All are optional; None if not configured.
        """
        fill   = self._ctx_fill.currentData()
        wp     = self._ctx_worn_paint.currentData()
        bpg    = self._ctx_bp_group.currentData()
        bpnw   = self._ctx_bp_nonworn.currentData()
        bpw    = self._ctx_bp_worn.currentData()
        inv    = None
        if fill is not None:
            try:
                for fx in fill.content_effects():
                    if _is_filter_fx(fx):
                        inv = fx; break
            except Exception:
                pass
        return {"fill": fill, "invert": inv, "worn_paint": wp,
                "bp_group": bpg, "bp_nonworn": bpnw, "bp_worn": bpw}

    def _apply_ctx(self, cat_type: str, ctx: dict):
        """
        Normal : invert=ON,  worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF
        Worn   : invert=ON,  worn_paint=ON,  bp_nonworn=OFF, bp_worn=ON
        Bright : invert=OFF, worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF
        """
        inv_on  = cat_type in ("normal", "worn")
        wp_on   = (cat_type == "worn")
        nw_on   = cat_type in ("normal", "bright")
        def _s(n, v):
            if n is not None:
                try:
                    n.set_visible(v)
                except Exception as e:
                    self._sp_log(f"set_visible on {n.get_name()}: {e}", warn=True)
        _s(ctx["invert"],    inv_on)
        _s(ctx["worn_paint"], wp_on)
        _s(ctx["bp_nonworn"], nw_on)
        _s(ctx["bp_worn"],    not nw_on)

    # =========================================================================
    # ── Config persistence ────────────────────────────────────────────────────
    # =========================================================================
    def _save_config(self):
        cfg = {
            "output_path":    self._b_path.text(),
            "single_path":    self._s_path.text(),
            "format":         self._b_fmt.currentText(),
            "single_format":  self._s_fmt.currentText(),
            "worn_suffix":    self._b_worn_suffix.text(),
            "channels":       [k for k, cb in self._channel_checks.items()
                               if cb.isChecked()],
            "ctx_fill_name":  (self._ctx_fill.currentData().get_name()
                               if self._ctx_fill.currentData() else None),
            "ctx_wp_name":    (self._ctx_worn_paint.currentData().get_name()
                               if self._ctx_worn_paint.currentData() else None),
            "ctx_bp_name":    (self._ctx_bp_group.currentData().get_name()
                               if self._ctx_bp_group.currentData() else None),
            "ctx_bpnw_name":  (self._ctx_bp_nonworn.currentData().get_name()
                               if self._ctx_bp_nonworn.currentData() else None),
            "ctx_bpw_name":   (self._ctx_bp_worn.currentData().get_name()
                               if self._ctx_bp_worn.currentData() else None),
        }
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            self._sp_log(f"Could not save config: {e}", warn=True)

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            return

        if cfg.get("output_path"):
            self._b_path.setText(cfg["output_path"])
        if cfg.get("single_path"):
            self._s_path.setText(cfg["single_path"])
        if cfg.get("format") in self.FORMATS:
            self._b_fmt.setCurrentText(cfg["format"])
        if cfg.get("single_format") in self.FORMATS:
            self._s_fmt.setCurrentText(cfg["single_format"])
        if cfg.get("worn_suffix") is not None:
            self._b_worn_suffix.setText(cfg["worn_suffix"])
        if cfg.get("channels"):
            for key, cb in self._channel_checks.items():
                cb.setChecked(key in cfg["channels"])

        # Ctx layer names resolved lazily in _refresh_ctx_pickers via auto-detect;
        # if auto-detect doesn't find them, these names are used as a fallback.
        self._saved_ctx = {
            "fill":    cfg.get("ctx_fill_name"),
            "wp":      cfg.get("ctx_wp_name"),
            "bp":      cfg.get("ctx_bp_name"),
            "bpnw":    cfg.get("ctx_bpnw_name"),
            "bpw":     cfg.get("ctx_bpw_name"),
        }

    def _try_restore_ctx_by_name(self):
        """
        After pickers are populated, try to match saved names as a fallback
        for when auto-detect didn't fire.
        """
        saved = getattr(self, "_saved_ctx", {})
        def _match(combo, name):
            if name is None: return
            for i in range(combo.count()):
                d = combo.itemData(i)
                if d is not None and d.get_name() == name:
                    combo.setCurrentIndex(i); return
        _match(self._ctx_fill,     saved.get("fill"))
        _match(self._ctx_worn_paint, saved.get("wp"))
        _match(self._ctx_bp_group, saved.get("bp"))
        _match(self._ctx_bp_nonworn, saved.get("bpnw"))
        _match(self._ctx_bp_worn,  saved.get("bpw"))

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

    def _sp_log(self, msg, warn=False):
        level = substance_painter.logging.WARNING if warn \
                else substance_painter.logging.INFO
        substance_painter.logging.log(level, "TextureExporter", msg)

    def _blog(self, msg):
        self._b_log.append(msg)
        self._sp_log(msg)

    def _tick(self, done, total, name):
        self._b_progress.setValue(done)
        self._b_current.setText(f"Exporting: {name}  ({done}/{total})")
        QCoreApplication.processEvents()

    def _enabled_channels(self):
        return [k for k, cb in self._channel_checks.items() if cb.isChecked()]

    # =========================================================================
    # ── Single export ─────────────────────────────────────────────────────────
    # =========================================================================
    def _single_export(self):
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Open a project first."); return
        out = self._s_path.text().strip().replace("\\", "/")
        if not out:
            QMessageBox.warning(self, "No Folder", "Choose an output folder."); return
        ts_list = substance_painter.textureset.all_texture_sets()
        if not ts_list:
            QMessageBox.warning(self, "No Texture Sets", "No texture sets found."); return
        ts = ts_list[0]
        name    = self._s_layer.currentText().strip()
        sp_fmt  = self.FORMATS[self._s_fmt.currentText()]
        idx     = self._s_preset.currentIndex()
        preset  = self._preset_data[idx] if idx < len(self._preset_data) \
                  else {"type": "inline"}

        config = {
            "exportShaderParams": False,
            "exportPath":   out,
            "exportList":   [{"rootPath": ts.name}],
            "exportParameters": [{"parameters": {
                "fileFormat":       sp_fmt,
                "bitDepth":         "16f" if sp_fmt == "exr" else "8",
                "dithering":        False,
                "paddingAlgorithm": "infinite",
                "dilationDistance": 16,
            }}]
        }
        if preset["type"] == "inline":
            config["exportPresets"] = [{"name": "SP", "maps": [{
                "fileName": name,
                "channels": [
                    {"destChannel": c, "srcChannel": c,
                     "srcMapType": "documentMap", "srcMapName": "baseColor"}
                    for c in ("R", "G", "B")
                ]
            }]}]
            config["defaultExportPreset"] = "SP"
        else:
            config["defaultExportPreset"] = preset["url"]

        try:
            r = substance_painter.export.export_project_textures(config)
            if r.status == substance_painter.export.ExportStatus.Success:
                n = sum(len(v) for v in r.textures.values())
                self._s_status.setText(f"✅ Exported {n} file(s) → {out}")
                self._save_config()
            else:
                self._s_status.setText(f"❌ {r.message}")
                QMessageBox.critical(self, "Export Failed", r.message)
        except Exception as e:
            self._s_status.setText(f"❌ {e}")
            QMessageBox.critical(self, "Error", str(e))

    # =========================================================================
    # ── Batch: detect ─────────────────────────────────────────────────────────
    # =========================================================================
    def _get_primary_stack(self):
        """Primary stack used for skin pack detection and layer toggling."""
        sel = self._selected_ts()
        if sel:
            return sel[0].get_stack()
        return substance_painter.textureset.get_active_stack()

    def _detect_structure(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack = self._get_primary_stack()
            root  = find_skin_pack_root(stack)
            if root is None:
                self._b_struct_label.setText(
                    "❌ No 3-level skin pack found.\n"
                    "Root group must have ≥3 category groups, each with skin groups."
                ); return
            normal, bright, worn_p, worn_m = categorize_level2(root)
            lines = [f"✅ Root: \"{root.get_name()}\"", ""]
            if normal:
                lines.append(f"  [Normal]  \"{normal.get_name()}\"  "
                             f"→  {len(_group_children(normal))} skins")
            if worn_p and worn_m:
                pc, mc = _group_children(worn_p), _group_children(worn_m)
                lines.append(f"  [Worn]    \"{worn_p.get_name()}\" + "
                             f"\"{worn_m.get_name()}\"  →  {min(len(pc),len(mc))} pairs")
            for cat in bright:
                lines.append(f"  [Bright]  \"{cat.get_name()}\"  "
                             f"→  {len(_group_children(cat))} skins")
            jobs, _, _ = build_export_jobs(root)
            sel_ts = self._selected_ts()
            lines += ["", f"Total exports: {len(jobs)} skins × "
                      f"{len(sel_ts)} TS × {len(self._enabled_channels())} channel(s)"
                      f" = {len(jobs) * len(sel_ts) * max(1,len(self._enabled_channels()))} files"]
            self._b_struct_label.setText("\n".join(lines))
        except Exception as e:
            self._b_struct_label.setText(f"❌ Error: {e}")

    # =========================================================================
    # ── Batch: dry-run preview ────────────────────────────────────────────────
    # =========================================================================
    def _preview_jobs(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack    = self._get_primary_stack()
            root     = find_skin_pack_root(stack)
            if root is None:
                self._b_struct_label.setText("❌ No skin pack found."); return
            jobs, _, _ = build_export_jobs(root)
            sel_ts   = self._selected_ts()
            channels = self._enabled_channels()
            multi_ts = len(sel_ts) > 1
            multi_ch = len(channels) > 1
            worn_sfx = self._b_worn_suffix.text()

            self._b_log.clear()
            self._b_log.append("─── Dry Run Preview ───")
            for job in jobs:
                name = job["filename"]
                if job["cat_type"] == "worn" and worn_sfx and \
                        "worn" not in name.lower():
                    name = name + worn_sfx
                for ts in sel_ts:
                    ts_prefix = f"{ts.name}_" if multi_ts else ""
                    for ch_key in channels:
                        ch_sfx = f"_{CHANNEL_DEFS[ch_key]['suffix']}" if multi_ch else ""
                        self._b_log.append(
                            f"  [{job['cat_type']:6}]  {ts_prefix}{name}{ch_sfx}"
                        )
            total = len(jobs) * len(sel_ts) * max(1, len(channels))
            self._b_log.append(f"─── {total} files total ───")
            self._b_struct_label.setText(
                f"Preview complete: {len(jobs)} skins × "
                f"{len(sel_ts)} TS × {max(1,len(channels))} ch = {total} files."
            )
        except Exception as e:
            self._b_log.append(f"❌ Preview error: {e}")

    # =========================================================================
    # ── Batch: cancel ─────────────────────────────────────────────────────────
    # =========================================================================
    def _request_cancel(self):
        self._cancel_requested = True
        self._b_cancel_btn.setEnabled(False)
        self._b_current.setText("Cancelling after current export…")

    # =========================================================================
    # ── Batch: run ────────────────────────────────────────────────────────────
    # =========================================================================
    def _run_batch(self):
        if self._batch_running:
            return
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Open a project first."); return
        out = self._b_path.text().strip().replace("\\", "/")
        if not out:
            QMessageBox.warning(self, "No Folder", "Choose an output folder."); return

        channels = self._enabled_channels()
        if not channels:
            QMessageBox.warning(self, "No Channels",
                                "Select at least one channel to export."); return

        sel_ts = self._selected_ts()
        if not sel_ts:
            QMessageBox.warning(self, "No Texture Sets",
                                "Select at least one Texture Set."); return

        try:
            stack = self._get_primary_stack()
            root  = find_skin_pack_root(stack)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e)); return

        if root is None:
            QMessageBox.critical(self, "Not Found",
                                 "3-level skin pack structure not detected.\n"
                                 "Use 'Detect Structure' for details."); return

        jobs, all_skins, all_cats = build_export_jobs(root)
        if not jobs:
            QMessageBox.warning(self, "No Jobs", "No export jobs found."); return

        sp_fmt    = self.FORMATS[self._b_fmt.currentText()]
        multi_ts  = len(sel_ts) > 1
        worn_sfx  = self._b_worn_suffix.text()
        ctx       = self._ctx_nodes()
        ts_paths  = [_ts_root_path(ts) for ts in sel_ts]
        total     = len(jobs)

        # Confirm
        ts_names = ", ".join(ts.name for ts in sel_ts)
        ch_names = ", ".join(CHANNEL_DEFS[k]["label"] for k in channels)
        reply = QMessageBox.question(
            self, "Run Batch Export",
            f"Export {total} skins to:\n{out}\n\n"
            f"Texture Sets : {ts_names}\n"
            f"Channels     : {ch_names}\n"
            f"Format       : {sp_fmt.upper()}\n\n"
            f"Total files  : {total * len(sel_ts) * len(channels)}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # ── Setup ─────────────────────────────────────────────────────────────
        self._batch_running    = True
        self._cancel_requested = False
        self._b_run_btn.setEnabled(False)
        self._b_cancel_btn.setEnabled(True)
        self._b_progress.setMaximum(total)
        self._b_progress.setValue(0)
        self._b_log.clear()

        # Snapshot all nodes we'll touch
        vis_nodes = (
            [root] + all_cats + all_skins
            + [n for n in [ctx["fill"], ctx["invert"], ctx["worn_paint"],
                            ctx["bp_group"], ctx["bp_nonworn"], ctx["bp_worn"]]
               if n is not None]
        )
        vis_state = _save_vis(vis_nodes)

        errors = []
        try:
            root.set_visible(True)
            for n in all_cats:
                n.set_visible(True)
            if ctx["fill"]:
                ctx["fill"].set_visible(True)
            if ctx["bp_group"]:
                ctx["bp_group"].set_visible(True)
            for n in all_skins:
                n.set_visible(False)

            for idx, job in enumerate(jobs):
                if self._cancel_requested:
                    self._blog("⚠ Cancelled by user.")
                    break

                # Compute final filename (worn suffix logic)
                name = job["filename"]
                if job["cat_type"] == "worn" and worn_sfx and \
                        "worn" not in name.lower():
                    name = name + worn_sfx

                self._tick(idx, total, name)
                self._apply_ctx(job["cat_type"], ctx)
                for n in job["show"]:
                    n.set_visible(True)

                config = build_export_config(
                    ts_root_paths = ts_paths,
                    out_dir       = out,
                    skin_name     = name,
                    sp_format     = sp_fmt,
                    enabled_channels = channels,
                    worn_suffix   = worn_sfx,
                    cat_type      = job["cat_type"],
                    multi_ts      = multi_ts,
                )
                try:
                    r = substance_painter.export.export_project_textures(config)
                    if r.status == substance_painter.export.ExportStatus.Success:
                        n_files = sum(len(v) for v in r.textures.values())
                        self._blog(f"✅  [{job['cat_type']:6}]  {name}  ({n_files} file(s))")
                    else:
                        self._blog(f"❌  [{job['cat_type']:6}]  {name}: {r.message}")
                        errors.append(name)
                except Exception as e:
                    self._blog(f"❌  [{job['cat_type']:6}]  {name}: {e}")
                    errors.append(name)

                for n in job["show"]:
                    n.set_visible(False)

            if not self._cancel_requested:
                self._tick(total, total, "Done")

        finally:
            _restore_vis(vis_state)
            self._batch_running = False
            self._b_run_btn.setEnabled(True)
            self._b_cancel_btn.setEnabled(False)
            self._save_config()

        ok = total - len(errors)
        cancelled = self._cancel_requested
        status = f"{'Cancelled — ' if cancelled else ''}Complete — {ok}/{total} succeeded"
        if errors:
            status += f", {len(errors)} failed"
        self._b_current.setText(status)

        if errors:
            QMessageBox.warning(self, "Batch Complete with Errors",
                                f"{ok} exported.\nFailed: {', '.join(errors)}")
        elif not cancelled:
            QMessageBox.information(self, "Batch Complete",
                                    f"All {ok} skins exported to:\n{out}")

    # ── Window close → hide ───────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_config()
        event.ignore()
        self.hide()


# =============================================================================
# ── Plugin hooks ──────────────────────────────────────────────────────────────
# =============================================================================
def start_plugin():
    global _window
    _window = TextureExporterWindow(substance_painter.ui.get_main_window())
    action  = QAction("Texture Exporter…", substance_painter.ui.get_main_window())
    action.triggered.connect(_show_window)
    substance_painter.ui.add_action(
        substance_painter.ui.ApplicationMenu.Window, action
    )
    _plugin_widgets.append(action)
    substance_painter.logging.log(
        substance_painter.logging.INFO, "TextureExporter",
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
        _window._refresh_ts_list()
        _window._refresh_ctx_pickers()
        _window._try_restore_ctx_by_name()


if __name__ == "__main__":
    start_plugin()