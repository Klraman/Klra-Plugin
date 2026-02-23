"""
Skin Pack Exporter -- Substance Painter Plugin v1.2.0
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
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui   # type: ignore
    from PySide6.QtCore import Qt

# -- Substance Painter API ----------------------------------------------------
import substance_painter.ui         as sp_ui
import substance_painter.project    as sp_project
import substance_painter.textureset as sp_textureset
import substance_painter.export     as sp_export
import substance_painter.logging    as sp_log
import substance_painter.js         as sp_js


# =============================================================================
#  CONSTANTS
# =============================================================================

PLUGIN_NAME    = "Skin Pack Exporter"
PLUGIN_VERSION = "1.2.0"

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

def _js(code: str) -> Any:
    """Run code in SP's JS engine. Returns None and logs on error."""
    try:
        return sp_js.evaluate(code)
    except Exception as exc:
        _log(f"JS error: {exc}\n--- code ---\n{code[:400]}", "error")
        return None


# ---------------------------------------------------------------------------
#  DIAGNOSTIC: run a minimal JS call to verify the bridge is working and
#  return what API surface is available. Helps debug "layer tree" failures.
# ---------------------------------------------------------------------------
_JS_DIAG = """
(function(tsName) {
    var result = {
        algExists:          typeof alg !== "undefined",
        texsetsExists:      typeof alg !== "undefined" && typeof alg.texturesets !== "undefined",
        layersExists:       typeof alg !== "undefined" && typeof alg.layers !== "undefined",
        stackFnExists:      false,
        stackResult:        null,
        nodesResult:        null,
        selectedFnExists:   false,
        error:              null
    };
    try {
        result.stackFnExists  = typeof alg.texturesets.stack === "function";
        result.selectedFnExists = typeof alg.layers.selectedLayers === "function";
        if (result.stackFnExists) {
            var s = alg.texturesets.stack(tsName);
            result.stackResult = s ? "ok" : "null";
            if (s) {
                var nodes = alg.layers.nodes(s);
                result.nodesResult = nodes ? ("count:" + nodes.length) : "null";
            }
        }
    } catch(e) { result.error = e.toString(); }
    return JSON.stringify(result);
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  GET FULL LAYER TREE
#  Returns list of {name, type, visible, children, maskNodes, effects}
# ---------------------------------------------------------------------------
_JS_GET_TREE = """
(function(tsName) {
    if (typeof alg === "undefined" || typeof alg.texturesets === "undefined")
        return JSON.stringify({error: "alg not available"});

    var stack = alg.texturesets.stack(tsName);
    if (!stack) return JSON.stringify({error: "stack_null"});

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
                    name:      mask[i].name || "",
                    type:      "MaskNode",
                    visible:   alg.layers.visibility(mask[i]),
                    children:  [],
                    maskNodes: [],
                    effects:   []
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

    return JSON.stringify({ok: true, tree: walk(stack)});
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  GET SELECTED LAYER NAME
#  Returns the name of the first currently-selected layer in SP's layer panel.
# ---------------------------------------------------------------------------
_JS_GET_SELECTED = """
(function(tsName) {
    if (typeof alg === "undefined") return JSON.stringify({error: "alg_missing"});
    try {
        var stack    = alg.texturesets.stack(tsName);
        if (!stack)  return JSON.stringify({error: "stack_null"});
        var selected = alg.layers.selectedLayers(stack);
        if (!selected || selected.length === 0)
            return JSON.stringify({error: "nothing_selected"});
        return JSON.stringify({ok: true, name: selected[0].name || ""});
    } catch(e) {
        return JSON.stringify({error: e.toString()});
    }
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  SNAPSHOT visibility for the whole stack ({path: bool})
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
                    var mnm = mask[j].name || ("m" + j);
                    snap[pth + "|__mask__|" + mnm] = alg.layers.visibility(mask[j]);
                }
            } catch(e) {}
        }
    }

    walk(stack, "");
    return JSON.stringify(snap);
})(TSNAME)
"""

# ---------------------------------------------------------------------------
#  RESTORE visibility from snapshot
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
            if ((nodes[i].name || ("layer_" + i)) === parts[0])
                return nodeByPath(nodes[i], parts.slice(1));
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
#  SCAN for Invert filter on a named layer
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
#  APPLY EXPORT STATE for one task
# ---------------------------------------------------------------------------
_JS_APPLY_STATE = """
(function(tsName, cfg) {
    var stack = alg.texturesets.stack(tsName);
    if (!stack) return "error:no_stack";

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

    // Show one child inside a group, hide all others.
    // If childName is empty/null, hide the group entirely.
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

    function hideGroup(groupPath) {
        var g = byPath(stack, groupPath);
        if (g) alg.layers.setVisibility(g, false);
    }

    // 1. Text fill layer --------------------------------------------------
    if (cfg.textLayerPath) {
        var textNode = byPath(stack, cfg.textLayerPath);
        if (textNode) {
            if (cfg.invertIdx >= 0) {
                try {
                    var efx = alg.layers.effects(textNode) || [];
                    if (efx[cfg.invertIdx]) {
                        alg.layers.setEffectEnabled(efx[cfg.invertIdx], cfg.invertOn);
                    }
                } catch(e) {}
            }
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

    // 2. Black Parts folder -----------------------------------------------
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

    // 3. Skin Pack folder -------------------------------------------------
    if (cfg.skinPackPath) {
        var sp = byPath(stack, cfg.skinPackPath);
        if (sp) {
            // default/default_worn: hide the entire skin pack
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
                    // Normal hidden entirely; both worn folders active simultaneously
                    hideGroup(normPath);
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
#  PYTHON HELPERS
# =============================================================================

def _build_js(template: str, subs: Dict[str, str]) -> str:
    code = template
    for k, v in subs.items():
        code = code.replace(k, v)
    return code


def _jparse(raw: Any) -> Optional[Dict]:
    """Parse a JS result that may be a str or already a dict."""
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def _run_diag(ts_name: str) -> Dict:
    raw = _js(_build_js(_JS_DIAG, {"TSNAME": json.dumps(ts_name)}))
    return _jparse(raw) or {"error": "evaluate returned None"}


def _get_layer_tree(ts_name: str) -> Optional[List[Dict]]:
    raw  = _js(_build_js(_JS_GET_TREE, {"TSNAME": json.dumps(ts_name)}))
    data = _jparse(raw)
    if data is None:
        return None
    if "error" in data:
        _log(f"get_layer_tree JS error: {data['error']}", "error")
        return None
    return data.get("tree", [])


def _get_selected_layer(ts_name: str) -> Optional[str]:
    raw  = _js(_build_js(_JS_GET_SELECTED, {"TSNAME": json.dumps(ts_name)}))
    data = _jparse(raw)
    if data is None or "error" in data:
        return None
    return data.get("name")


def _snapshot_visibility(ts_name: str) -> str:
    raw = _js(_build_js(_JS_SNAPSHOT, {"TSNAME": json.dumps(ts_name)}))
    if raw is None:
        return "{}"
    return raw if isinstance(raw, str) else json.dumps(raw)


def _restore_visibility(ts_name: str, snap: str):
    _js(_build_js(_JS_RESTORE, {"TSNAME": json.dumps(ts_name), "SNAP": snap}))


def _scan_for_invert(ts_name: str, layer_path: str) -> Dict:
    raw  = _js(_build_js(_JS_SCAN_INVERT, {
        "TSNAME":    json.dumps(ts_name),
        "LAYERPATH": json.dumps(layer_path),
    }))
    return _jparse(raw) or {"found": False, "index": -1, "names": []}


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
        diag_btn = QtWidgets.QPushButton("Diagnose JS Bridge")
        diag_btn.setToolTip(
            "Runs a quick JS check and prints what alg.* APIs are visible.\n"
            "Use this if the layer tree fails to load."
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
                "  2. You have clicked a layer in SP's Layers panel\n"
                "  3. The JS bridge is working (press Diagnose to check)"
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
        if result.get("found"):
            self._invert_idx.setValue(result["index"])
            self._ulog(f"Invert filter found at index {result['index']} on '{text_name}'.")
        else:
            names = result.get("names", [])
            self._ulog(
                f"No 'Invert' effect found on '{text_name}'.\n"
                f"Effects found: {names if names else '(none)'}\n"
                f"Set the index manually if it has a different name."
            )

    def _run_diag(self):
        """Run the JS diagnostic and dump results to the log."""
        ts = self._ts_name()
        self._ulog("--- JS Bridge Diagnostic ---")
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

    # Register a menu action so the user can re-open the window at any time
    _action = QtWidgets.QAction(PLUGIN_NAME, sp_ui.get_main_window())
    _action.triggered.connect(_show_window)
    sp_ui.add_action("Python/Plugins", _action)

    # Open the window immediately on plugin load
    _show_window()
    _log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} loaded.")


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