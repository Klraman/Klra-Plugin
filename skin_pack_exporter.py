"""
Skin Pack Exporter -- Substance Painter Plugin v1.5.0
Compatible with SP 11.1.2+

INSTALLATION:
  Copy this file to:
  %USERPROFILE%/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/

  Then in Substance Painter:
  Python > Plugins > Skin Pack Exporter

  The plugin opens as a floating window you can move anywhere.
  To reopen it: Python > Plugins > Skin Pack Exporter (click again).

HOW "SELECT Layer" WORKS:
  1. Click any layer/folder in SP's layer panel to select it.
  2. Click the matching "Select" button in this plugin.
  3. The plugin reads which layer SP currently has highlighted.

LAYER STATE PER EXPORT TYPE:
  normal       | invert ON,  worn OFF, black worn OFF | Normal folder
  worn         | invert ON,  worn ON,  black worn ON  | Worn(Plastic) + Worn(Metal) both active
  bright       | invert OFF, worn OFF, black worn OFF | Bright folder
  default      | invert ON,  worn OFF, black worn OFF | Black Parts only, Skin Pack hidden
  default_worn | invert ON,  worn ON,  black worn ON  | Black Parts only, Skin Pack hidden
"""

import os
import re
import json
import glob
import traceback
from typing import Optional, List, Dict, Any

# -- Qt -----------------------------------------------------------------------
try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt
    from PySide2.QtWidgets import QAction
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui   # type: ignore
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction

# -- Substance Painter API ----------------------------------------------------
import substance_painter.ui         as sp_ui
import substance_painter.project    as sp_project
import substance_painter.textureset as sp_textureset
import substance_painter.export     as sp_export
import substance_painter.logging    as sp_log
import substance_painter.layerstack as sp_layerstack


# =============================================================================
#  CONSTANTS
# =============================================================================

PLUGIN_NAME    = "Skin Pack Exporter"
PLUGIN_VERSION = "1.5.0"

DEFAULT_SKIN_PACK  = "!!Skin Pack (All)"
DEFAULT_TEXT       = "Text"
DEFAULT_BLACK      = "Black Parts"
DEFAULT_NORMAL     = "Normal"
DEFAULT_WORN_PLAS  = "Worn (Plastic)"
DEFAULT_WORN_MET   = "Worn (Metal)"
DEFAULT_BRIGHT     = "Bright"
DEFAULT_WORN_PAINT = "Worn"
DEFAULT_TEMPLATE   = "Base Color"

_RE_SUFFIX = re.compile(r'\s*\([^)]*\)\s*$')


# =============================================================================
#  LOGGING
# =============================================================================

def _log(msg: str, level: str = "info"):
    lvl = {"info": sp_log.INFO, "warn": sp_log.WARNING, "error": sp_log.ERROR}
    sp_log.log(lvl.get(level, sp_log.INFO), PLUGIN_NAME, str(msg))


# =============================================================================
#  JAVASCRIPT BRIDGE
#  All layer access goes through sp_js.evaluate().
#  We return JSON strings from JS so Python always gets plain data.
#
#  SP 11 JS API reference (alg namespace):
#    alg.texturesets.stack(name)        -> LayerStack
#    alg.layers.nodes(stack)            -> [LayerNode]
#    alg.layers.visibility(node)        -> bool
#    alg.layers.setVisibility(node, b)
#    alg.layers.effects(node)           -> [EffectNode]
#    alg.layers.isEffectEnabled(effect) -> bool
#    alg.layers.setEffectEnabled(e, b)
#    alg.layers.maskNodes(node)         -> [LayerNode]  (layers inside the mask)
#    alg.layers.selectedLayers(stack)   -> [LayerNode]
# =============================================================================

# =============================================================================
#  NATIVE PYTHON LAYER API (Replaces JS Bridge)
# =============================================================================

def get_node_name(node):
    if hasattr(node, 'name'):
        return node.name() if callable(node.name) else node.name
    return str(getattr(node, 'uid', lambda: "unknown")())

def get_node_enabled(node):
    if hasattr(node, 'is_enabled'):
        return node.is_enabled()
    return getattr(node, 'enabled', True)

def set_node_enabled(node, enabled):
    if hasattr(node, 'set_enabled'):
        node.set_enabled(enabled)
    else:
        node.enabled = enabled

def _find_node_by_path(stack, pathStr: str):
    if not pathStr: return None
    parts = pathStr.split("|")
    current_nodes = sp_layerstack.get_root_layer_nodes(stack)
    found = None
    for part in parts:
        found = None
        for n in current_nodes:
            if get_node_name(n) == part:
                found = n
                break
        if not found: return None
        if hasattr(found, 'sub_layers'):
            current_nodes = found.sub_layers()
        else:
            current_nodes = []
    return found

def _get_ts_stack(ts_name: str):
    for ts in sp_textureset.all_texture_sets():
        if ts.name() == ts_name:
            return ts.get_stack()
    return None

def _run_diag(ts_name: str) -> dict:
    out = {
        "Python API Version": sp_project.application.version() if hasattr(sp_project, 'application') else "unknown",
        "LayerStack Supported": hasattr(sp_layerstack, 'get_root_layer_nodes'),
    }
    try:
        stack = _get_ts_stack(ts_name)
        out["Target TS Found"] = stack is not None
        if stack:
            nodes = sp_layerstack.get_root_layer_nodes(stack)
            out["Root Nodes Count"] = len(nodes)
            if nodes:
                out["Test Name()"] = get_node_name(nodes[0])
                out["Test is_enabled()"] = get_node_enabled(nodes[0])
    except Exception as e:
        out["Error"] = str(e)
    return out

def _build_tree(nodes):
    tree = []
    for n in nodes:
        name = get_node_name(n)
        visible = get_node_enabled(n)
        ntype = str(getattr(n, 'get_type', lambda: "Unknown")()).split('.')[-1]
        
        # Always try to recurse -- don't gate on type string which varies between
        # SP versions ("GroupLayer" vs "NodeType.GroupLayer" vs enum repr)
        children = []
        if hasattr(n, 'sub_layers'):
            try:
                sub = n.sub_layers()
                if sub:
                    children = _build_tree(sub)
            except Exception:
                pass
            
        mask_nodes = []
        if hasattr(n, 'mask_effects'):
            for me in n.mask_effects():
                mask_nodes.append({
                    "name": get_node_name(me),
                    "type": "MaskNode",
                    "visible": get_node_enabled(me),
                    "uid": me.uid(),
                    "children": [], "maskNodes": [], "effects": []
                })
                
        effects = []
        if hasattr(n, 'content_effects'):
            for i, ce in enumerate(n.content_effects()):
                effects.append({
                    "index": i, "name": get_node_name(ce),
                    "enabled": get_node_enabled(ce), "uid": ce.uid()
                })
                
        tree.append({
            "name": name, "type": ntype, "visible": visible,
            "uid": n.uid(), "children": children,
            "maskNodes": mask_nodes, "effects": effects
        })
    return tree

def _get_layer_tree(ts_name: str):
    stack = _get_ts_stack(ts_name)
    if not stack: return None
    try:
        nodes = sp_layerstack.get_root_layer_nodes(stack)
        return _build_tree(nodes)
    except Exception as e:
        sp_log.error(f"Layer tree error: {e}")
        return None

def _get_selected_layer(ts_name: str) -> str:
    stack = _get_ts_stack(ts_name)
    if not stack: return None
    try:
        # 1. Get the selected Layer UIDs (integers)
        selection = sp_layerstack.get_selected_nodes(stack)
        if not selection: return None
        
        # If selection gives us objects, use them; if ints, we must search.
        # SP 11+ often returns list of ints.
        target_uid = selection[0]
        if hasattr(target_uid, 'uid'):
            return get_node_name(target_uid) # It was an object
            
        # 2. It's a number (UID), so we must traverse the stack to find the name
        def find_name_by_uid(nodes, uid):
            for n in nodes:
                if n.uid() == uid:
                    return get_node_name(n)
                # Check children (Group layers)
                if hasattr(n, 'sub_layers'):
                    found = find_name_by_uid(n.sub_layers(), uid)
                    if found: return found
            return None

        # Start search from root — use str() comparison to handle type mismatches
        # between the integer UIDs get_selected_nodes returns and what uid() gives
        def find_name_by_uid_safe(nodes, uid):
            uid_s = str(uid)
            for n in nodes:
                try:
                    if str(n.uid()) == uid_s:
                        return get_node_name(n)
                except Exception:
                    pass
                if hasattr(n, 'sub_layers'):
                    try:
                        result = find_name_by_uid_safe(n.sub_layers(), uid)
                        if result:
                            return result
                    except Exception:
                        pass
            return None

        root_nodes = sp_layerstack.get_root_layer_nodes(stack)
        name = find_name_by_uid_safe(root_nodes, target_uid)
        # Return None (not a UID string) so the caller shows its own error message
        return name if name else None

    except Exception as e:
        sp_log.error(f"Get selected error: {e}")
    return None

_snap_state = {}

def _snapshot_visibility(ts_name: str) -> str:
    global _snap_state
    _snap_state.clear()
    stack = _get_ts_stack(ts_name)
    if not stack: return "{}"
    
    def walk(nodes):
        for n in nodes:
            _snap_state[n.uid()] = get_node_enabled(n)
            if hasattr(n, 'sub_layers'): walk(n.sub_layers())
            if hasattr(n, 'mask_effects'):
                for me in n.mask_effects(): _snap_state[me.uid()] = get_node_enabled(me)
            if hasattr(n, 'content_effects'):
                for ce in n.content_effects(): _snap_state[ce.uid()] = get_node_enabled(ce)
                
    walk(sp_layerstack.get_root_layer_nodes(stack))
    return "ok"

def _restore_visibility(ts_name: str, snap: str):
    global _snap_state
    if not _snap_state: return
    stack = _get_ts_stack(ts_name)
    if not stack: return
    
    def walk(nodes):
        for n in nodes:
            if n.uid() in _snap_state: set_node_enabled(n, _snap_state[n.uid()])
            if hasattr(n, 'sub_layers'): walk(n.sub_layers())
            if hasattr(n, 'mask_effects'):
                for me in n.mask_effects():
                    if me.uid() in _snap_state: set_node_enabled(me, _snap_state[me.uid()])
            if hasattr(n, 'content_effects'):
                for ce in n.content_effects():
                    if ce.uid() in _snap_state: set_node_enabled(ce, _snap_state[ce.uid()])

    with sp_layerstack.ScopedModification("Restore Visibility"):
        walk(sp_layerstack.get_root_layer_nodes(stack))

def _scan_for_invert(ts_name: str, layer_path: str) -> dict:
    """
    Scan a fill layer's effect list and find the Invert filter.

    SP stores filters added via "Add filter" in content_effects().
    The internal resource name shown in Python may differ from the UI label
    ("Invert" in the UI could appear as "invert", "Invert Color", a UUID, etc.)
    We try several strategies and return all names found so the user can
    identify the correct index manually if auto-detect fails.
    """
    stack = _get_ts_stack(ts_name)
    if not stack: return {"found": False, "index": -1, "names": [], "note": "stack not found"}

    node = _find_node_by_path(stack, layer_path)
    if not node: return {"found": False, "index": -1, "names": [], "note": f"layer '{layer_path}' not found"}

    effects = []
    # Try content_effects first (standard SP 11 API for layer filters)
    if hasattr(node, 'content_effects'):
        try:
            effects = list(node.content_effects())
        except Exception as e:
            sp_log.warning(f"content_effects() failed: {e}")
    # Fallback: generic effects() method
    if not effects and hasattr(node, 'effects'):
        try:
            effects = list(node.effects())
        except Exception as e:
            sp_log.warning(f"effects() failed: {e}")

    names = []
    found = -1
    for i, ce in enumerate(effects):
        # Collect every string we can find on this effect node
        candidates = []
        for attr in ('name', 'label', 'resource_uid', 'filter_name'):
            val = getattr(ce, attr, None)
            if val is not None:
                try:
                    candidates.append(str(val() if callable(val) else val))
                except Exception:
                    pass
        # get_node_name as last resort
        candidates.append(get_node_name(ce))

        # Use the shortest non-empty candidate as the display name
        display = next((c for c in candidates if c), f"effect_{i}")
        names.append(display)

        # Match if any candidate contains "invert"
        if found == -1 and any("invert" in c.lower() for c in candidates):
            found = i

    return {"found": found != -1, "index": found, "names": names,
            "note": f"scanned {len(effects)} effect(s) via content_effects()"}

def _isolate_child(group_node, childName):
    if not hasattr(group_node, 'sub_layers'): return
    show = bool(childName)
    set_node_enabled(group_node, show)
    if not show: return
    for kid in group_node.sub_layers():
        set_node_enabled(kid, get_node_name(kid) == childName)

def _apply_export_state(ts_name: str, cfg: dict):
    stack = _get_ts_stack(ts_name)
    if not stack: return
    
    with sp_layerstack.ScopedModification("Skin Pack Exporter State"):
        text_node = _find_node_by_path(stack, cfg.get("textLayerPath"))
        if text_node:
            idx = cfg.get("invertIdx", -1)
            if idx >= 0 and hasattr(text_node, 'content_effects'):
                efx = list(text_node.content_effects())
                if idx < len(efx):
                    set_node_enabled(efx[idx], cfg.get("invertOn", True))
                    
            worn_paint = cfg.get("wornPaintName")
            if worn_paint and hasattr(text_node, 'mask_effects'):
                for me in text_node.mask_effects():
                    if get_node_name(me) == worn_paint:
                        set_node_enabled(me, cfg.get("wornPaintOn", False))
                        
        bp_node = _find_node_by_path(stack, cfg.get("blackPartsPath"))
        if bp_node:
            set_node_enabled(bp_node, True)
            if hasattr(bp_node, 'sub_layers'):
                bname = cfg.get("blackName")
                bwname = cfg.get("blackWornName")
                for kid in bp_node.sub_layers():
                    kn = get_node_name(kid)
                    if kn == bwname: set_node_enabled(kid, cfg.get("blackWornOn", False))
                    elif kn == bname: set_node_enabled(kid, True)
                    
        sp_node = _find_node_by_path(stack, cfg.get("skinPackPath"))
        if sp_node:
            is_def = cfg.get("isDefault", False)
            set_node_enabled(sp_node, not is_def)
            
            if not is_def:
                n_node = _find_node_by_path(stack, cfg.get("skinPackPath") + "|" + cfg.get("normalFolderName"))
                wp_node = _find_node_by_path(stack, cfg.get("skinPackPath") + "|" + cfg.get("wornPlasticName"))
                wm_node = _find_node_by_path(stack, cfg.get("skinPackPath") + "|" + cfg.get("wornMetalName"))
                br_node = _find_node_by_path(stack, cfg.get("skinPackPath") + "|" + cfg.get("brightFolderName"))
                
                ttype = cfg.get("type")
                if ttype == "normal":
                    if n_node: _isolate_child(n_node, cfg.get("activeSkin"))
                    if wp_node: set_node_enabled(wp_node, False)
                    if wm_node: set_node_enabled(wm_node, False)
                    if br_node: set_node_enabled(br_node, False)
                elif ttype == "worn":
                    if n_node: set_node_enabled(n_node, False)
                    if wp_node: _isolate_child(wp_node, cfg.get("wornPlasticSkin"))
                    if wm_node: _isolate_child(wm_node, cfg.get("wornMetalSkin"))
                    if br_node: set_node_enabled(br_node, False)
                elif ttype == "bright":
                    if n_node: set_node_enabled(n_node, False)
                    if wp_node: set_node_enabled(wp_node, False)
                    if wm_node: set_node_enabled(wm_node, False)
                    if br_node: _isolate_child(br_node, cfg.get("activeSkin"))

# =============================================================================
#  NAME UTILITIES
# =============================================================================

def clean_skin_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith("!Skin "):
        name = name[6:]
    name = _RE_SUFFIX.sub("", name).strip()
    low  = name.lower()
    if low == "black":      return "Default"
    if low == "black worn": return "Default Worn"
    return name


def _find_node(tree: List[Dict], name: str) -> Optional[Dict]:
    for node in tree:
        if node["name"] == name:
            return node
        found = _find_node(node.get("children", []), name)
        if found:
            return found
    return None


def _find_child(nodes: List[Dict], name: str) -> Optional[Dict]:
    return next((n for n in nodes if n["name"] == name), None)


# =============================================================================
#  MAIN DIALOG  (floating window)
# =============================================================================

class SkinExporterDialog(QtWidgets.QDialog):
    """
    Floating, non-modal window.
    Stays on top of SP but doesn't block interaction with SP itself.
    Can be re-opened from Python > Plugins > Skin Pack Exporter.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{PLUGIN_NAME}  v{PLUGIN_VERSION}")
        # Stay on top but remain non-modal so SP stays interactive
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowStaysOnTopHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMinimizeButtonHint
        )
        self.resize(420, 780)
        self._build_ui()

    # -------------------------------------------------------------------------
    #  UI CONSTRUCTION
    # -------------------------------------------------------------------------

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # -- Texture Set -------------------------------------------------------
        ts_group = QtWidgets.QGroupBox("Texture Set")
        ts_lay   = QtWidgets.QHBoxLayout(ts_group)
        self._ts_combo = QtWidgets.QComboBox()
        ts_ref = QtWidgets.QPushButton("Refresh")
        ts_ref.setFixedWidth(60)
        ts_ref.clicked.connect(self._refresh_ts)
        ts_lay.addWidget(self._ts_combo, 1)
        ts_lay.addWidget(ts_ref)
        root.addWidget(ts_group)

        # -- Layer Assignments -------------------------------------------------
        la_group = QtWidgets.QGroupBox("Layer Assignments")
        la_form  = QtWidgets.QFormLayout(la_group)
        la_form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapAllRows)

        # Helper note
        note = QtWidgets.QLabel(
            "Click a layer in SP's Layers panel, then press the\n"
            "matching Select button to capture its name."
        )
        note.setStyleSheet("color: #aaa; font-size: 10px;")
        la_form.addRow(note)

        self._skin_pack_edit   = self._select_row(la_form, "Skin Pack Folder:",   DEFAULT_SKIN_PACK)
        self._text_layer_edit  = self._select_row(la_form, "Text Fill Layer:",    DEFAULT_TEXT)
        self._black_parts_edit = self._select_row(la_form, "Black Parts Folder:", DEFAULT_BLACK)
        root.addWidget(la_group)

        # -- Sub-folder Names --------------------------------------------------
        sf_group = QtWidgets.QGroupBox("Skin Pack Sub-folder Names")
        sf_form  = QtWidgets.QFormLayout(sf_group)
        self._normal_edit    = self._text_row(sf_form, "Normal:",         DEFAULT_NORMAL)
        self._worn_plas_edit = self._text_row(sf_form, "Worn (Plastic):", DEFAULT_WORN_PLAS)
        self._worn_met_edit  = self._text_row(sf_form, "Worn (Metal):",   DEFAULT_WORN_MET)
        self._bright_edit    = self._text_row(sf_form, "Bright:",         DEFAULT_BRIGHT)
        root.addWidget(sf_group)

        # -- Text Layer Settings -----------------------------------------------
        tl_group = QtWidgets.QGroupBox("Text Layer Settings")
        tl_form  = QtWidgets.QFormLayout(tl_group)

        invert_row = QtWidgets.QHBoxLayout()
        self._invert_idx = QtWidgets.QSpinBox()
        self._invert_idx.setRange(-1, 30)
        self._invert_idx.setValue(-1)
        self._invert_idx.setSpecialValueText("\u26a0 Not set")
        self._invert_idx.setToolTip(
            "0-based index of the Invert filter on the Text fill layer.\n"
            "Press Scan to auto-detect. -1 means it will not be toggled."
        )
        scan_btn = QtWidgets.QPushButton("Scan")
        scan_btn.setFixedWidth(50)
        scan_btn.clicked.connect(self._scan_invert)
        invert_row.addWidget(self._invert_idx, 1)
        invert_row.addWidget(scan_btn)
        tl_form.addRow("Invert Filter Index:", invert_row)

        self._worn_paint_edit = QtWidgets.QLineEdit(DEFAULT_WORN_PAINT)
        self._worn_paint_edit.setToolTip(
            "Name of the paint layer inside the Text layer's black mask.\n"
            "Turned ON for 'worn' and 'default_worn' exports."
        )
        tl_form.addRow("Worn Paint Name:", self._worn_paint_edit)
        root.addWidget(tl_group)

        # -- Export Settings ---------------------------------------------------
        ex_group = QtWidgets.QGroupBox("Export Settings")
        ex_form  = QtWidgets.QFormLayout(ex_group)

        folder_row = QtWidgets.QHBoxLayout()
        self._folder_edit = QtWidgets.QLineEdit()
        self._folder_edit.setPlaceholderText("Choose export folder...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setFixedWidth(65)
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self._folder_edit, 1)
        folder_row.addWidget(browse_btn)
        ex_form.addRow("Export Folder:", folder_row)

        self._template_edit = QtWidgets.QLineEdit(DEFAULT_TEMPLATE)
        self._template_edit.setToolTip("Name of your SP export preset (e.g. 'Base Color').")
        ex_form.addRow("Output Preset:", self._template_edit)

        self._fmt_combo = QtWidgets.QComboBox()
        self._fmt_combo.addItems(["png", "tga", "tif", "jpeg", "exr"])
        ex_form.addRow("File Format:", self._fmt_combo)

        self._restore_check = QtWidgets.QCheckBox("Restore layer state after export")
        self._restore_check.setChecked(True)
        ex_form.addRow("", self._restore_check)
        root.addWidget(ex_group)

        root.addWidget(self._hline())

        # -- Diagnose button (helps debug JS issues) ---------------------------
        diag_row = QtWidgets.QHBoxLayout()
        diag_btn = QtWidgets.QPushButton("Diagnose Layer API")
        diag_btn.setToolTip(
            "Runs a quick Python API check and prints what layerstack\n"
            "functions are visible. Use if the layer tree fails to load."
        )
        diag_btn.clicked.connect(self._run_diag)
        diag_row.addWidget(diag_btn)
        root.addLayout(diag_row)

        # -- Action buttons ----------------------------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        self._preview_btn = QtWidgets.QPushButton("Preview")
        self._preview_btn.clicked.connect(self._preview)
        self._export_btn  = QtWidgets.QPushButton("Export All")
        self._export_btn.setStyleSheet(
            "QPushButton { background:#1e6b1e; color:white;"
            " font-weight:bold; padding:6px; }"
            "QPushButton:disabled { background:#3a3a3a; color:#666; }"
        )
        self._export_btn.clicked.connect(self._run_export)
        btn_row.addWidget(self._preview_btn, 1)
        btn_row.addWidget(self._export_btn, 2)
        root.addLayout(btn_row)

        # -- Log ---------------------------------------------------------------
        log_group = QtWidgets.QGroupBox("Log")
        log_lay   = QtWidgets.QVBoxLayout(log_group)
        self._log_view = QtWidgets.QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(140)
        self._log_view.setFont(QtGui.QFont("Courier New", 8))
        log_lay.addWidget(self._log_view)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.setFixedWidth(55)
        clear_btn.clicked.connect(self._log_view.clear)
        log_lay.addWidget(clear_btn, alignment=Qt.AlignRight)
        root.addWidget(log_group)

        self._refresh_ts()

    # -------------------------------------------------------------------------
    #  UI HELPERS
    # -------------------------------------------------------------------------

    def _select_row(self, form, label: str, default: str) -> QtWidgets.QLineEdit:
        """
        A row with a text field and a "Select" button.
        Clicking Select grabs the currently-highlighted layer name from SP.
        """
        row  = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit(default)
        btn  = QtWidgets.QPushButton("Select")
        btn.setFixedWidth(55)
        btn.setToolTip(
            "Click a layer in SP's layer panel first,\n"
            "then press this button to capture its name."
        )
        # Use a default-argument capture to avoid the late-binding closure trap.
        # clicked(checked: bool) -- we accept it with checked=False default.
        def _on_click(checked=False, _edit=edit):
            self._grab_selected_layer(_edit)
        btn.clicked.connect(_on_click)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        form.addRow(label, row)
        return edit

    def _text_row(self, form, label: str, default: str) -> QtWidgets.QLineEdit:
        edit = QtWidgets.QLineEdit(default)
        form.addRow(label, edit)
        return edit

    def _hline(self) -> QtWidgets.QFrame:
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        return line

    # -------------------------------------------------------------------------
    #  UI ACTIONS
    # -------------------------------------------------------------------------

    def _refresh_ts(self):
        self._ts_combo.clear()
        if not sp_project.is_open():
            self._ts_combo.addItem("(no project open)")
            return
        for ts in sp_textureset.all_texture_sets():
            self._ts_combo.addItem(ts.name())

    def _browse_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Export Folder", self._folder_edit.text()
        )
        if folder:
            self._folder_edit.setText(folder)

    def _grab_selected_layer(self, target_edit: QtWidgets.QLineEdit):
        """Read the currently-selected layer from SP and put its name in target_edit."""
        ts = self._ts_name()
        if not ts:
            self._ulog("No texture set selected. Refresh and pick one first.")
            return
        name = _get_selected_layer(ts)
        if name is None:
            self._ulog(
                "Could not get selected layer. Make sure:\n"
                "  1. A project is open\n"
                "  2. You have clicked exactly one layer in SP's Layers panel\n"
                "  3. Press Diagnose to verify the Python layer API is working"
            )
            return
        target_edit.setText(name)
        self._ulog(f"Captured: '{name}'")

    def _scan_invert(self):
        ts = self._ts_name()
        if not ts:
            return
        text_name = self._text_layer_edit.text().strip()
        if not text_name:
            self._ulog("Fill in the Text Fill Layer name first, then Scan.")
            return
        result = _scan_for_invert(ts, text_name)
        names = result.get("names", [])
        note  = result.get("note", "")

        # Always show the full numbered list so the user can identify by index
        if names:
            self._ulog(f"Effects found on '{text_name}' ({note}):")
            for i, nm in enumerate(names):
                marker = " <-- Invert" if i == result.get("index", -1) else ""
                self._ulog(f"  [{i}]  {nm}{marker}")
        else:
            self._ulog(
                f"No effects found on '{text_name}'. ({note})\n"
                f"Check that:\n"
                f"  - The Text layer name is correct (use Select to capture it)\n"
                f"  - The Invert filter has been added to that layer\n"
                f"  - Run Diagnose to verify the Python API is working"
            )
            return

        if result.get("found"):
            self._invert_idx.setValue(result["index"])
            self._ulog(f"Auto-set index to {result['index']}.")
        else:
            self._ulog(
                f"Could not auto-detect 'Invert' by name.\n"
                f"Look at the list above and type the correct index manually."
            )

    def _run_diag(self):
        """Run the Python layer API diagnostic and dump results to the log."""
        ts = self._ts_name()
        self._ulog("--- Layer API Diagnostic ---")
        if not ts:
            self._ulog("No texture set selected.")
            return
        result = _run_diag(ts)
        for k, v in result.items():
            self._ulog(f"  {k}: {v}")
        self._ulog("----------------------------")

    # -------------------------------------------------------------------------

    def _ts_name(self) -> Optional[str]:
        name = self._ts_combo.currentText()
        return None if (not name or name == "(no project open)") else name

    def _ulog(self, msg: str, level: str = "info"):
        self._log_view.appendPlainText(msg)
        _log(msg, level)
        QtWidgets.QApplication.processEvents()

    # -------------------------------------------------------------------------
    #  EXPORT PLAN
    # -------------------------------------------------------------------------

    def _build_plan(self):
        ts = self._ts_name()
        if not ts:
            return None, "No texture set selected."

        tree = _get_layer_tree(ts)
        if tree is None:
            # Provide the diagnostic output automatically
            diag = _run_diag(ts)
            diag_str = ", ".join(f"{k}={v}" for k, v in diag.items())
            return None, (
                f"Could not retrieve layer tree from SP.\n\n"
                f"JS diagnostic:\n  {diag_str}\n\n"
                f"Common causes:\n"
                f"  - Project not fully loaded yet\n"
                f"  - SP version mismatch (this plugin targets SP 11.1.2)\n"
                f"  - Press 'Diagnose JS Bridge' for more detail"
            )

        sp_name     = self._skin_pack_edit.text().strip()
        bp_name     = self._black_parts_edit.text().strip()
        norm_name   = self._normal_edit.text().strip()
        wp_name     = self._worn_plas_edit.text().strip()
        wm_name     = self._worn_met_edit.text().strip()
        bright_name = self._bright_edit.text().strip()

        skin_pack = _find_node(tree, sp_name)
        if skin_pack is None:
            return None, (
                f"Skin Pack folder '{sp_name}' not found.\n"
                f"Use the Select button next to 'Skin Pack Folder' to pick it."
            )

        sp_kids          = skin_pack.get("children", [])
        normal_folder    = _find_child(sp_kids, norm_name)
        worn_plas_folder = _find_child(sp_kids, wp_name)
        worn_met_folder  = _find_child(sp_kids, wm_name)
        bright_folder    = _find_child(sp_kids, bright_name)

        tasks: List[Dict[str, Any]] = []

        # Normal
        if normal_folder:
            for child in normal_folder.get("children", []):
                clean = clean_skin_name(child["name"])
                if clean in ("Default", "Default Worn"):
                    continue
                tasks.append({
                    "filename":       clean,
                    "type":           "normal",
                    "activeSkin":     child["name"],
                    "wornPlasticSkin": None,
                    "wornMetalSkin":  None,
                })

        # Worn — pair Plastic + Metal by clean name
        if worn_plas_folder or worn_met_folder:
            plas_map: Dict[str, str] = {}
            met_map:  Dict[str, str] = {}
            if worn_plas_folder:
                for c in worn_plas_folder.get("children", []):
                    plas_map[clean_skin_name(c["name"])] = c["name"]
            if worn_met_folder:
                for c in worn_met_folder.get("children", []):
                    met_map[clean_skin_name(c["name"])] = c["name"]
            for clean in sorted(set(plas_map) | set(met_map)):
                if clean in ("Default", "Default Worn"):
                    continue
                tasks.append({
                    "filename":       clean,
                    "type":           "worn",
                    "activeSkin":     None,
                    "wornPlasticSkin": plas_map.get(clean),
                    "wornMetalSkin":  met_map.get(clean),
                })

        # Bright
        if bright_folder:
            for child in bright_folder.get("children", []):
                tasks.append({
                    "filename":       clean_skin_name(child["name"]),
                    "type":           "bright",
                    "activeSkin":     child["name"],
                    "wornPlasticSkin": None,
                    "wornMetalSkin":  None,
                })

        # Black Parts
        black_parts = _find_node(tree, bp_name) if bp_name else None
        if black_parts:
            black_name = None
            bworn_name = None
            for child in black_parts.get("children", []):
                cn = clean_skin_name(child["name"])
                if cn == "Default"      and black_name is None: black_name = child["name"]
                if cn == "Default Worn" and bworn_name is None: bworn_name = child["name"]
            if black_name:
                tasks.append({"filename": "Default",
                               "type": "default",
                               "activeSkin": None,
                               "wornPlasticSkin": None,
                               "wornMetalSkin": None})
            if bworn_name:
                tasks.append({"filename": "Default Worn",
                               "type": "default_worn",
                               "activeSkin": None,
                               "wornPlasticSkin": None,
                               "wornMetalSkin": None})

        return tasks, None

    # -------------------------------------------------------------------------
    #  PREVIEW
    # -------------------------------------------------------------------------

    def _preview(self):
        self._log_view.clear()
        if not sp_project.is_open():
            self._ulog("No project open.")
            return
        tasks, err = self._build_plan()
        if err:
            self._ulog(f"ERROR: {err}")
            return
        fmt   = self._fmt_combo.currentText()
        col_w = max((len(t["filename"]) for t in tasks), default=0) + 2
        self._ulog(f"Export plan -- {len(tasks)} file(s):\n")
        for i, t in enumerate(tasks, 1):
            notes = []
            if t["wornPlasticSkin"] and t["wornMetalSkin"]: notes.append("plastic+metal")
            elif t["wornPlasticSkin"]:                       notes.append("plastic only")
            elif t["wornMetalSkin"]:                         notes.append("metal only")
            note = f"  ({', '.join(notes)})" if notes else ""
            self._ulog(
                f"  {i:>3}.  [{t['type']:<12}]  "
                f"{t['filename'].ljust(col_w)}.{fmt}{note}"
            )

    # -------------------------------------------------------------------------
    #  EXPORT RUNNER
    # -------------------------------------------------------------------------

    def _run_export(self):
        if not sp_project.is_open():
            QtWidgets.QMessageBox.warning(self, "No Project", "No project is open.")
            return
        export_dir = self._folder_edit.text().strip()
        if not export_dir:
            QtWidgets.QMessageBox.warning(self, "No Folder", "Please pick an export folder.")
            return

        # Pre-flight warnings
        warns = []
        if self._invert_idx.value() < 0:
            warns.append(
                "Invert Filter Index is not set -- the filter will NOT be toggled.\n"
                "  Bright skins will have the same invert state as Normal skins.\n"
                "  Press Scan next to the Invert Filter Index to fix this."
            )
        if not self._text_layer_edit.text().strip():
            warns.append("Text Fill Layer is empty -- text effects will be skipped.")
        if not self._black_parts_edit.text().strip():
            warns.append("Black Parts Folder is empty -- Default/Default Worn skipped.")
        if warns:
            msg   = "\n\n".join(f"  * {w}" for w in warns)
            reply = QtWidgets.QMessageBox.warning(
                self, "Export Warnings",
                f"Proceed anyway?\n\n{msg}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        tasks, err = self._build_plan()
        if err:
            QtWidgets.QMessageBox.critical(self, "Plan Error", err)
            return
        if not tasks:
            QtWidgets.QMessageBox.information(self, "Nothing to Export",
                "No exportable skins were found.")
            return

        os.makedirs(export_dir, exist_ok=True)

        ts_name    = self._ts_name()
        file_fmt   = self._fmt_combo.currentText()
        template   = self._template_edit.text().strip()
        sp_name    = self._skin_pack_edit.text().strip()
        bp_name    = self._black_parts_edit.text().strip()
        text_name  = self._text_layer_edit.text().strip()
        invert_idx = self._invert_idx.value()
        worn_paint = self._worn_paint_edit.text().strip()

        # Resolve Black Parts child names once
        tree        = _get_layer_tree(ts_name)
        black_parts = _find_node(tree, bp_name) if (tree and bp_name) else None
        black_name  = None
        bworn_name  = None
        if black_parts:
            for child in black_parts.get("children", []):
                cn = clean_skin_name(child["name"])
                if cn == "Default"      and black_name is None: black_name = child["name"]
                if cn == "Default Worn" and bworn_name is None: bworn_name = child["name"]

        # Snapshot
        snap_json = ""
        if self._restore_check.isChecked():
            self._ulog("Snapshotting layer state...")
            snap_json = _snapshot_visibility(ts_name)

        self._log_view.clear()
        self._export_btn.setEnabled(False)
        self._ulog(f"Starting export of {len(tasks)} texture(s) to:\n  {export_dir}\n")

        ok_count = fail_count = 0

        progress = QtWidgets.QProgressDialog("Exporting...", "Cancel", 0, len(tasks), self)
        progress.setWindowTitle(PLUGIN_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        for i, task in enumerate(tasks):
            if progress.wasCanceled():
                self._ulog("\nExport cancelled by user.")
                break

            progress.setValue(i)
            progress.setLabelText(
                f"Exporting {task['filename']}.{file_fmt}  ({i+1}/{len(tasks)})"
            )
            self._ulog(f"[{i+1}/{len(tasks)}] {task['filename']}.{file_fmt}  ({task['type']})")

            try:
                ttype      = task["type"]
                is_worn    = ttype in ("worn", "default_worn")
                is_default = ttype in ("default", "default_worn")

                cfg = {
                    "type":             ttype,
                    "isDefault":        is_default,
                    "invertOn":         ttype != "bright",
                    "invertIdx":        invert_idx,
                    "wornPaintOn":      is_worn,
                    "blackWornOn":      is_worn,
                    "textLayerPath":    text_name,
                    "wornPaintName":    worn_paint,
                    "skinPackPath":     sp_name,
                    "blackPartsPath":   bp_name,
                    "blackName":        black_name or "",
                    "blackWornName":    bworn_name or "",
                    "normalFolderName": self._normal_edit.text().strip(),
                    "wornPlasticName":  self._worn_plas_edit.text().strip(),
                    "wornMetalName":    self._worn_met_edit.text().strip(),
                    "brightFolderName": self._bright_edit.text().strip(),
                    "activeSkin":       task.get("activeSkin") or "",
                    "wornPlasticSkin":  task.get("wornPlasticSkin") or "",
                    "wornMetalSkin":    task.get("wornMetalSkin") or "",
                }

                _apply_export_state(ts_name, cfg)
                exported = self._do_export(
                    ts_name, template, file_fmt, export_dir, task["filename"]
                )
                if exported:
                    ok_count += 1
                    self._ulog("  OK")
                else:
                    fail_count += 1
                    self._ulog("  Export returned no result -- check SP's log.")

            except Exception:
                fail_count += 1
                self._ulog(f"  Exception:\n{traceback.format_exc()}", "error")

        progress.setValue(len(tasks))

        if self._restore_check.isChecked() and snap_json:
            _restore_visibility(ts_name, snap_json)
            self._ulog("\nLayer state restored to original.")

        self._ulog(f"\n{'='*40}")
        self._ulog(f"Done -- {ok_count} succeeded, {fail_count} failed.")
        self._export_btn.setEnabled(True)

    # -------------------------------------------------------------------------
    #  SP EXPORT CALL
    # -------------------------------------------------------------------------

    def _do_export(self, ts_name, template, file_fmt, export_dir, filename) -> bool:
        config = {
            "exportPath":          export_dir,
            "defaultExportPreset": template,
            "exportList":          [{"rootPath": ts_name}],
            "exportParameters": [{
                "rootPath": ts_name,
                "filter":   {},
                "parameters": {
                    "fileFormat":       file_fmt,
                    "bitDepth":         "8",
                    "dithering":        True,
                    "paddingAlgorithm": "infinite",
                    "dilationDistance": 16,
                    "fileName":         filename,
                }
            }]
        }

        result = sp_export.export_project_textures(config)

        # Auto-rename if SP appended preset name (e.g. "Tan_Base Color.png")
        expected = os.path.join(export_dir, f"{filename}.{file_fmt}")
        if not os.path.exists(expected):
            for pattern in (
                os.path.join(export_dir, f"{filename}_*.{file_fmt}"),
                os.path.join(export_dir, f"{filename} *.{file_fmt}"),
            ):
                matches = glob.glob(pattern)
                if len(matches) == 1:
                    os.rename(matches[0], expected)
                    _log(f"  Renamed '{os.path.basename(matches[0])}' -> '{filename}.{file_fmt}'")
                    break

        return result is not None


# =============================================================================
#  PLUGIN ENTRY POINTS
# =============================================================================

_dialog: Optional[SkinExporterDialog] = None
_action: Optional[Any] = None   # QAction in SP's Python menu


def start_plugin():
    global _dialog, _action

    # Create the action
    _action = QAction("Open Skin Pack Exporter", sp_ui.get_main_window())
    _action.triggered.connect(_show_window)
    
    # Add it to the 'Window' menu so it persists even if you close the panel
    sp_ui.add_action(sp_ui.ApplicationMenu.Window, _action)

    # Open the window immediately on load
    _show_window()
    _log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} loaded. Re-open via Window > Open Skin Pack Exporter")


def _show_window():
    """Open or bring the floating window to the front."""
    global _dialog
    if _dialog is None or not _dialog.isVisible():
        _dialog = SkinExporterDialog(sp_ui.get_main_window())
    _dialog.show()
    _dialog.raise_()
    _dialog.activateWindow()


def close_plugin():
    global _dialog, _action
    if _dialog is not None:
        _dialog.close()
        _dialog = None
    if _action is not None:
        sp_ui.delete_ui_element(_action)
        _action = None
    _log(f"{PLUGIN_NAME} unloaded.")


if __name__ == "__main__":
    print("Run this as a Substance Painter plugin, not standalone.")