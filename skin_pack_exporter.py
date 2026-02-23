"""
Skin Pack Exporter -- Substance Painter Plugin v1.1.0
Compatible with SP 11.1.2+

INSTALLATION:
  Place this file in:
  %USERPROFILE%/Documents/Adobe/Adobe Substance 3D Painter/python/plugins/skin_pack_exporter.py

  Then in Substance Painter: Python > Plugins > Skin Pack Exporter

API NOTE:
  SP's Python SDK does NOT expose substance_painter.layers.
  All layer manipulation goes through substance_painter.js.evaluate()
  which runs code inside SP's JavaScript engine (alg.* namespace).

EXPORT TYPES:
  normal       : Text invert ON,  worn OFF, black worn OFF  | Normal folder
  worn         : Text invert ON,  worn ON,  black worn ON   | Worn (Plastic) + Worn (Metal)
  bright       : Text invert OFF, worn OFF, black worn OFF  | Bright folder
  default      : Text invert ON,  worn OFF, black worn OFF  | Black Parts only
  default_worn : Text invert ON,  worn ON,  black worn ON   | Black Parts only

NAME MAPPING:
  !Skin Tan (Metal)        -> Tan
  !Skin Tan Worn (Plastic) -> Tan Worn
  !Skin Black (Any Mat)    -> Default
  !Skin Black Worn (Metal) -> Default Worn
  !Skin Gold (Any Mat)     -> Gold
"""

import os
import re
import json
import glob
import traceback
from typing import Optional, List, Dict, Any

# -- Qt (SP 11 ships PySide2; PySide6 fallback just in case) ------------------
try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui   # type: ignore
    from PySide6.QtCore import Qt

# -- Substance Painter Python API ---------------------------------------------
# NOTE: substance_painter.layers does NOT exist in SP 11.
# Layer access must go through the JavaScript bridge (substance_painter.js).
import substance_painter.ui         as sp_ui
import substance_painter.project    as sp_project
import substance_painter.textureset as sp_textureset
import substance_painter.export     as sp_export
import substance_painter.logging    as sp_log
import substance_painter.js         as sp_js   # JS bridge for layer ops


# =============================================================================
#  CONSTANTS
# =============================================================================

PLUGIN_NAME    = "Skin Pack Exporter"
PLUGIN_VERSION = "1.1.0"

DEFAULT_SKIN_PACK_NAME  = "!!Skin Pack (All)"
DEFAULT_TEXT_LAYER      = "Text"
DEFAULT_BLACK_PARTS     = "Black Parts"
DEFAULT_NORMAL_FOLDER   = "Normal"
DEFAULT_WORN_PLASTIC    = "Worn (Plastic)"
DEFAULT_WORN_METAL      = "Worn (Metal)"
DEFAULT_BRIGHT_FOLDER   = "Bright"
DEFAULT_WORN_PAINT      = "Worn"
DEFAULT_EXPORT_TEMPLATE = "Base Color"

_RE_SUFFIX = re.compile(r'\s*\([^)]*\)\s*$')


# =============================================================================
#  LOGGING
# =============================================================================

def _log(msg: str, level: str = "info"):
    lvl = {"info": sp_log.INFO, "warn": sp_log.WARNING, "error": sp_log.ERROR}
    sp_log.log(lvl.get(level, sp_log.INFO), PLUGIN_NAME, str(msg))


# =============================================================================
#  JAVASCRIPT BRIDGE
#  sp_js.evaluate(code) runs code in SP's built-in JS engine and returns the
#  result.  We JSON.stringify complex objects on the JS side so Python always
#  receives plain strings it can json.loads().
# =============================================================================

def _js(code: str) -> Any:
    """Evaluate JS in SP's engine. Returns None and logs on failure."""
    try:
        return sp_js.evaluate(code)
    except Exception as exc:
        _log(f"JS evaluate error: {exc}\nCode: {code[:300]}", "error")
        return None


# ---------------------------------------------------------------------------
#  JS: get full layer tree as JSON
#  Returns a list of node dicts: {name, type, visible, children, maskNodes,
#  effects:[{index,name,enabled}]}
# ---------------------------------------------------------------------------
_JS_GET_TREE = """
(function(tsName) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return JSON.stringify([]);

    function getEffects(node) {
        var out = [];
        try {
            var efx = alg.layers.effects(node) || [];
            for (var i = 0; i < efx.length; i++) {
                out.push({
                    index:   i,
                    name:    efx[i].name || ("effect_" + i),
                    enabled: alg.layers.isEffectEnabled(efx[i])
                });
            }
        } catch(e) {}
        return out;
    }

    function getMaskKids(node) {
        var out = [];
        try {
            var mask = alg.layers.maskNodes(node) || [];
            for (var i = 0; i < mask.length; i++) {
                out.push({
                    name:     mask[i].name || "",
                    type:     "MaskNode",
                    visible:  alg.layers.visibility(mask[i]),
                    children: [],
                    maskNodes:[],
                    effects:  []
                });
            }
        } catch(e) {}
        return out;
    }

    function walk(parent) {
        var nodes = alg.layers.nodes(parent) || [];
        var result = [];
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            result.push({
                name:      n.name || ("layer_" + i),
                type:      n.type || "Unknown",
                visible:   alg.layers.visibility(n),
                children:  walk(n),
                maskNodes: getMaskKids(n),
                effects:   getEffects(n)
            });
        }
        return result;
    }

    return JSON.stringify(walk(stack));
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  JS: lightweight name+type+children tree (for the layer picker dialog)
# ---------------------------------------------------------------------------
_JS_NAME_TREE = """
(function(tsName) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return JSON.stringify([]);
    function walk(parent) {
        var nodes = alg.layers.nodes(parent) || [];
        var out = [];
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            out.push({
                name:     n.name || ("layer_" + i),
                type:     n.type || "Unknown",
                children: walk(n)
            });
        }
        return out;
    }
    return JSON.stringify(walk(stack));
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  JS: snapshot all visibility values into a {path: bool} JSON map
#  'path' uses "|" as a separator.  Mask children use the sentinel __mask__.
# ---------------------------------------------------------------------------
_JS_SNAPSHOT = """
(function(tsName) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return JSON.stringify({});
    var snap = {};

    function walk(parent, prefix) {
        var nodes = alg.layers.nodes(parent) || [];
        for (var i = 0; i < nodes.length; i++) {
            var n   = nodes[i];
            var nm  = n.name || ("layer_" + i);
            var pth = prefix ? (prefix + "|" + nm) : nm;
            snap[pth] = alg.layers.visibility(n);
            walk(n, pth);
            try {
                var mask = alg.layers.maskNodes(n) || [];
                for (var j = 0; j < mask.length; j++) {
                    var mn  = mask[j];
                    var mnm = mn.name || ("m" + j);
                    snap[pth + "|__mask__|" + mnm] = alg.layers.visibility(mn);
                }
            } catch(e) {}
        }
    }

    walk(stack, "");
    return JSON.stringify(snap);
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  JS: restore visibility from a {path: bool} snapshot
# ---------------------------------------------------------------------------
_JS_RESTORE = """
(function(tsName, snapJson) {
    var snap  = JSON.parse(snapJson);
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return false;

    function nodeByPath(root, parts) {
        if (!parts.length) return root;
        var nodes = alg.layers.nodes(root) || [];
        for (var i = 0; i < nodes.length; i++) {
            if ((nodes[i].name || ("layer_" + i)) === parts[0]) {
                return nodeByPath(nodes[i], parts.slice(1));
            }
        }
        return null;
    }

    function maskNodeByPath(root, parts) {
        var nodes = alg.layers.nodes(root) || [];
        for (var i = 0; i < nodes.length; i++) {
            var n  = nodes[i];
            var nm = n.name || ("layer_" + i);
            if (nm === parts[0]) {
                if (parts[1] === "__mask__") {
                    var mask = alg.layers.maskNodes(n) || [];
                    for (var j = 0; j < mask.length; j++) {
                        if ((mask[j].name || ("m" + j)) === parts[2]) return mask[j];
                    }
                    return null;
                }
                return maskNodeByPath(n, parts.slice(1));
            }
        }
        return null;
    }

    for (var path in snap) {
        if (!snap.hasOwnProperty(path)) continue;
        var parts = path.split("|");
        var node  = parts.indexOf("__mask__") !== -1
                      ? maskNodeByPath(stack, parts)
                      : nodeByPath(stack, parts);
        if (node) {
            try { alg.layers.setVisibility(node, snap[path]); } catch(e) {}
        }
    }
    return true;
})(TSNAME, SNAP)
"""

# ---------------------------------------------------------------------------
#  JS: scan a layer's effects for the first one containing "invert"
# ---------------------------------------------------------------------------
_JS_SCAN_INVERT = """
(function(tsName, layerPath) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return JSON.stringify({found:false, index:-1, names:[]});
    var parts = layerPath.split("|");
    var cur   = stack;
    for (var i = 0; i < parts.length; i++) {
        var nodes = alg.layers.nodes(cur) || [];
        var next  = null;
        for (var j = 0; j < nodes.length; j++) {
            if (nodes[j].name === parts[i]) { next = nodes[j]; break; }
        }
        if (!next) return JSON.stringify({found:false, index:-1, names:[]});
        cur = next;
    }
    var efx   = [];
    try { efx = alg.layers.effects(cur) || []; } catch(e) {}
    var names = [];
    var found = -1;
    for (var k = 0; k < efx.length; k++) {
        var nm = efx[k].name || ("effect_" + k);
        names.push(nm);
        if (found === -1 && nm.toLowerCase().indexOf("invert") !== -1) found = k;
    }
    return JSON.stringify({found: found !== -1, index: found, names: names});
})(TSNAME, LAYERPATH)
"""

# ---------------------------------------------------------------------------
#  JS: apply the full export state for one task in a single evaluate() call
# ---------------------------------------------------------------------------
_JS_APPLY_STATE = """
(function(tsName, cfg) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return "error:no_stack";

    // Find node by "|"-delimited path from the stack root
    function byPath(root, pathStr) {
        if (!pathStr) return null;
        var parts = pathStr.split("|");
        var cur   = root;
        for (var i = 0; i < parts.length; i++) {
            var nodes = alg.layers.nodes(cur) || [];
            var found = null;
            for (var j = 0; j < nodes.length; j++) {
                if (nodes[j].name === parts[i]) { found = nodes[j]; break; }
            }
            if (!found) return null;
            cur = found;
        }
        return cur;
    }

    // Make one child visible inside a group; hide the rest; hide group if no child
    function isolateChild(groupPath, childName) {
        var group = byPath(stack, groupPath);
        if (!group) return;
        var show = (childName !== null && childName !== "");
        alg.layers.setVisibility(group, show);
        if (!show) return;
        var kids = alg.layers.nodes(group) || [];
        for (var i = 0; i < kids.length; i++) {
            alg.layers.setVisibility(kids[i], kids[i].name === childName);
        }
    }

    // Hide a group entirely
    function hideGroup(groupPath) {
        var g = byPath(stack, groupPath);
        if (g) alg.layers.setVisibility(g, false);
    }

    // -- 1. Text fill layer --------------------------------------------------
    if (cfg.textLayerPath) {
        var textNode = byPath(stack, cfg.textLayerPath);
        if (textNode) {
            // Toggle invert filter
            if (cfg.invertIdx >= 0) {
                try {
                    var efx = alg.layers.effects(textNode) || [];
                    if (efx[cfg.invertIdx]) {
                        alg.layers.setEffectEnabled(efx[cfg.invertIdx], cfg.invertOn);
                    }
                } catch(e) {}
            }
            // Toggle worn paint layer inside the black mask
            if (cfg.wornPaintName) {
                try {
                    var mask = alg.layers.maskNodes(textNode) || [];
                    for (var mi = 0; mi < mask.length; mi++) {
                        if (mask[mi].name === cfg.wornPaintName) {
                            alg.layers.setVisibility(mask[mi], cfg.wornPaintOn);
                        }
                    }
                } catch(e) {}
            }
        }
    }

    // -- 2. Black Parts folder -----------------------------------------------
    if (cfg.blackPartsPath) {
        var bp = byPath(stack, cfg.blackPartsPath);
        if (bp) {
            alg.layers.setVisibility(bp, true);
            var bpKids = alg.layers.nodes(bp) || [];
            for (var bi = 0; bi < bpKids.length; bi++) {
                var bname = bpKids[bi].name;
                if (bname === cfg.blackWornName) {
                    alg.layers.setVisibility(bpKids[bi], cfg.blackWornOn);
                } else if (bname === cfg.blackName) {
                    alg.layers.setVisibility(bpKids[bi], true);
                }
            }
        }
    }

    // -- 3. Skin Pack folder -------------------------------------------------
    if (cfg.skinPackPath) {
        var sp = byPath(stack, cfg.skinPackPath);
        if (sp) {
            // Hide entire skin pack for default/default_worn (Black Parts carries those)
            alg.layers.setVisibility(sp, !cfg.isDefault);

            if (!cfg.isDefault) {
                var normPath   = cfg.skinPackPath + "|" + cfg.normalFolderName;
                var wPlasPath  = cfg.skinPackPath + "|" + cfg.wornPlasticName;
                var wMetPath   = cfg.skinPackPath + "|" + cfg.wornMetalName;
                var brightPath = cfg.skinPackPath + "|" + cfg.brightFolderName;

                if (cfg.type === "normal") {
                    isolateChild(normPath,   cfg.activeSkin);
                    hideGroup(wPlasPath);
                    hideGroup(wMetPath);
                    hideGroup(brightPath);
                } else if (cfg.type === "worn") {
                    // Normal folder hidden entirely for worn exports
                    hideGroup(normPath);
                    // Both worn folders active at once (one child each)
                    isolateChild(wPlasPath, cfg.wornPlasticSkin);
                    isolateChild(wMetPath,  cfg.wornMetalSkin);
                    hideGroup(brightPath);
                } else if (cfg.type === "bright") {
                    hideGroup(normPath);
                    hideGroup(wPlasPath);
                    hideGroup(wMetPath);
                    isolateChild(brightPath, cfg.activeSkin);
                }
            }
        }
    }

    return "ok";
})(TSNAME, CFG)
"""


# =============================================================================
#  PYTHON-SIDE HELPERS that wrap the JS calls
# =============================================================================

def _build_js(template: str, replacements: Dict[str, str]) -> str:
    code = template
    for key, val in replacements.items():
        code = code.replace(key, val)
    return code


def _get_layer_tree(ts_name: str) -> List[Dict]:
    raw = _js(_build_js(_JS_GET_TREE, {"TSNAME": json.dumps(ts_name)}))
    if raw is None:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        _log(f"Layer tree parse failed: {exc}", "error")
        return []


def _get_name_tree(ts_name: str) -> List[Dict]:
    raw = _js(_build_js(_JS_NAME_TREE, {"TSNAME": json.dumps(ts_name)}))
    if raw is None:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []


def _snapshot_visibility(ts_name: str) -> str:
    raw = _js(_build_js(_JS_SNAPSHOT, {"TSNAME": json.dumps(ts_name)}))
    if raw is None:
        return "{}"
    return raw if isinstance(raw, str) else json.dumps(raw)


def _restore_visibility(ts_name: str, snap_json: str):
    _js(_build_js(_JS_RESTORE, {
        "TSNAME": json.dumps(ts_name),
        "SNAP":   snap_json,
    }))


def _scan_for_invert(ts_name: str, layer_path: str) -> Dict:
    raw = _js(_build_js(_JS_SCAN_INVERT, {
        "TSNAME":    json.dumps(ts_name),
        "LAYERPATH": json.dumps(layer_path),
    }))
    if raw is None:
        return {"found": False, "index": -1, "names": []}
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"found": False, "index": -1, "names": []}


def _apply_export_state(ts_name: str, cfg: Dict):
    result = _js(_build_js(_JS_APPLY_STATE, {
        "TSNAME": json.dumps(ts_name),
        "CFG":    json.dumps(cfg),
    }))
    if result != "ok":
        _log(f"apply_state returned: {result}", "warn")


# =============================================================================
#  NAME UTILITIES
# =============================================================================

def clean_skin_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith("!Skin "):
        name = name[6:]
    name = _RE_SUFFIX.sub("", name).strip()
    lower = name.lower()
    if lower == "black":
        return "Default"
    if lower == "black worn":
        return "Default Worn"
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
    for n in nodes:
        if n["name"] == name:
            return n
    return None


# =============================================================================
#  LAYER PICKER DIALOG
# =============================================================================

class LayerPickerDialog(QtWidgets.QDialog):

    def __init__(self, ts_name: str, parent=None, title: str = "Select a Layer"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(340, 520)
        self._ts_name        = ts_name
        self._selected_name: Optional[str] = None

        layout = QtWidgets.QVBoxLayout(self)

        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Filter layers...")
        self._search.textChanged.connect(self._filter_tree)
        layout.addWidget(self._search)

        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(self._accept)
        layout.addWidget(self._tree)

        btn_row    = QtWidgets.QHBoxLayout()
        ok_btn     = QtWidgets.QPushButton("Select")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._populate()

    def _populate(self):
        self._tree.clear()
        for node in _get_name_tree(self._ts_name):
            self._add_item(node, self._tree.invisibleRootItem())
        self._tree.expandAll()

    def _add_item(self, node: Dict, parent_item):
        name = node["name"]
        item = QtWidgets.QTreeWidgetItem(parent_item, [name])
        item.setData(0, Qt.UserRole, name)
        is_group = bool(node.get("children")) or "group" in node.get("type", "").lower()
        if is_group:
            item.setForeground(0, QtGui.QColor("#e8a84e"))
            item.setFont(0, QtGui.QFont("", -1, QtGui.QFont.Bold))
        for child in node.get("children", []):
            self._add_item(child, item)

    def _filter_tree(self, text: str):
        text = text.lower()

        def _any_match(item) -> bool:
            if text in item.text(0).lower():
                return True
            return any(_any_match(item.child(i)) for i in range(item.childCount()))

        def _apply(item):
            item.setHidden(not _any_match(item))
            for i in range(item.childCount()):
                _apply(item.child(i))

        for i in range(self._tree.invisibleRootItem().childCount()):
            _apply(self._tree.invisibleRootItem().child(i))

    def _accept(self):
        item = self._tree.currentItem()
        if item:
            self._selected_name = item.data(0, Qt.UserRole)
            self.accept()

    def selected_name(self) -> Optional[str]:
        return self._selected_name


# =============================================================================
#  MAIN EXPORTER WIDGET
# =============================================================================

class SkinExporterWidget(QtWidgets.QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(PLUGIN_NAME)
        self._build_ui()

    # -------------------------------------------------------------------------
    #  UI CONSTRUCTION
    # -------------------------------------------------------------------------

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        title_lbl = QtWidgets.QLabel(
            f'<b style="font-size:13px">{PLUGIN_NAME}</b>'
            f'<span style="color:#777; font-size:10px">  v{PLUGIN_VERSION}</span>'
        )
        root.addWidget(title_lbl)
        root.addWidget(self._hline())

        # -- Texture Set -------------------------------------------------------
        ts_group = QtWidgets.QGroupBox("Texture Set")
        ts_lay   = QtWidgets.QHBoxLayout(ts_group)
        self._ts_combo = QtWidgets.QComboBox()
        ts_ref = QtWidgets.QPushButton("Refresh")
        ts_ref.setFixedWidth(56)
        ts_ref.clicked.connect(self._refresh_ts)
        ts_lay.addWidget(self._ts_combo, 1)
        ts_lay.addWidget(ts_ref)
        root.addWidget(ts_group)

        # -- Layer Assignments -------------------------------------------------
        la_group = QtWidgets.QGroupBox("Layer Assignments")
        la_form  = QtWidgets.QFormLayout(la_group)
        la_form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapAllRows)
        self._skin_pack_edit   = self._picker_row(la_form, "Skin Pack Folder:",   DEFAULT_SKIN_PACK_NAME)
        self._text_layer_edit  = self._picker_row(la_form, "Text Fill Layer:",    DEFAULT_TEXT_LAYER)
        self._black_parts_edit = self._picker_row(la_form, "Black Parts Folder:", DEFAULT_BLACK_PARTS)
        root.addWidget(la_group)

        # -- Sub-folder Names --------------------------------------------------
        sf_group = QtWidgets.QGroupBox("Skin Pack Sub-folder Names")
        sf_form  = QtWidgets.QFormLayout(sf_group)
        self._normal_edit    = self._text_row(sf_form, "Normal:",         DEFAULT_NORMAL_FOLDER)
        self._worn_plas_edit = self._text_row(sf_form, "Worn (Plastic):", DEFAULT_WORN_PLASTIC)
        self._worn_met_edit  = self._text_row(sf_form, "Worn (Metal):",   DEFAULT_WORN_METAL)
        self._bright_edit    = self._text_row(sf_form, "Bright:",         DEFAULT_BRIGHT_FOLDER)
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
            "0-based index of the Invert filter in the Text layer's effect list.\n"
            "Use Scan to auto-detect, or type the index manually.\n"
            "-1 = filter will not be toggled during export."
        )
        scan_btn = QtWidgets.QPushButton("Scan")
        scan_btn.setFixedWidth(48)
        scan_btn.setToolTip("Scan the Text layer's effects and detect the Invert filter.")
        scan_btn.clicked.connect(self._scan_invert)
        invert_row.addWidget(self._invert_idx, 1)
        invert_row.addWidget(scan_btn)
        tl_form.addRow("Invert Filter Index:", invert_row)

        self._worn_paint_edit = QtWidgets.QLineEdit(DEFAULT_WORN_PAINT)
        self._worn_paint_edit.setToolTip(
            "Name of the paint layer inside the Text layer's black mask.\n"
            "Turned ON for Worn and Default Worn exports."
        )
        tl_form.addRow("Worn Paint Layer Name:", self._worn_paint_edit)
        root.addWidget(tl_group)

        # -- Export Settings ---------------------------------------------------
        ex_group = QtWidgets.QGroupBox("Export Settings")
        ex_form  = QtWidgets.QFormLayout(ex_group)

        folder_row = QtWidgets.QHBoxLayout()
        self._folder_edit = QtWidgets.QLineEdit()
        self._folder_edit.setPlaceholderText("Choose export folder...")
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setFixedWidth(62)
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self._folder_edit, 1)
        folder_row.addWidget(browse_btn)
        ex_form.addRow("Export Folder:", folder_row)

        self._template_edit = QtWidgets.QLineEdit(DEFAULT_EXPORT_TEMPLATE)
        self._template_edit.setToolTip("Name of your custom export preset (e.g. 'Base Color').")
        ex_form.addRow("Output Preset:", self._template_edit)

        self._fmt_combo = QtWidgets.QComboBox()
        self._fmt_combo.addItems(["png", "tga", "tif", "jpeg", "exr"])
        ex_form.addRow("File Format:", self._fmt_combo)

        self._restore_check = QtWidgets.QCheckBox("Restore layer state after export")
        self._restore_check.setChecked(True)
        ex_form.addRow("", self._restore_check)
        root.addWidget(ex_group)
        root.addWidget(self._hline())

        # -- Buttons -----------------------------------------------------------
        btn_row = QtWidgets.QHBoxLayout()
        self._preview_btn = QtWidgets.QPushButton("Preview")
        self._preview_btn.clicked.connect(self._preview)
        self._export_btn  = QtWidgets.QPushButton("Export All")
        self._export_btn.setStyleSheet(
            "QPushButton { background:#1e6b1e; color:white;"
            " font-weight:bold; padding:5px; }"
            "QPushButton:disabled { background:#444; color:#888; }"
        )
        self._export_btn.clicked.connect(self._run_export)
        btn_row.addWidget(self._preview_btn, 1)
        btn_row.addWidget(self._export_btn, 2)
        root.addLayout(btn_row)

        # -- Log ---------------------------------------------------------------
        log_group = QtWidgets.QGroupBox("Export Log")
        log_lay   = QtWidgets.QVBoxLayout(log_group)
        self._log_view = QtWidgets.QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumHeight(120)
        self._log_view.setFont(QtGui.QFont("Courier New", 8))
        log_lay.addWidget(self._log_view)
        clear_btn = QtWidgets.QPushButton("Clear Log")
        clear_btn.setFixedWidth(72)
        clear_btn.clicked.connect(self._log_view.clear)
        log_lay.addWidget(clear_btn, alignment=Qt.AlignRight)
        root.addWidget(log_group)

        self._refresh_ts()

    # -- UI helpers -----------------------------------------------------------

    def _picker_row(self, form, label, default):
        row  = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit(default)
        btn  = QtWidgets.QPushButton("Browse...")
        btn.setFixedWidth(62)
        btn.clicked.connect(lambda _, e=edit: self._pick_layer(e))
        row.addWidget(edit, 1)
        row.addWidget(btn)
        form.addRow(label, row)
        return edit

    def _text_row(self, form, label, default):
        edit = QtWidgets.QLineEdit(default)
        form.addRow(label, edit)
        return edit

    def _hline(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        return line

    # -- UI actions -----------------------------------------------------------

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

    def _pick_layer(self, target_edit: QtWidgets.QLineEdit):
        ts = self._ts_name()
        if not ts:
            QtWidgets.QMessageBox.warning(self, "No Texture Set",
                "Select a texture set first.")
            return
        dlg = LayerPickerDialog(ts, parent=self, title="Select Layer / Folder")
        if dlg.exec_() == QtWidgets.QDialog.Accepted and dlg.selected_name():
            target_edit.setText(dlg.selected_name())

    def _scan_invert(self):
        ts = self._ts_name()
        if not ts:
            return
        text_name = self._text_layer_edit.text().strip()
        if not text_name:
            self._ulog("Fill in the Text Layer name before scanning.")
            return
        result = _scan_for_invert(ts, text_name)
        if result["found"]:
            self._invert_idx.setValue(result["index"])
            self._ulog(f"Invert filter found at index {result['index']} on '{text_name}'.")
        else:
            self._ulog(
                f"No 'Invert' effect found on '{text_name}'.\n"
                f"Effects present: {result['names'] or '(none)'}\n"
                f"Set the index manually if the effect has a different name."
            )

    # -------------------------------------------------------------------------

    def _ts_name(self) -> Optional[str]:
        name = self._ts_combo.currentText()
        return None if (not name or name == "(no project open)") else name

    def _ulog(self, msg: str, level: str = "info"):
        self._log_view.appendPlainText(msg)
        _log(msg, level)
        QtWidgets.QApplication.processEvents()

    # -------------------------------------------------------------------------
    #  EXPORT PLAN BUILDER
    # -------------------------------------------------------------------------

    def _build_plan(self):
        ts = self._ts_name()
        if not ts:
            return None, "No texture set selected."

        tree = _get_layer_tree(ts)
        if not tree:
            return None, "Could not retrieve layer tree. Is a project open?"

        sp_name     = self._skin_pack_edit.text().strip()
        bp_name     = self._black_parts_edit.text().strip()
        norm_name   = self._normal_edit.text().strip()
        wp_name     = self._worn_plas_edit.text().strip()
        wm_name     = self._worn_met_edit.text().strip()
        bright_name = self._bright_edit.text().strip()

        skin_pack = _find_node(tree, sp_name)
        if skin_pack is None:
            return None, f"Skin Pack folder '{sp_name}' not found."

        sp_kids = skin_pack.get("children", [])
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

        # Worn (pair by clean name)
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
        black_parts = _find_node(tree, bp_name)
        if black_parts:
            black_name = None
            bworn_name = None
            for child in black_parts.get("children", []):
                cn = clean_skin_name(child["name"])
                if cn == "Default"      and black_name is None: black_name = child["name"]
                if cn == "Default Worn" and bworn_name is None: bworn_name = child["name"]
            if black_name:
                tasks.append({"filename": "Default",      "type": "default",
                               "activeSkin": None, "wornPlasticSkin": None, "wornMetalSkin": None})
            if bworn_name:
                tasks.append({"filename": "Default Worn", "type": "default_worn",
                               "activeSkin": None, "wornPlasticSkin": None, "wornMetalSkin": None})

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
    #  MAIN EXPORT RUNNER
    # -------------------------------------------------------------------------

    def _run_export(self):
        if not sp_project.is_open():
            QtWidgets.QMessageBox.warning(self, "No Project", "No project is currently open.")
            return
        export_dir = self._folder_edit.text().strip()
        if not export_dir:
            QtWidgets.QMessageBox.warning(self, "No Folder", "Please select an export folder.")
            return

        # Pre-export warnings
        warns = []
        if self._invert_idx.value() < 0:
            warns.append(
                "Invert Filter Index is not set -- the Invert filter will NOT be toggled "
                "(Bright vs Normal/Worn distinction will be missing). Use Scan to fix this."
            )
        if not self._text_layer_edit.text().strip():
            warns.append("Text Fill Layer name is empty -- text effects will be skipped.")
        if not self._black_parts_edit.text().strip():
            warns.append("Black Parts Folder name is empty -- Default/Default Worn will be skipped.")
        if warns:
            msg   = "\n\n".join(f"  * {w}" for w in warns)
            reply = QtWidgets.QMessageBox.warning(
                self, "Export Warnings", f"Proceed anyway?\n\n{msg}",
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
                "No exportable skins found.")
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

        # Resolve Black Parts children names for the JS cfg
        tree        = _get_layer_tree(ts_name)
        black_parts = _find_node(tree, bp_name)
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

        progress = QtWidgets.QProgressDialog(
            "Exporting...", "Cancel", 0, len(tasks), self
        )
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
                exported = self._do_export(ts_name, template, file_fmt,
                                           export_dir, task["filename"])
                if exported:
                    ok_count += 1
                    self._ulog("  OK")
                else:
                    fail_count += 1
                    self._ulog("  Export returned no result -- check SP's log panel.")

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

        # Auto-rename if SP appended the preset name (e.g. "Tan_Base Color.png")
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

_dock_widget: Optional[QtWidgets.QDockWidget] = None


def start_plugin():
    global _dock_widget
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(SkinExporterWidget())
    _dock_widget = QtWidgets.QDockWidget(PLUGIN_NAME)
    _dock_widget.setObjectName("SkinPackExporterDock")
    _dock_widget.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
    _dock_widget.setWidget(scroll)
    sp_ui.add_dock_widget(_dock_widget)
    _log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} loaded.")


def close_plugin():
    global _dock_widget
    if _dock_widget is not None:
        sp_ui.delete_ui_element(_dock_widget)
        _dock_widget = None
    _log(f"{PLUGIN_NAME} unloaded.")


if __name__ == "__main__":
    print("Run this as a Substance Painter plugin, not standalone.")