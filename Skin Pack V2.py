"""
Texture Exporter – v4
Substance Painter 11.1.2  |  PySide6

v4 changes:
  1. Per-TS output paths   – each Texture Set row now has its own path field
                             and Browse button. Leave it blank to use the global
                             default path. TSes sharing the same resolved path
                             are batched in one call (with $textureSet prefix);
                             TSes with different paths get separate calls.
  2. Reliable batch cancel – processEvents() is now called immediately after
                             every export_project_textures() returns, so cancel
                             button clicks register at the earliest possible
                             moment regardless of how long each export took.
  3. Category filter       – Normal / Worn / Bright checkboxes above the run
                             button. Unchecked categories are skipped entirely;
                             the progress bar max and file count reflect only
                             the categories that will actually run.

API references (uploaded docs):
  ui.html         → ApplicationMenu.Window, add_action, get_main_window
  export.html     → export_project_textures, ExportStatus, exportShaderParams,
                    exportPresets, exportList (rootPath, $textureSet wildcard),
                    exportParameters, list_predefined_export_presets,
                    list_resource_export_presets; srcMapName values:
                    baseColor, roughness, metallic, normal, height, emissive,
                    opacity, ambientOcclusion, specular
  textureset.html → TextureSet.name, TextureSet.is_layered_material(),
                    TextureSet.get_stack(), all_texture_sets()
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
from collections import defaultdict

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
    QCheckBox,
)
from PySide6.QtCore import Qt, QCoreApplication

# ─────────────────────────────────────────────────────────────────────────────
_plugin_widgets = []
_window         = None
sp_ls           = substance_painter.layerstack

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "texture_exporter_config.json"
)

# ── All export categories ─────────────────────────────────────────────────────
ALL_CAT_TYPES = ("normal", "worn", "bright")

# ── Channel definitions ───────────────────────────────────────────────────────
# srcMapName values from export.html (lines 1350-1354)
# rgb=True → R/G/B channels;  rgb=False → L (greyscale)
CHANNEL_DEFS = {
    "baseColor":        {"label": "Base Color",       "suffix": "BC",  "rgb": True},
    "roughness":        {"label": "Roughness",         "suffix": "R",   "rgb": False},
    "metallic":         {"label": "Metallic",          "suffix": "M",   "rgb": False},
    "normal":           {"label": "Normal",            "suffix": "N",   "rgb": True},
    "height":           {"label": "Height",            "suffix": "H",   "rgb": False},
    "emissive":         {"label": "Emissive",          "suffix": "E",   "rgb": True},
    "opacity":          {"label": "Opacity",           "suffix": "O",   "rgb": False},
    "ambientOcclusion": {"label": "Ambient Occlusion", "suffix": "AO",  "rgb": False},
    "specular":         {"label": "Specular",          "suffix": "S",   "rgb": False},
}


def _channel_map_entry(map_name: str, src_map_name: str, rgb: bool) -> dict:
    """Single exportPresets.maps entry. RGB→R/G/B, greyscale→L."""
    if rgb:
        ch = [{"destChannel": c, "srcChannel": c,
               "srcMapType": "documentMap", "srcMapName": src_map_name}
              for c in ("R", "G", "B")]
    else:
        ch = [{"destChannel": "L", "srcChannel": "L",
               "srcMapType": "documentMap", "srcMapName": src_map_name}]
    return {"fileName": map_name, "channels": ch}


# =============================================================================
# ── Layer helpers ─────────────────────────────────────────────────────────────
# =============================================================================
def _is_group(n):     return isinstance(n, sp_ls.GroupLayerNode)
def _is_fill(n):      return isinstance(n, sp_ls.FillLayerNode)
def _is_paint_fx(n):  return isinstance(n, sp_ls.PaintEffectNode)
def _is_filter_fx(n): return isinstance(n, sp_ls.FilterEffectNode)

def _is_proper_layer(n):
    return isinstance(n, (sp_ls.GroupLayerNode, sp_ls.PaintLayerNode,
                           sp_ls.FillLayerNode,  sp_ls.InstanceLayerNode))

def _group_children(node):
    return [c for c in node.sub_layers() if _is_group(c)]

def _walk_all(stack):
    out = []
    def _r(nodes, d):
        for n in nodes:
            if _is_proper_layer(n): out.append((d, n))
            if _is_group(n):        _r(n.sub_layers(), d + 1)
    _r(sp_ls.get_root_layer_nodes(stack), 0)
    return out

def _walk_fills(stack):  return [(d, n) for d, n in _walk_all(stack) if _is_fill(n)]
def _walk_groups(stack): return [(d, n) for d, n in _walk_all(stack) if _is_group(n)]


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
    Root group with ≥3 Level-2 category groups each having ≥1 skin group.
    The ≥3 requirement prevents matching smaller utility groups (e.g. Black Parts).
    """
    for node in sp_ls.get_root_layer_nodes(stack):
        if not _is_group(node): continue
        level2 = _group_children(node)
        if len(level2) < 3: continue
        if all(len(_group_children(c)) > 0 for c in level2):
            return node
    return None


def categorize_level2(skin_pack_node):
    """Returns (normal_cat, bright_cats, worn_plastic, worn_metal)."""
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
            if 'plastic' in an or 'metal' in bn:     worn_p, worn_m = a, b
            elif 'metal' in an or 'plastic' in bn:   worn_p, worn_m = b, a
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
    """Returns (jobs, all_skin_nodes, all_cat_nodes). Each job: dict."""
    normal, bright_cats, worn_p, worn_m = categorize_level2(skin_pack_node)
    jobs = []; all_skins = []; all_cats = []

    def _add(cat, cat_type):
        all_cats.append(cat)
        skins = _group_children(cat)
        all_skins.extend(skins)
        for skin in skins:
            jobs.append({"filename": clean_skin_name(skin.get_name()),
                         "show": [skin], "all_in_category": skins,
                         "cat_type": cat_type})

    if normal: _add(normal, "normal")
    if worn_p and worn_m:
        all_cats.extend([worn_p, worn_m])
        ps, ms = _group_children(worn_p), _group_children(worn_m)
        all_skins.extend(ps + ms)
        all_worn = ps + ms
        for i in range(min(len(ps), len(ms))):
            jobs.append({"filename": clean_skin_name(ps[i].get_name()),
                         "show": [ps[i], ms[i]], "all_in_category": all_worn,
                         "cat_type": "worn"})
    for cat in bright_cats: _add(cat, "bright")
    return jobs, all_skins, all_cats


# =============================================================================
# ── Visibility helpers ────────────────────────────────────────────────────────
# =============================================================================
def _save_vis(nodes):  return {n: n.is_visible() for n in nodes if n is not None}
def _restore_vis(st):
    for n, v in st.items():
        try: n.set_visible(v)
        except Exception: pass


# =============================================================================
# ── TextureSet helpers ────────────────────────────────────────────────────────
# =============================================================================
def _ts_root_path(ts) -> str:
    """Non-layered → ts.name;  layered → ts.name/stack.name."""
    if ts.is_layered_material():
        return ts.name + "/" + ts.get_stack().name
    return ts.name


# =============================================================================
# ── Export config builder ─────────────────────────────────────────────────────
# =============================================================================
def build_export_config(
    ts_root_paths: list,   # one or more rootPath strings
    out_dir: str,
    skin_name: str,
    sp_format: str,
    enabled_channels: list,
    multi_ts: bool,        # True when ≥2 TSes share this output path
) -> dict:
    """
    Filename scheme:
      1 TS,  1 ch  →  {skin}
      1 TS,  N ch  →  {skin}_{ch_suffix}
      N TSes, 1 ch →  $textureSet_{skin}
      N TSes, N ch →  $textureSet_{skin}_{ch_suffix}

    $textureSet is resolved by SP at export time to each TS name.
    """
    bit_depth = "16f" if sp_format == "exr" else "8"
    multi_ch  = len(enabled_channels) > 1
    maps = []
    for key in enabled_channels:
        ch    = CHANNEL_DEFS[key]
        base  = f"$textureSet_{skin_name}" if multi_ts else skin_name
        fname = f"{base}_{ch['suffix']}" if multi_ch else base
        maps.append(_channel_map_entry(fname, key, ch["rgb"]))

    return {
        "exportShaderParams": False,
        "exportPath":         out_dir,
        "exportPresets":      [{"name": "BatchPreset", "maps": maps}],
        "defaultExportPreset": "BatchPreset",
        "exportList":         [{"rootPath": rp} for rp in ts_root_paths],
        "exportParameters":   [{"parameters": {
            "fileFormat":       sp_format,
            "bitDepth":         bit_depth,
            "dithering":        False,
            "paddingAlgorithm": "infinite",
            "dilationDistance": 16,
        }}]
    }


# =============================================================================
# ── Auto-detect contextual layers ─────────────────────────────────────────────
# =============================================================================
def _auto_detect_ctx(stack, skin_root):
    result = {"fill": None, "bp_group": None, "bp_nonworn": None, "bp_worn": None}
    best = fallback = None
    for _, node in _walk_fills(stack):
        try:
            has_f = any(_is_filter_fx(fx) for fx in node.content_effects())
            has_p = any(_is_paint_fx(fx)  for fx in node.mask_effects())
        except Exception: continue
        if has_f and has_p: best = node; break
        if has_f and not fallback: fallback = node
    result["fill"] = best or fallback

    try:
        for node in sp_ls.get_root_layer_nodes(stack):
            if not _is_group(node): continue
            if skin_root is not None and node is skin_root: continue
            children = [c for c in node.sub_layers() if _is_proper_layer(c)]
            if len(children) >= 2:
                result["bp_group"] = node
                worn_ch = nonworn_ch = None
                for ch in children:
                    if "worn" in ch.get_name().lower():
                        if not worn_ch:    worn_ch    = ch
                    else:
                        if not nonworn_ch: nonworn_ch = ch
                if not nonworn_ch and children:    nonworn_ch = children[0]
                if not worn_ch and len(children) >= 2: worn_ch = children[1]
                result["bp_nonworn"] = nonworn_ch
                result["bp_worn"]    = worn_ch
                break
    except Exception: pass
    return result


# =============================================================================
# ── Window ────────────────────────────────────────────────────────────────────
# =============================================================================
class TextureExporterWindow(QWidget):

    FORMATS = {"PNG": "png", "JPG": "jpeg", "TIFF": "tiff", "EXR": "exr", "TGA": "tga"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(580)
        self.setMinimumHeight(640)
        self.setWindowFlags(Qt.Window)
        self._last_dir         = ""
        self._preset_data      = []
        self._batch_running    = False
        self._cancel_requested = False
        # {ts_name: {"check": QCheckBox, "path": QLineEdit}}
        self._ts_widgets       = {}
        self._channel_checks   = {}   # {srcMapName: QCheckBox}
        self._cat_checks       = {}   # {"normal": QCheckBox, ...}
        self._saved_ctx        = {}
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
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(8); lay.setContentsMargins(8,8,8,8)

        grp = QGroupBox("Output Template Preset"); g = QVBoxLayout(grp)
        row = QHBoxLayout(); row.addWidget(QLabel("Preset:"))
        self._s_preset = QComboBox(); self._s_preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self._s_preset)
        b = QPushButton("↺"); b.setFixedWidth(28); b.clicked.connect(self._refresh_presets); row.addWidget(b)
        g.addLayout(row); lay.addWidget(grp)

        grp2 = QGroupBox("Layer (output filename)"); g2 = QVBoxLayout(grp2)
        row2 = QHBoxLayout(); row2.addWidget(QLabel("Layer:"))
        self._s_layer = QComboBox(); self._s_layer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row2.addWidget(self._s_layer)
        b2 = QPushButton("↺"); b2.setFixedWidth(28); b2.clicked.connect(self._refresh_single_layers); row2.addWidget(b2)
        g2.addLayout(row2); lay.addWidget(grp2)

        grp3 = QGroupBox("Export Settings"); g3 = QVBoxLayout(grp3); g3.setSpacing(6)
        pr = QHBoxLayout(); pr.addWidget(QLabel("Output Folder:"))
        self._s_path = QLineEdit(); self._s_path.setPlaceholderText("Browse…")
        self._s_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); pr.addWidget(self._s_path)
        bb = QPushButton("Browse…"); bb.setFixedWidth(68); bb.clicked.connect(lambda: self._browse(self._s_path)); pr.addWidget(bb)
        g3.addLayout(pr)
        fr = QHBoxLayout(); fr.addWidget(QLabel("Format:"))
        self._s_fmt = QComboBox()
        for lbl in self.FORMATS: self._s_fmt.addItem(lbl)
        self._s_fmt.setFixedWidth(80); fr.addWidget(self._s_fmt); fr.addStretch()
        g3.addLayout(fr); lay.addWidget(grp3)

        btn = QPushButton("Export Texture"); btn.setFixedHeight(28); btn.clicked.connect(self._single_export); lay.addWidget(btn)
        self._s_status = QLabel(""); self._s_status.setWordWrap(True); self._s_status.setAlignment(Qt.AlignCenter); lay.addWidget(self._s_status)
        lay.addStretch()
        self._refresh_presets(); self._refresh_single_layers()
        return w

    # =========================================================================
    # ── Tab 2: Batch Export ───────────────────────────────────────────────────
    # =========================================================================
    def _build_batch_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(8); lay.setContentsMargins(8,8,8,8)

        # ── Global format / worn suffix ───────────────────────────────────────
        fmt_grp = QGroupBox("Export Settings"); fg = QVBoxLayout(fmt_grp); fg.setSpacing(6)
        fr = QHBoxLayout(); fr.addWidget(QLabel("Format:"))
        self._b_fmt = QComboBox()
        for lbl in self.FORMATS: self._b_fmt.addItem(lbl)
        self._b_fmt.setFixedWidth(80); fr.addWidget(self._b_fmt)
        fr.addSpacing(20); fr.addWidget(QLabel("Worn Suffix:"))
        self._b_worn_suffix = QLineEdit(" Worn"); self._b_worn_suffix.setFixedWidth(80)
        self._b_worn_suffix.setToolTip(
            "Appended to worn skin filenames when the skin name does not\n"
            "already contain the word 'worn' (case-insensitive)."
        )
        fr.addWidget(self._b_worn_suffix); fr.addStretch()
        fg.addLayout(fr)
        # Global default path (used when a TS has no path of its own)
        gpr = QHBoxLayout(); gpr.addWidget(QLabel("Default Output Folder:"))
        self._b_path = QLineEdit(); self._b_path.setPlaceholderText("Used for any Texture Set with no path set")
        self._b_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); gpr.addWidget(self._b_path)
        gbb = QPushButton("Browse…"); gbb.setFixedWidth(68); gbb.clicked.connect(lambda: self._browse(self._b_path)); gpr.addWidget(gbb)
        fg.addLayout(gpr)
        lay.addWidget(fmt_grp)

        # ── Texture Sets (with per-TS path) ───────────────────────────────────
        ts_grp = QGroupBox("Texture Sets & Output Paths"); tg = QVBoxLayout(ts_grp)
        note = QLabel(
            "Each Texture Set can export to its own folder. "
            "Leave blank to use the Default Output Folder above.\n"
            "TSes sharing the same path are batched in one export call."
        )
        note.setWordWrap(True); note.setStyleSheet("color:#aaa;font-size:11px;"); tg.addWidget(note)
        self._ts_container = QWidget()
        self._ts_layout    = QVBoxLayout(self._ts_container)
        self._ts_layout.setContentsMargins(0, 0, 0, 0); self._ts_layout.setSpacing(3)
        tg.addWidget(self._ts_container)
        btn_ref_ts = QPushButton("↺  Refresh Texture Sets"); btn_ref_ts.clicked.connect(self._refresh_ts_list); tg.addWidget(btn_ref_ts)
        lay.addWidget(ts_grp)

        # ── Channels ──────────────────────────────────────────────────────────
        ch_grp = QGroupBox("Export Channels"); cg = QGridLayout(ch_grp); cg.setSpacing(4)
        cn = QLabel("1 channel selected → no suffix.  Multiple → filename_{suffix} per channel.")
        cn.setStyleSheet("color:#aaa;font-size:11px;"); cn.setWordWrap(True); cg.addWidget(cn, 0, 0, 1, 3)
        for i, (key, ch) in enumerate(CHANNEL_DEFS.items()):
            cb = QCheckBox(f"{ch['label']}  [{ch['suffix']}]")
            cb.setChecked(key == "baseColor")
            self._channel_checks[key] = cb
            cg.addWidget(cb, 1 + i // 3, i % 3)
        lay.addWidget(ch_grp)

        # ── Category filter ───────────────────────────────────────────────────
        cat_grp = QGroupBox("Export Categories"); catg = QHBoxLayout(cat_grp)
        cat_note = QLabel("Only checked categories will be exported:")
        cat_note.setStyleSheet("color:#aaa;font-size:11px;")
        catg.addWidget(cat_note)
        for cat_type in ALL_CAT_TYPES:
            cb = QCheckBox(cat_type.capitalize())
            cb.setChecked(True)
            cb.stateChanged.connect(self._update_job_count)
            self._cat_checks[cat_type] = cb
            catg.addWidget(cb)
        catg.addStretch()
        lay.addWidget(cat_grp)

        # ── Contextual layers ─────────────────────────────────────────────────
        ctx_grp = QGroupBox("Contextual Layers  (all optional)"); ctg = QVBoxLayout(ctx_grp); ctg.setSpacing(6)
        ctx_note = QLabel(
            "Toggled automatically per category. Leave any picker on '— none —' to skip."
        )
        ctx_note.setWordWrap(True); ctx_note.setStyleSheet("color:#aaa;font-size:11px;"); ctg.addWidget(ctx_note)
        btn_ref_ctx = QPushButton("↺  Refresh All Layer Pickers"); btn_ref_ctx.clicked.connect(self._refresh_ctx_pickers); ctg.addWidget(btn_ref_ctx)
        LW = 200
        def _crow(label, attr, tip=""):
            r = QHBoxLayout(); l = QLabel(label); l.setFixedWidth(LW); r.addWidget(l)
            c = QComboBox(); c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if tip: c.setToolTip(tip)
            setattr(self, attr, c); r.addWidget(c); ctg.addLayout(r)
        _crow("Text Fill Layer:",        "_ctx_fill",     "FillLayerNode with Invert filter in content_effects().")
        _crow("Worn Paint Layer:",       "_ctx_worn_paint","PaintEffectNode in fill layer's mask_effects(). Auto-populated.")
        _crow("Black Parts Group:",      "_ctx_bp_group", "GroupLayerNode containing non-worn and worn child layers.")
        _crow("Black Parts — Non-Worn:", "_ctx_bp_nonworn","Visible for Normal and Bright exports.")
        _crow("Black Parts — Worn:",     "_ctx_bp_worn",  "Visible for Worn exports.")
        self._ctx_fill.currentIndexChanged.connect(self._on_fill_changed)
        self._ctx_bp_group.currentIndexChanged.connect(self._on_bp_changed)
        lay.addWidget(ctx_grp)

        # ── Skin pack structure ───────────────────────────────────────────────
        sp_grp = QGroupBox("Skin Pack Structure"); spg = QVBoxLayout(sp_grp)
        self._b_struct_label = QLabel("Click 'Detect' or 'Preview' to inspect.")
        self._b_struct_label.setWordWrap(True); spg.addWidget(self._b_struct_label)
        btn_row = QHBoxLayout()
        btn_d = QPushButton("Detect Structure"); btn_d.clicked.connect(self._detect_structure)
        btn_p = QPushButton("🔍 Preview All Jobs"); btn_p.clicked.connect(self._preview_jobs)
        btn_row.addWidget(btn_d); btn_row.addWidget(btn_p); spg.addLayout(btn_row)
        lay.addWidget(sp_grp)

        # ── Progress ──────────────────────────────────────────────────────────
        prog_grp = QGroupBox("Progress"); pg = QVBoxLayout(prog_grp)
        self._b_progress = QProgressBar(); self._b_progress.setValue(0); self._b_progress.setFormat("%v / %m"); pg.addWidget(self._b_progress)
        self._b_current = QLabel(""); self._b_current.setAlignment(Qt.AlignCenter); pg.addWidget(self._b_current)
        lay.addWidget(prog_grp)

        # ── Log ───────────────────────────────────────────────────────────────
        log_grp = QGroupBox("Log"); lg = QVBoxLayout(log_grp)
        self._b_log = QTextEdit(); self._b_log.setReadOnly(True); self._b_log.setMinimumHeight(100); self._b_log.setMaximumHeight(180); lg.addWidget(self._b_log)
        bc = QPushButton("Clear"); bc.setFixedWidth(60); bc.clicked.connect(self._b_log.clear); lg.addWidget(bc, alignment=Qt.AlignRight)
        lay.addWidget(log_grp)

        # ── Run / Cancel ──────────────────────────────────────────────────────
        run_row = QHBoxLayout()
        self._b_run_btn = QPushButton("▶  Run Batch Export")
        self._b_run_btn.setFixedHeight(32)
        self._b_run_btn.setStyleSheet(
            "QPushButton          {background:#2d6a9f;color:white;font-weight:bold;}"
            "QPushButton:hover    {background:#3a82c4;}"
            "QPushButton:disabled {background:#444;color:#888;}"
        )
        self._b_run_btn.clicked.connect(self._run_batch)
        self._b_cancel_btn = QPushButton("⏹ Cancel")
        self._b_cancel_btn.setFixedHeight(32); self._b_cancel_btn.setFixedWidth(90)
        self._b_cancel_btn.setEnabled(False)
        self._b_cancel_btn.setStyleSheet(
            "QPushButton          {background:#8b2020;color:white;}"
            "QPushButton:hover    {background:#b03030;}"
            "QPushButton:disabled {background:#444;color:#888;}"
        )
        self._b_cancel_btn.clicked.connect(self._request_cancel)
        run_row.addWidget(self._b_run_btn); run_row.addWidget(self._b_cancel_btn)
        lay.addLayout(run_row)
        lay.addStretch()
        scroll.setWidget(w)
        return scroll

    # =========================================================================
    # ── Single-export helpers ─────────────────────────────────────────────────
    # =========================================================================
    def _refresh_presets(self):
        self._s_preset.blockSignals(True); self._s_preset.clear(); self._preset_data.clear()
        self._s_preset.addItem("★ Base Color  [custom]"); self._preset_data.append({"type": "inline"})
        if substance_painter.project.is_open():
            try:
                for p in substance_painter.export.list_predefined_export_presets():
                    self._s_preset.addItem(f"{p.name}  [predefined]")
                    self._preset_data.append({"type": "predefined", "url": p.url})
            except Exception as e: self._sp_log(f"Predefined presets: {e}", warn=True)
            try:
                for p in substance_painter.export.list_resource_export_presets():
                    self._s_preset.addItem(f"{p.resource_id.name}  [shelf]")
                    self._preset_data.append({"type": "resource", "url": p.resource_id.url()})
            except Exception as e: self._sp_log(f"Resource presets: {e}", warn=True)
        self._s_preset.blockSignals(False); self._s_preset.setCurrentIndex(0)

    def _refresh_single_layers(self):
        self._s_layer.clear()
        if not substance_painter.project.is_open():
            self._s_layer.addItem("(no project open)"); return
        try:
            stack = substance_painter.textureset.get_active_stack()
            items = _walk_groups(stack)
            if not items: self._s_layer.addItem("(no group layers found)"); return
            for d, n in items: self._s_layer.addItem("  " * d + n.get_name(), n)
        except Exception as e: self._s_layer.addItem(f"Error: {e}")

    # =========================================================================
    # ── Texture Set list (per-TS paths) ───────────────────────────────────────
    # =========================================================================
    def _refresh_ts_list(self):
        # Save any existing per-TS paths before rebuilding
        saved_paths = {name: w["path"].text() for name, w in self._ts_widgets.items()}

        while self._ts_layout.count():
            item = self._ts_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._ts_widgets.clear()

        if not substance_painter.project.is_open():
            self._ts_layout.addWidget(QLabel("(no project open)")); return

        try:
            ts_list = substance_painter.textureset.all_texture_sets()
            for ts in ts_list:
                row_w   = QWidget()
                row_lay = QHBoxLayout(row_w)
                row_lay.setContentsMargins(0, 0, 0, 0); row_lay.setSpacing(6)

                cb = QCheckBox(ts.name); cb.setChecked(True)
                cb.setMinimumWidth(140); cb.setMaximumWidth(160)
                row_lay.addWidget(cb)

                path_edit = QLineEdit()
                path_edit.setPlaceholderText("← uses Default Output Folder")
                path_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                # Restore previously typed path if the TS name matches
                if ts.name in saved_paths and saved_paths[ts.name]:
                    path_edit.setText(saved_paths[ts.name])
                row_lay.addWidget(path_edit)

                browse_btn = QPushButton("…"); browse_btn.setFixedWidth(28)
                browse_btn.clicked.connect(lambda _, pe=path_edit: self._browse(pe))
                row_lay.addWidget(browse_btn)

                self._ts_layout.addWidget(row_w)
                self._ts_widgets[ts.name] = {"check": cb, "path": path_edit}

            if not ts_list:
                self._ts_layout.addWidget(QLabel("(no texture sets found)"))
        except Exception as e:
            self._ts_layout.addWidget(QLabel(f"Error: {e}"))

    def _selected_ts(self):
        """Return list of selected TextureSet objects."""
        if not substance_painter.project.is_open(): return []
        try:
            all_ts = substance_painter.textureset.all_texture_sets()
            sel    = [ts for ts in all_ts
                      if ts.name in self._ts_widgets
                      and self._ts_widgets[ts.name]["check"].isChecked()]
            return sel or all_ts
        except Exception: return []

    def _resolved_path_for(self, ts_name: str) -> str:
        """Per-TS path if set, otherwise the global default path."""
        if ts_name in self._ts_widgets:
            p = self._ts_widgets[ts_name]["path"].text().strip().replace("\\", "/")
            if p: return p
        return self._b_path.text().strip().replace("\\", "/")

    def _ts_groups_by_path(self, sel_ts: list) -> dict:
        """
        Group selected TSes by their resolved output path.
        Returns {path: [ts, ...]}. TSes in the same bucket are batched
        into one export call and use the $textureSet filename prefix.
        """
        buckets = defaultdict(list)
        for ts in sel_ts:
            buckets[self._resolved_path_for(ts.name)].append(ts)
        return dict(buckets)

    # =========================================================================
    # ── Category filter ───────────────────────────────────────────────────────
    # =========================================================================
    def _enabled_cat_types(self) -> set:
        return {ct for ct, cb in self._cat_checks.items() if cb.isChecked()}

    def _update_job_count(self):
        """Refresh the struct label file count when category filter changes."""
        if not substance_painter.project.is_open(): return
        try:
            stack = self._get_primary_stack()
            root  = find_skin_pack_root(stack)
            if root is None: return
            jobs, _, _ = build_export_jobs(root)
            enabled    = self._enabled_cat_types()
            filtered   = [j for j in jobs if j["cat_type"] in enabled]
            sel_ts     = self._selected_ts()
            chs        = self._enabled_channels()
            total      = len(filtered) * max(1, len(chs))
            self._b_struct_label.setText(
                f"{len(filtered)} skins × {len(sel_ts)} TS × "
                f"{max(1,len(chs))} ch = {total * len(sel_ts)} files  "
                f"(categories: {', '.join(sorted(enabled)) or 'none'})"
            )
        except Exception: pass

    # =========================================================================
    # ── Contextual layer pickers ──────────────────────────────────────────────
    # =========================================================================
    def _refresh_ctx_pickers(self):
        if not substance_painter.project.is_open(): return
        try:
            stack     = substance_painter.textureset.get_active_stack()
            skin_root = find_skin_pack_root(stack)
            auto      = _auto_detect_ctx(stack, skin_root)
            self._populate_fill_picker(stack, auto["fill"])
            self._populate_bp_group_picker(stack, skin_root, auto["bp_group"])
            self._auto_select_bp_children(auto["bp_nonworn"], auto["bp_worn"])
            self._try_restore_ctx_by_name()
        except Exception as e: self._sp_log(f"Ctx picker refresh: {e}", warn=True)

    def _populate_fill_picker(self, stack, auto_select=None):
        self._ctx_fill.blockSignals(True); self._ctx_fill.clear(); self._ctx_fill.addItem("— none —", None)
        best = 0
        for i, (d, n) in enumerate(_walk_fills(stack)):
            self._ctx_fill.addItem("  " * d + n.get_name(), n)
            if auto_select is n: best = i + 1
        self._ctx_fill.blockSignals(False); self._ctx_fill.setCurrentIndex(best)
        self._populate_worn_paint_picker(auto_first=(best > 0))

    def _populate_worn_paint_picker(self, auto_first=False):
        self._ctx_worn_paint.blockSignals(True); self._ctx_worn_paint.clear(); self._ctx_worn_paint.addItem("— none —", None)
        best = 0; fill = self._ctx_fill.currentData()
        if fill is not None:
            try:
                cnt = 0
                for fx in fill.mask_effects():
                    if _is_paint_fx(fx):
                        self._ctx_worn_paint.addItem(fx.get_name(), fx); cnt += 1
                        if auto_first and cnt == 1: best = 1
            except Exception as e: self._sp_log(f"Worn paint picker: {e}", warn=True)
        self._ctx_worn_paint.blockSignals(False); self._ctx_worn_paint.setCurrentIndex(best)

    def _populate_bp_group_picker(self, stack, skin_root=None, auto_select=None):
        self._ctx_bp_group.blockSignals(True); self._ctx_bp_group.clear(); self._ctx_bp_group.addItem("— none —", None)
        best = 0
        for i, (d, n) in enumerate(_walk_groups(stack)):
            self._ctx_bp_group.addItem("  " * d + n.get_name(), n)
            if auto_select is n: best = i + 1
        self._ctx_bp_group.blockSignals(False); self._ctx_bp_group.setCurrentIndex(best)
        self._populate_bp_child_pickers()

    def _populate_bp_child_pickers(self):
        for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            cb.blockSignals(True); cb.clear(); cb.addItem("— none —", None)
        bp = self._ctx_bp_group.currentData()
        if bp is not None:
            try:
                for ch in (c for c in bp.sub_layers() if _is_proper_layer(c)):
                    for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
                        cb.addItem(ch.get_name(), ch)
            except Exception as e: self._sp_log(f"BP child picker: {e}", warn=True)
        for cb in (self._ctx_bp_nonworn, self._ctx_bp_worn):
            cb.blockSignals(False); cb.setCurrentIndex(0)

    def _auto_select_bp_children(self, nonworn, worn):
        for combo, target in ((self._ctx_bp_nonworn, nonworn), (self._ctx_bp_worn, worn)):
            if target is None: continue
            for i in range(combo.count()):
                if combo.itemData(i) is target: combo.setCurrentIndex(i); break

    def _on_fill_changed(self, _): self._populate_worn_paint_picker()
    def _on_bp_changed(self, _):   self._populate_bp_child_pickers()

    def _ctx_nodes(self):
        fill = self._ctx_fill.currentData()
        inv  = None
        if fill is not None:
            try:
                for fx in fill.content_effects():
                    if _is_filter_fx(fx): inv = fx; break
            except Exception: pass
        return {"fill": fill, "invert": inv,
                "worn_paint": self._ctx_worn_paint.currentData(),
                "bp_group":   self._ctx_bp_group.currentData(),
                "bp_nonworn": self._ctx_bp_nonworn.currentData(),
                "bp_worn":    self._ctx_bp_worn.currentData()}

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
                try: n.set_visible(v)
                except Exception as e: self._sp_log(f"set_visible on {n.get_name()}: {e}", warn=True)
        _s(ctx["invert"],     inv_on)
        _s(ctx["worn_paint"], wp_on)
        _s(ctx["bp_nonworn"], nw_on)
        _s(ctx["bp_worn"],    not nw_on)

    # =========================================================================
    # ── Config persistence ────────────────────────────────────────────────────
    # =========================================================================
    def _save_config(self):
        ts_paths = {name: w["path"].text() for name, w in self._ts_widgets.items()}
        ts_checks = {name: w["check"].isChecked() for name, w in self._ts_widgets.items()}
        cfg = {
            "output_path":   self._b_path.text(),
            "single_path":   self._s_path.text(),
            "format":        self._b_fmt.currentText(),
            "single_format": self._s_fmt.currentText(),
            "worn_suffix":   self._b_worn_suffix.text(),
            "channels":      [k for k, cb in self._channel_checks.items() if cb.isChecked()],
            "categories":    [ct for ct, cb in self._cat_checks.items() if cb.isChecked()],
            "ts_paths":      ts_paths,
            "ts_checks":     ts_checks,
            "ctx_fill_name":   (self._ctx_fill.currentData().get_name()       if self._ctx_fill.currentData()       else None),
            "ctx_wp_name":     (self._ctx_worn_paint.currentData().get_name() if self._ctx_worn_paint.currentData() else None),
            "ctx_bp_name":     (self._ctx_bp_group.currentData().get_name()   if self._ctx_bp_group.currentData()   else None),
            "ctx_bpnw_name":   (self._ctx_bp_nonworn.currentData().get_name() if self._ctx_bp_nonworn.currentData() else None),
            "ctx_bpw_name":    (self._ctx_bp_worn.currentData().get_name()    if self._ctx_bp_worn.currentData()    else None),
        }
        try:
            with open(CONFIG_PATH, "w") as f: json.dump(cfg, f, indent=2)
        except Exception as e: self._sp_log(f"Could not save config: {e}", warn=True)

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH): return
        try:
            with open(CONFIG_PATH) as f: cfg = json.load(f)
        except Exception: return
        if cfg.get("output_path"):   self._b_path.setText(cfg["output_path"])
        if cfg.get("single_path"):   self._s_path.setText(cfg["single_path"])
        if cfg.get("format") in self.FORMATS:        self._b_fmt.setCurrentText(cfg["format"])
        if cfg.get("single_format") in self.FORMATS: self._s_fmt.setCurrentText(cfg["single_format"])
        if cfg.get("worn_suffix") is not None:       self._b_worn_suffix.setText(cfg["worn_suffix"])
        if cfg.get("channels"):
            for key, cb in self._channel_checks.items(): cb.setChecked(key in cfg["channels"])
        if cfg.get("categories"):
            for ct, cb in self._cat_checks.items(): cb.setChecked(ct in cfg["categories"])
        # TS paths/checks applied in _refresh_ts_list (called after project opens)
        self._saved_ts_paths  = cfg.get("ts_paths", {})
        self._saved_ts_checks = cfg.get("ts_checks", {})
        self._saved_ctx = {
            "fill": cfg.get("ctx_fill_name"),   "wp":   cfg.get("ctx_wp_name"),
            "bp":   cfg.get("ctx_bp_name"),     "bpnw": cfg.get("ctx_bpnw_name"),
            "bpw":  cfg.get("ctx_bpw_name"),
        }

    def _try_restore_ctx_by_name(self):
        saved = getattr(self, "_saved_ctx", {})
        def _match(combo, name):
            if not name: return
            for i in range(combo.count()):
                d = combo.itemData(i)
                if d is not None and d.get_name() == name: combo.setCurrentIndex(i); return
        _match(self._ctx_fill,      saved.get("fill"))
        _match(self._ctx_worn_paint, saved.get("wp"))
        _match(self._ctx_bp_group,  saved.get("bp"))
        _match(self._ctx_bp_nonworn, saved.get("bpnw"))
        _match(self._ctx_bp_worn,   saved.get("bpw"))

    def _apply_saved_ts_data(self):
        """Apply saved per-TS paths and check states after _refresh_ts_list runs."""
        saved_paths  = getattr(self, "_saved_ts_paths",  {})
        saved_checks = getattr(self, "_saved_ts_checks", {})
        for name, w in self._ts_widgets.items():
            if name in saved_paths  and saved_paths[name]:
                w["path"].setText(saved_paths[name])
            if name in saved_checks:
                w["check"].setChecked(saved_checks[name])

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
            target.setText(folder); self._last_dir = folder

    def _sp_log(self, msg, warn=False):
        substance_painter.logging.log(
            substance_painter.logging.WARNING if warn else substance_painter.logging.INFO,
            "TextureExporter", msg
        )

    def _blog(self, msg): self._b_log.append(msg); self._sp_log(msg)

    def _tick(self, done, total, name):
        self._b_progress.setValue(done)
        self._b_current.setText(f"Exporting: {name}  ({done}/{total})")
        QCoreApplication.processEvents()

    def _enabled_channels(self):
        return [k for k, cb in self._channel_checks.items() if cb.isChecked()]

    def _get_primary_stack(self):
        sel = self._selected_ts()
        if sel: return sel[0].get_stack()
        return substance_painter.textureset.get_active_stack()

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
        ts      = ts_list[0]
        name    = self._s_layer.currentText().strip()
        sp_fmt  = self.FORMATS[self._s_fmt.currentText()]
        idx     = self._s_preset.currentIndex()
        preset  = self._preset_data[idx] if idx < len(self._preset_data) else {"type": "inline"}

        config  = {
            "exportShaderParams": False, "exportPath": out,
            "exportList": [{"rootPath": ts.name}],
            "exportParameters": [{"parameters": {
                "fileFormat": sp_fmt, "bitDepth": "16f" if sp_fmt == "exr" else "8",
                "dithering": False, "paddingAlgorithm": "infinite", "dilationDistance": 16,
            }}]
        }
        if preset["type"] == "inline":
            config["exportPresets"] = [{"name": "SP", "maps": [{"fileName": name, "channels": [
                {"destChannel": c, "srcChannel": c,
                 "srcMapType": "documentMap", "srcMapName": "baseColor"} for c in ("R","G","B")
            ]}]}]; config["defaultExportPreset"] = "SP"
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
    def _detect_structure(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack = self._get_primary_stack()
            root  = find_skin_pack_root(stack)
            if root is None:
                self._b_struct_label.setText(
                    "❌ No 3-level skin pack found.\n"
                    "Root group must have ≥3 category groups each containing skin groups."
                ); return
            normal, bright, worn_p, worn_m = categorize_level2(root)
            lines = [f"✅ Root: \"{root.get_name()}\"", ""]
            if normal:
                lines.append(f"  [Normal]  \"{normal.get_name()}\"  →  {len(_group_children(normal))} skins")
            if worn_p and worn_m:
                pc, mc = _group_children(worn_p), _group_children(worn_m)
                lines.append(f"  [Worn]    \"{worn_p.get_name()}\" + \"{worn_m.get_name()}\"  →  {min(len(pc),len(mc))} pairs")
            for cat in bright:
                lines.append(f"  [Bright]  \"{cat.get_name()}\"  →  {len(_group_children(cat))} skins")
            jobs, _, _ = build_export_jobs(root)
            enabled    = self._enabled_cat_types()
            filtered   = [j for j in jobs if j["cat_type"] in enabled]
            sel_ts     = self._selected_ts(); chs = self._enabled_channels()
            lines += ["", f"Filtered:  {len(filtered)} skins  (categories: {', '.join(sorted(enabled)) or 'none'})"]
            lines += [f"Total files: {len(filtered)} × {len(sel_ts)} TS × {max(1,len(chs))} ch "
                      f"= {len(filtered) * len(sel_ts) * max(1,len(chs))}"]
            self._b_struct_label.setText("\n".join(lines))
        except Exception as e: self._b_struct_label.setText(f"❌ Error: {e}")

    # =========================================================================
    # ── Batch: preview ────────────────────────────────────────────────────────
    # =========================================================================
    def _preview_jobs(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack    = self._get_primary_stack()
            root     = find_skin_pack_root(stack)
            if root is None: self._b_struct_label.setText("❌ No skin pack found."); return
            jobs, _, _ = build_export_jobs(root)
            enabled    = self._enabled_cat_types()
            sel_ts     = self._selected_ts()
            channels   = self._enabled_channels()
            worn_sfx   = self._b_worn_suffix.text()
            buckets    = self._ts_groups_by_path(sel_ts)
            multi_ch   = len(channels) > 1

            self._b_log.clear(); self._b_log.append("─── Dry Run Preview ───")
            for job in jobs:
                if job["cat_type"] not in enabled: continue
                name = job["filename"]
                if job["cat_type"] == "worn" and worn_sfx and "worn" not in name.lower():
                    name = name + worn_sfx
                for out_path, ts_group in buckets.items():
                    multi_ts  = len(ts_group) > 1
                    ts_prefix = "$textureSet_" if multi_ts else (ts_group[0].name + "_" if len(sel_ts) > 1 else "")
                    for ch_key in channels:
                        ch_sfx = f"_{CHANNEL_DEFS[ch_key]['suffix']}" if multi_ch else ""
                        self._b_log.append(
                            f"  [{job['cat_type']:6}]  {out_path}/  {ts_prefix}{name}{ch_sfx}"
                        )
            total = sum(
                len([j for j in jobs if j["cat_type"] in enabled])
                * len(ts_group) * max(1, len(channels))
                for ts_group in buckets.values()
            )
            self._b_log.append(f"─── {total} files total ───")
            self._b_struct_label.setText(f"Preview: {total} files across {len(buckets)} output path(s).")
        except Exception as e: self._b_log.append(f"❌ Preview error: {e}")

    # =========================================================================
    # ── Batch: cancel ─────────────────────────────────────────────────────────
    # =========================================================================
    def _request_cancel(self):
        self._cancel_requested = True
        self._b_cancel_btn.setEnabled(False)
        self._b_current.setText("Cancelling after current export…")
        # Force immediate UI update so the user sees feedback now
        QCoreApplication.processEvents()

    # =========================================================================
    # ── Batch: run ────────────────────────────────────────────────────────────
    # =========================================================================
    def _run_batch(self):
        if self._batch_running: return
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Open a project first."); return

        channels = self._enabled_channels()
        if not channels:
            QMessageBox.warning(self, "No Channels", "Select at least one channel."); return

        sel_ts = self._selected_ts()
        if not sel_ts:
            QMessageBox.warning(self, "No Texture Sets", "Select at least one Texture Set."); return

        # Validate all resolved paths are non-empty
        buckets = self._ts_groups_by_path(sel_ts)
        if "" in buckets:
            missing = ", ".join(ts.name for ts in buckets[""])
            QMessageBox.warning(self, "Missing Output Path",
                f"No output path set for: {missing}\n\n"
                "Set a Default Output Folder or a per-TS path for each selected Texture Set."
            ); return

        enabled_cats = self._enabled_cat_types()
        if not enabled_cats:
            QMessageBox.warning(self, "No Categories", "Select at least one category (Normal / Worn / Bright)."); return

        try:
            stack = self._get_primary_stack()
            root  = find_skin_pack_root(stack)
        except Exception as e: QMessageBox.critical(self, "Error", str(e)); return
        if root is None:
            QMessageBox.critical(self, "Not Found",
                "3-level skin pack structure not detected.\nUse 'Detect Structure' for details."); return

        all_jobs, all_skins, all_cats = build_export_jobs(root)
        # Apply category filter
        jobs = [j for j in all_jobs if j["cat_type"] in enabled_cats]
        if not jobs:
            QMessageBox.warning(self, "No Jobs",
                "No skins match the selected categories."); return

        sp_fmt   = self.FORMATS[self._b_fmt.currentText()]
        worn_sfx = self._b_worn_suffix.text()
        ctx      = self._ctx_nodes()
        total    = len(jobs)

        # Confirm dialog
        cat_str  = ", ".join(c.capitalize() for c in sorted(enabled_cats))
        ts_str   = ", ".join(ts.name for ts in sel_ts)
        ch_str   = ", ".join(CHANNEL_DEFS[k]["label"] for k in channels)
        path_str = "\n".join(f"  {p}  ({', '.join(t.name for t in tg)})"
                             for p, tg in buckets.items())
        n_files  = total * sum(len(tg) for tg in buckets.values()) * len(channels)
        reply = QMessageBox.question(
            self, "Run Batch Export",
            f"Export {total} skins\n\n"
            f"Categories   : {cat_str}\n"
            f"Texture Sets : {ts_str}\n"
            f"Channels     : {ch_str}\n"
            f"Format       : {sp_fmt.upper()}\n"
            f"Output paths :\n{path_str}\n\n"
            f"Total files  : {n_files}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes: return

        # ── Setup ─────────────────────────────────────────────────────────────
        self._batch_running    = True
        self._cancel_requested = False
        self._b_run_btn.setEnabled(False)
        self._b_cancel_btn.setEnabled(True)
        self._b_progress.setMaximum(total)
        self._b_progress.setValue(0)
        self._b_log.clear()

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
            for n in all_cats: n.set_visible(True)
            if ctx["fill"]:     ctx["fill"].set_visible(True)
            if ctx["bp_group"]: ctx["bp_group"].set_visible(True)
            for n in all_skins: n.set_visible(False)

            for idx, job in enumerate(jobs):
                # ── Cancel check (reliable: runs before AND after each export) ──
                if self._cancel_requested:
                    self._blog("⚠ Cancelled by user."); break

                name = job["filename"]
                if job["cat_type"] == "worn" and worn_sfx and "worn" not in name.lower():
                    name = name + worn_sfx

                self._tick(idx, total, name)
                self._apply_ctx(job["cat_type"], ctx)
                for n in job["show"]: n.set_visible(True)

                # One export call per path bucket for this skin
                for out_path, ts_group in buckets.items():
                    if self._cancel_requested: break   # ← also checked mid-bucket

                    ts_root_paths = [_ts_root_path(ts) for ts in ts_group]
                    multi_ts      = len(ts_group) > 1
                    config = build_export_config(
                        ts_root_paths    = ts_root_paths,
                        out_dir          = out_path,
                        skin_name        = name,
                        sp_format        = sp_fmt,
                        enabled_channels = channels,
                        multi_ts         = multi_ts,
                    )
                    try:
                        r = substance_painter.export.export_project_textures(config)
                        # ── Process events immediately after each export so cancel
                        #    button clicks register without waiting for the next tick ──
                        QCoreApplication.processEvents()
                        if r.status == substance_painter.export.ExportStatus.Success:
                            n_files = sum(len(v) for v in r.textures.values())
                            self._blog(f"✅  [{job['cat_type']:6}]  {name}  → {out_path}  ({n_files} files)")
                        else:
                            self._blog(f"❌  [{job['cat_type']:6}]  {name}  → {out_path}: {r.message}")
                            errors.append(name)
                    except Exception as e:
                        QCoreApplication.processEvents()
                        self._blog(f"❌  [{job['cat_type']:6}]  {name}  → {out_path}: {e}")
                        errors.append(name)

                for n in job["show"]: n.set_visible(False)

            if not self._cancel_requested: self._tick(total, total, "Done")

        finally:
            _restore_vis(vis_state)
            self._batch_running = False
            self._b_run_btn.setEnabled(True)
            self._b_cancel_btn.setEnabled(False)
            self._save_config()

        ok        = total - len(errors)
        cancelled = self._cancel_requested
        status    = f"{'Cancelled — ' if cancelled else ''}Complete — {ok}/{total} succeeded"
        if errors: status += f", {len(errors)} failed"
        self._b_current.setText(status)

        if errors:
            QMessageBox.warning(self, "Batch Complete with Errors",
                                f"{ok} exported.\nFailed: {', '.join(dict.fromkeys(errors))}")
        elif not cancelled:
            QMessageBox.information(self, "Batch Complete",
                                    f"All {ok} skins exported.")

    # ── Window close → hide ───────────────────────────────────────────────────
    def closeEvent(self, event):
        self._save_config(); event.ignore(); self.hide()


# =============================================================================
# ── Plugin hooks ──────────────────────────────────────────────────────────────
# =============================================================================
def start_plugin():
    global _window
    _window = TextureExporterWindow(substance_painter.ui.get_main_window())
    action  = QAction("Texture Exporter…", substance_painter.ui.get_main_window())
    action.triggered.connect(_show_window)
    substance_painter.ui.add_action(substance_painter.ui.ApplicationMenu.Window, action)
    _plugin_widgets.append(action)
    substance_painter.logging.log(
        substance_painter.logging.INFO, "TextureExporter",
        "Loaded – Window ▶ Texture Exporter…"
    )


def close_plugin():
    global _window
    for w in _plugin_widgets: substance_painter.ui.delete_ui_element(w)
    _plugin_widgets.clear()
    if _window: _window.deleteLater(); _window = None
    substance_painter.logging.log(
        substance_painter.logging.INFO, "TextureExporter", "Unloaded."
    )


def _show_window():
    if _window:
        _window.show(); _window.raise_(); _window.activateWindow()
        _window._refresh_presets()
        _window._refresh_single_layers()
        _window._refresh_ts_list()
        _window._apply_saved_ts_data()
        _window._refresh_ctx_pickers()
        _window._try_restore_ctx_by_name()


if __name__ == "__main__":
    start_plugin()