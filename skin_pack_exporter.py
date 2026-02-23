"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              SKIN PACK EXPORTER — Substance Painter Plugin                 ║
║              Version 1.0.0  |  Compatible with SP 11.1.2+                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  INSTALLATION:                                                               ║
║    Place this file in:                                                       ║
║    %USERPROFILE%/Documents/Adobe/Adobe Substance 3D Painter/                ║
║                 python/plugins/skin_pack_exporter.py                        ║
║                                                                              ║
║    Then in Substance Painter:                                                ║
║    Python > Plugins > Skin Pack Exporter                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

WORKFLOW OVERVIEW
─────────────────
This plugin automates batch-exporting skin textures by toggling layer
visibility and filter states in your layer stack, then calling SP's export
API for each combination.

EXPORT TYPES
────────────
  normal       → Single skin from Normal folder
                 Text: Invert ON, Worn OFF | Black Worn: OFF
  worn         → Paired Plastic + Metal worn layers exported simultaneously
                 Text: Invert ON, Worn ON  | Black Worn: ON
  bright       → Single skin from Bright folder
                 Text: Invert OFF, Worn OFF | Black Worn: OFF
  default      → Black Parts only (black layer)
                 Text: Invert ON, Worn OFF  | Black Worn: OFF
  default_worn → Black Parts only (black worn layer)
                 Text: Invert ON, Worn ON   | Black Worn: ON

NAME MAPPING
────────────
  '!Skin Tan (Metal)'         → 'Tan'
  '!Skin Tan Worn (Plastic)'  → 'Tan Worn'
  '!Skin Black (Any Mat)'     → 'Default'
  '!Skin Black Worn (Metal)'  → 'Default Worn'
  '!Skin Gold (Any Mat)'      → 'Gold'
"""

import os
import re
import glob
import traceback
from copy import deepcopy
from typing import Optional, List, Dict, Tuple, Any

# ── Qt import (SP 11 ships PySide2; fall back to PySide6 just in case) ────────
try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from PySide2.QtCore import Qt
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui  # type: ignore
    from PySide6.QtCore import Qt

# ── Substance Painter API ─────────────────────────────────────────────────────
import substance_painter
import substance_painter.ui       as sp_ui
import substance_painter.project  as sp_project
import substance_painter.textureset as sp_textureset
import substance_painter.export   as sp_export
import substance_painter.layers   as sp_layers
import substance_painter.logging  as sp_log

# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

PLUGIN_NAME    = "Skin Pack Exporter"
PLUGIN_VERSION = "1.0.0"

# Default layer/folder names (user can override in the UI)
DEFAULT_SKIN_PACK_NAME  = "!!Skin Pack (All)"
DEFAULT_TEXT_LAYER      = "Text"
DEFAULT_BLACK_PARTS     = "Black Parts"
DEFAULT_NORMAL_FOLDER   = "Normal"
DEFAULT_WORN_PLASTIC    = "Worn (Plastic)"
DEFAULT_WORN_METAL      = "Worn (Metal)"
DEFAULT_BRIGHT_FOLDER   = "Bright"
DEFAULT_WORN_PAINT      = "Worn"          # Name of the paint layer inside Text's mask
DEFAULT_EXPORT_TEMPLATE = "Base Color"

# Regex: strip trailing material-type suffix from a skin name
# Handles: (Any Mat)  (Metal)  (Plastic)  and any truncated variant like (Pla...
_RE_SUFFIX = re.compile(r'\s*\([^)]*\)\s*$')


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def _log(msg: str, level: str = "info"):
    """Write to SP's internal log."""
    lvl = {"info": sp_log.INFO, "warn": sp_log.WARNING, "error": sp_log.ERROR}
    sp_log.log(lvl.get(level, sp_log.INFO), PLUGIN_NAME, str(msg))


# ═══════════════════════════════════════════════════════════════════════════════
#  NAME UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def clean_skin_name(raw: str) -> str:
    """
    Convert a raw SP layer name to a clean export filename (no extension).

    Examples:
        '!Skin Tan (Metal)'          → 'Tan'
        '!Skin Tan Worn (Plastic)'   → 'Tan Worn'
        '!Skin Black (Any Mat)'      → 'Default'
        '!Skin Black Worn (Metal)'   → 'Default Worn'
        '!Skin Gold (Any Mat)'       → 'Gold'
        '!Skin Zenitco Desert (Any)' → 'Zenitco Desert'
    """
    name = raw.strip()

    # 1. Drop the "!Skin " prefix
    if name.startswith("!Skin "):
        name = name[6:]

    # 2. Strip trailing (Any Mat) / (Metal) / (Plastic) / any parenthetical
    name = _RE_SUFFIX.sub("", name).strip()

    # 3. Special rename: Black → Default  |  Black Worn → Default Worn
    lower = name.lower()
    if lower == "black":
        return "Default"
    if lower == "black worn":
        return "Default Worn"

    return name


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER API WRAPPERS
#  These thin wrappers centralise all SP Python API calls so that if Autodesk
#  changes a method name between SP releases, there is only ONE place to fix.
# ═══════════════════════════════════════════════════════════════════════════════

def _root_nodes(stack) -> List:
    """Top-level layer nodes for a stack."""
    try:
        return sp_layers.get_root_layer_nodes(stack)
    except Exception as exc:
        _log(f"get_root_layer_nodes failed: {exc}", "error")
        return []


def _children(node) -> List:
    """Direct children of a group node; empty list for non-groups."""
    try:
        if node.get_type() == sp_layers.NodeType.GroupLayer:
            return node.get_nodes()
    except Exception:
        pass
    return []


def _find_child(nodes: List, name: str):
    """Find the first direct-child node matching `name` (case-sensitive)."""
    for n in nodes:
        if n.get_name() == name:
            return n
    return None


def _find_recursive(nodes: List, name: str):
    """Find the first node matching `name` anywhere in the subtree."""
    for n in nodes:
        if n.get_name() == name:
            return n
        found = _find_recursive(_children(n), name)
        if found:
            return found
    return None


def _set_visible(node, visible: bool):
    """Toggle a layer's eye-icon (visibility)."""
    try:
        node.set_visible(visible)
    except Exception as exc:
        _log(f"Cannot set visibility on '{node.get_name()}': {exc}", "warn")


def _is_visible(node) -> bool:
    try:
        return node.is_visible()
    except Exception:
        return True


# ── Effect / filter helpers ───────────────────────────────────────────────────

def _get_effects(node) -> List:
    """
    Return the filter/effect nodes attached to a layer (fill-layer effects).
    SP 11 exposes these via node.get_effects().  We also try common fallbacks.
    """
    for attr in ("get_effects", "effects"):
        try:
            val = getattr(node, attr)
            return val() if callable(val) else list(val)
        except Exception:
            pass
    return []


def _set_effect_enabled(node, index: int, enabled: bool) -> bool:
    """Enable or disable the nth effect on a layer (0-indexed). Returns True on success."""
    effects = _get_effects(node)
    if not (0 <= index < len(effects)):
        _log(
            f"Effect index {index} out of range "
            f"(layer '{node.get_name()}' has {len(effects)} effects)", "warn"
        )
        return False
    try:
        effects[index].set_enabled(enabled)
        return True
    except Exception as exc:
        _log(f"Cannot toggle effect[{index}] on '{node.get_name()}': {exc}", "warn")
        return False


def _find_invert_effect_index(node) -> int:
    """
    Scan a fill layer's effects list and return the index of the first
    effect whose name contains 'invert' (case-insensitive).
    Returns -1 if not found.
    """
    for i, effect in enumerate(_get_effects(node)):
        try:
            if "invert" in effect.get_name().lower():
                return i
        except Exception:
            pass
    return -1


# ── Mask helpers ──────────────────────────────────────────────────────────────

def _mask_nodes(node) -> List:
    """
    Return the paint / fill layers that live inside a layer's black mask.
    SP 11 exposes the mask via node.get_mask(); the mask itself is a NodeStack
    whose children are retrieved with .get_nodes().
    """
    for attr in ("get_mask",):
        try:
            mask = getattr(node, attr)()
            if mask:
                return mask.get_nodes()
        except Exception:
            pass
    # Fallback: try direct attribute
    try:
        return list(node.mask_nodes)
    except Exception:
        pass
    return []


# ═══════════════════════════════════════════════════════════════════════════════
#  VISIBILITY SNAPSHOT  (save / restore full layer state)
# ═══════════════════════════════════════════════════════════════════════════════

def _snapshot_visibility(nodes: List, out: Dict = None) -> Dict:
    """
    Recursively snapshot the visible state of every node in the subtree.
    Returns a dict keyed by the node's Python id().
    """
    if out is None:
        out = {}
    for n in nodes:
        out[id(n)] = (n, _is_visible(n))
        _snapshot_visibility(_children(n), out)
        _snapshot_visibility(_mask_nodes(n), out)
    return out


def _restore_visibility(snapshot: Dict):
    """Restore every node to its snapshotted visibility."""
    for node, was_visible in snapshot.values():
        try:
            node.set_visible(was_visible)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
#  LAYER PICKER DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class LayerPickerDialog(QtWidgets.QDialog):
    """
    Shows the layer tree for a given stack and lets the user click-select one
    node. Groups are coloured differently; double-click or "Select" to confirm.
    """

    def __init__(self, stack, parent=None, title: str = "Select a Layer"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(340, 520)
        self._stack = stack
        self._selected_name: Optional[str] = None

        layout = QtWidgets.QVBoxLayout(self)

        # Search bar
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Filter layers…")
        self._search.textChanged.connect(self._filter_tree)
        layout.addWidget(self._search)

        # Tree
        self._tree = QtWidgets.QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemDoubleClicked.connect(self._accept)
        layout.addWidget(self._tree)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        ok_btn = QtWidgets.QPushButton("Select")
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
        for node in _root_nodes(self._stack):
            self._add_item(node, self._tree.invisibleRootItem())
        self._tree.expandAll()

    def _add_item(self, node, parent_item):
        name = node.get_name()
        item = QtWidgets.QTreeWidgetItem(parent_item, [name])
        item.setData(0, Qt.UserRole, name)
        is_group = node.get_type() == sp_layers.NodeType.GroupLayer
        if is_group:
            item.setForeground(0, QtGui.QColor("#e8a84e"))
            item.setFont(0, QtGui.QFont("", -1, QtGui.QFont.Bold))
        for child in _children(node):
            self._add_item(child, item)

    def _filter_tree(self, text: str):
        """Show/hide items matching the search text."""
        text = text.lower()
        def _apply(item):
            label = item.text(0).lower()
            match = not text or text in label
            item.setHidden(not match and not _any_child_visible(item))
            for i in range(item.childCount()):
                _apply(item.child(i))
        def _any_child_visible(item):
            for i in range(item.childCount()):
                c = item.child(i)
                if text in c.text(0).lower():
                    return True
                if _any_child_visible(c):
                    return True
            return False
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            _apply(root.child(i))

    def _accept(self):
        item = self._tree.currentItem()
        if item:
            self._selected_name = item.data(0, Qt.UserRole)
            self.accept()

    def selected_name(self) -> Optional[str]:
        return self._selected_name


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN EXPORTER WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class SkinExporterWidget(QtWidgets.QWidget):
    """
    The main dockable panel for the Skin Pack Exporter plugin.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(PLUGIN_NAME)
        self._build_ui()

    # ──────────────────────────────────────────────────────────────────────────
    #  UI CONSTRUCTION
    # ──────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # Title bar
        title_lbl = QtWidgets.QLabel(
            f'<b style="font-size:13px">{PLUGIN_NAME}</b>'
            f'<span style="color:#777; font-size:10px">  v{PLUGIN_VERSION}</span>'
        )
        root.addWidget(title_lbl)
        root.addWidget(self._hline())

        # ── TEXTURE SET ───────────────────────────────────────────────────────
        ts_group = QtWidgets.QGroupBox("Texture Set")
        ts_lay   = QtWidgets.QHBoxLayout(ts_group)
        self._ts_combo = QtWidgets.QComboBox()
        ts_refresh = QtWidgets.QPushButton("⟳")
        ts_refresh.setFixedWidth(28)
        ts_refresh.setToolTip("Refresh texture set list")
        ts_refresh.clicked.connect(self._refresh_ts)
        ts_lay.addWidget(self._ts_combo, 1)
        ts_lay.addWidget(ts_refresh)
        root.addWidget(ts_group)

        # ── LAYER ASSIGNMENTS ─────────────────────────────────────────────────
        la_group = QtWidgets.QGroupBox("Layer Assignments")
        la_form  = QtWidgets.QFormLayout(la_group)
        la_form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapAllRows)

        self._skin_pack_edit = self._layer_picker_row(
            la_form, "Skin Pack Folder:", DEFAULT_SKIN_PACK_NAME,
            "Browse…",
            tip="The !!Skin Pack (All) folder (or whatever name it has)"
        )
        self._text_layer_edit = self._layer_picker_row(
            la_form, "Text Fill Layer:", DEFAULT_TEXT_LAYER,
            "Browse…",
            tip="The fill layer with the Invert filter and the black mask containing 'Worn'"
        )
        self._black_parts_edit = self._layer_picker_row(
            la_form, "Black Parts Folder:", DEFAULT_BLACK_PARTS,
            "Browse…",
            tip="The folder holding the black + black worn layers"
        )
        root.addWidget(la_group)

        # ── SKIN PACK SUB-FOLDERS ─────────────────────────────────────────────
        sf_group = QtWidgets.QGroupBox("Skin Pack Sub-folder Names")
        sf_group.setToolTip(
            "Exact names of the four sub-folders inside the Skin Pack folder"
        )
        sf_form = QtWidgets.QFormLayout(sf_group)
        self._normal_edit    = self._text_row(sf_form, "Normal:",         DEFAULT_NORMAL_FOLDER)
        self._worn_plas_edit = self._text_row(sf_form, "Worn (Plastic):", DEFAULT_WORN_PLASTIC)
        self._worn_met_edit  = self._text_row(sf_form, "Worn (Metal):",   DEFAULT_WORN_METAL)
        self._bright_edit    = self._text_row(sf_form, "Bright:",         DEFAULT_BRIGHT_FOLDER)
        root.addWidget(sf_group)

        # ── TEXT LAYER SETTINGS ───────────────────────────────────────────────
        tl_group = QtWidgets.QGroupBox("Text Layer Settings")
        tl_form  = QtWidgets.QFormLayout(tl_group)

        # Invert filter index
        # Important: NOT auto-set to 0. Use Scan or type the index manually.
        # -1 means "not configured yet" and will produce a warning on export.
        invert_row = QtWidgets.QHBoxLayout()
        self._invert_idx = QtWidgets.QSpinBox()
        self._invert_idx.setRange(-1, 30)
        self._invert_idx.setValue(-1)
        self._invert_idx.setSpecialValueText("\u26a0 Not set")
        self._invert_idx.setToolTip(
            "0-based index of the Invert filter in the Text layer's effects list.\n"
            "Click 'Scan' to auto-detect it, or type the index manually.\n"
            "Leave at -1 / 'Not set' if there is no Text layer in use."
        )
        scan_btn = QtWidgets.QPushButton("Scan")
        scan_btn.setFixedWidth(48)
        scan_btn.setToolTip(
            "Scan the Text layer's effects list to auto-detect the Invert filter.\n"
            "Fill in the Text layer name field first."
        )
        scan_btn.clicked.connect(self._scan_invert)
        invert_row.addWidget(self._invert_idx, 1)
        invert_row.addWidget(scan_btn)
        tl_form.addRow("Invert Filter Index:", invert_row)

        # Worn paint layer name
        self._worn_paint_edit = QtWidgets.QLineEdit(DEFAULT_WORN_PAINT)
        self._worn_paint_edit.setToolTip(
            "Name of the paint layer inside the Text layer's black mask\n"
            "(the one you turn on to make text look worn)"
        )
        tl_form.addRow("'Worn' Paint Layer Name:", self._worn_paint_edit)

        root.addWidget(tl_group)

        # ── EXPORT SETTINGS ───────────────────────────────────────────────────
        ex_group = QtWidgets.QGroupBox("Export Settings")
        ex_form  = QtWidgets.QFormLayout(ex_group)

        # Output folder
        folder_row = QtWidgets.QHBoxLayout()
        self._folder_edit = QtWidgets.QLineEdit()
        self._folder_edit.setPlaceholderText("Choose export folder…")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.setFixedWidth(62)
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(self._folder_edit, 1)
        folder_row.addWidget(browse_btn)
        ex_form.addRow("Export Folder:", folder_row)

        # Output template
        self._template_edit = QtWidgets.QLineEdit(DEFAULT_EXPORT_TEMPLATE)
        self._template_edit.setToolTip(
            "Name of your custom export preset in SP (e.g. 'Base Color')"
        )
        ex_form.addRow("Output Preset:", self._template_edit)

        # File format
        self._fmt_combo = QtWidgets.QComboBox()
        self._fmt_combo.addItems(["png", "tga", "tif", "jpeg", "exr"])
        ex_form.addRow("File Format:", self._fmt_combo)

        # Restore state after export
        self._restore_check = QtWidgets.QCheckBox("Restore layer state after export")
        self._restore_check.setChecked(True)
        self._restore_check.setToolTip(
            "When checked, all layer visibility is restored to what it was\n"
            "before the export run began."
        )
        ex_form.addRow("", self._restore_check)

        root.addWidget(ex_group)
        root.addWidget(self._hline())

        # ── ACTION BUTTONS ────────────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()

        self._preview_btn = QtWidgets.QPushButton("👁  Preview")
        self._preview_btn.setToolTip(
            "Show the full list of files that will be exported without actually exporting"
        )
        self._preview_btn.clicked.connect(self._preview)

        self._export_btn = QtWidgets.QPushButton("▶  Export All")
        self._export_btn.setStyleSheet(
            "QPushButton { background:#1e6b1e; color:white; font-weight:bold; padding:5px; }"
            "QPushButton:disabled { background:#444; color:#888; }"
        )
        self._export_btn.clicked.connect(self._run_export)

        btn_row.addWidget(self._preview_btn, 1)
        btn_row.addWidget(self._export_btn, 2)
        root.addLayout(btn_row)

        # ── LOG ───────────────────────────────────────────────────────────────
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

        # Populate TS list on startup
        self._refresh_ts()

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _layer_picker_row(self, form, label, default, btn_label, tip=""):
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit(default)
        edit.setToolTip(tip)
        btn = QtWidgets.QPushButton(btn_label)
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

    # ── UI actions ────────────────────────────────────────────────────────────

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
        if not sp_project.is_open():
            QtWidgets.QMessageBox.warning(self, "No Project", "No project is currently open.")
            return
        stack = self._get_stack()
        if stack is None:
            return
        dlg = LayerPickerDialog(stack, parent=self, title="Select Layer or Folder")
        if dlg.exec_() == QtWidgets.QDialog.Accepted and dlg.selected_name():
            target_edit.setText(dlg.selected_name())

    def _scan_invert(self):
        """Auto-detect the Invert filter on the Text fill layer."""
        stack = self._get_stack()
        if stack is None:
            return
        roots = _root_nodes(stack)
        text_name = self._text_layer_edit.text().strip()
        text_node  = _find_recursive(roots, text_name)
        if text_node is None:
            self._ulog(f"Text layer '{text_name}' not found. Check the name.")
            return
        idx = _find_invert_effect_index(text_node)
        if idx == -1:
            effects = _get_effects(text_node)
            names   = [e.get_name() for e in effects]
            self._ulog(
                f"No 'Invert' effect found on '{text_name}'.\n"
                f"Effects found: {names if names else '(none)'}\n"
                f"Set the index manually."
            )
        else:
            self._invert_idx.setValue(idx)
            self._ulog(f"Invert filter found at index {idx} on '{text_name}'.")

    # ──────────────────────────────────────────────────────────────────────────
    #  STACK / TS HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _get_ts_name(self) -> Optional[str]:
        name = self._ts_combo.currentText()
        if not name or name == "(no project open)":
            return None
        return name

    def _get_stack(self):
        ts_name = self._get_ts_name()
        if ts_name is None:
            return None
        for ts in sp_textureset.all_texture_sets():
            if ts.name() == ts_name:
                try:
                    return ts.get_stack()
                except Exception as exc:
                    self._ulog(f"Error getting stack for '{ts_name}': {exc}")
                    return None
        return None

    # ──────────────────────────────────────────────────────────────────────────
    #  LOGGING (in-plugin)
    # ──────────────────────────────────────────────────────────────────────────

    def _ulog(self, msg: str, level: str = "info"):
        self._log_view.appendPlainText(msg)
        _log(msg, level)
        QtWidgets.QApplication.processEvents()

    # ──────────────────────────────────────────────────────────────────────────
    #  EXPORT PLAN BUILDER
    # ──────────────────────────────────────────────────────────────────────────

    def _build_plan(self):
        """
        Analyse the current layer stack and return a list of export task dicts.

        Each task dict contains:
          filename         str   — clean output name (no extension)
          type             str   — 'normal'|'worn'|'bright'|'default'|'default_worn'
          normal_layer     str|None
          worn_plas_layer  str|None
          worn_met_layer   str|None
          bright_layer     str|None

        Returns (tasks, error_string).  error_string is None on success.
        """
        stack = self._get_stack()
        if stack is None:
            return None, "Could not get texture set stack. Is a project open?"

        roots = _root_nodes(stack)

        # ── Resolve Skin Pack folder
        sp_name   = self._skin_pack_edit.text().strip()
        skin_pack = _find_recursive(roots, sp_name)
        if skin_pack is None:
            return None, f"Skin Pack folder not found: '{sp_name}'"

        sp_kids = _children(skin_pack)

        # ── Resolve sub-folders (missing folders are silently skipped)
        def _sub(name): return _find_child(sp_kids, name.strip())

        normal_folder    = _sub(self._normal_edit.text())
        worn_plas_folder = _sub(self._worn_plas_edit.text())
        worn_met_folder  = _sub(self._worn_met_edit.text())
        bright_folder    = _sub(self._bright_edit.text())

        tasks: List[Dict[str, Any]] = []

        # ── Normal skins ──────────────────────────────────────────────────────
        if normal_folder:
            for node in _children(normal_folder):
                clean = clean_skin_name(node.get_name())
                if clean in ("Default", "Default Worn"):
                    continue  # handled by Black Parts
                tasks.append({
                    "filename":        clean,
                    "type":            "normal",
                    "normal_layer":    node.get_name(),
                    "worn_plas_layer": None,
                    "worn_met_layer":  None,
                    "bright_layer":    None,
                })

        # ── Worn skins (pair Plastic + Metal by clean name) ───────────────────
        if worn_plas_folder or worn_met_folder:
            plas_map: Dict[str, str] = {}
            met_map:  Dict[str, str] = {}

            if worn_plas_folder:
                for node in _children(worn_plas_folder):
                    plas_map[clean_skin_name(node.get_name())] = node.get_name()
            if worn_met_folder:
                for node in _children(worn_met_folder):
                    met_map[clean_skin_name(node.get_name())] = node.get_name()

            # Merge by clean name; skip Default / Default Worn (Black Parts handles those)
            for clean in sorted(set(plas_map) | set(met_map)):
                if clean in ("Default", "Default Worn"):
                    continue
                tasks.append({
                    "filename":        clean,
                    "type":            "worn",
                    "normal_layer":    None,
                    "worn_plas_layer": plas_map.get(clean),   # None if no plastic version
                    "worn_met_layer":  met_map.get(clean),    # None if no metal version
                    "bright_layer":    None,
                })

        # ── Bright skins ──────────────────────────────────────────────────────
        if bright_folder:
            for node in _children(bright_folder):
                tasks.append({
                    "filename":        clean_skin_name(node.get_name()),
                    "type":            "bright",
                    "normal_layer":    None,
                    "worn_plas_layer": None,
                    "worn_met_layer":  None,
                    "bright_layer":    node.get_name(),
                })

        # ── Black Parts (Default + Default Worn) ──────────────────────────────
        bp_name     = self._black_parts_edit.text().strip()
        black_parts = _find_recursive(roots, bp_name)
        if black_parts:
            black_raw = None
            b_worn_raw = None
            for child in _children(black_parts):
                cn = clean_skin_name(child.get_name())
                if cn == "Default" and black_raw is None:
                    black_raw = child.get_name()
                elif cn == "Default Worn" and b_worn_raw is None:
                    b_worn_raw = child.get_name()

            if black_raw:
                tasks.append({
                    "filename":        "Default",
                    "type":            "default",
                    "normal_layer":    None,
                    "worn_plas_layer": None,
                    "worn_met_layer":  None,
                    "bright_layer":    None,
                })
            if b_worn_raw:
                tasks.append({
                    "filename":        "Default Worn",
                    "type":            "default_worn",
                    "normal_layer":    None,
                    "worn_plas_layer": None,
                    "worn_met_layer":  None,
                    "bright_layer":    None,
                })

        return tasks, None

    # ──────────────────────────────────────────────────────────────────────────
    #  PREVIEW
    # ──────────────────────────────────────────────────────────────────────────

    def _preview(self):
        self._log_view.clear()
        if not sp_project.is_open():
            self._ulog("No project open.")
            return
        tasks, err = self._build_plan()
        if err:
            self._ulog(f"ERROR: {err}")
            return

        self._ulog(f"Export plan — {len(tasks)} file(s):\n")
        fmt = self._fmt_combo.currentText()
        col_w = max((len(t["filename"]) for t in tasks), default=0) + 2
        for i, t in enumerate(tasks, 1):
            name_padded = t["filename"].ljust(col_w)
            notes = []
            if t["worn_plas_layer"] and t["worn_met_layer"]:
                notes.append("plastic+metal")
            elif t["worn_plas_layer"]:
                notes.append("plastic only")
            elif t["worn_met_layer"]:
                notes.append("metal only")
            note = f"  ({', '.join(notes)})" if notes else ""
            self._ulog(
                f"  {i:>3}.  [{t['type']:<12}]  {name_padded}.{fmt}{note}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    #  MAIN EXPORT RUNNER
    # ──────────────────────────────────────────────────────────────────────────

    def _run_export(self):
        if not sp_project.is_open():
            QtWidgets.QMessageBox.warning(self, "No Project", "No project is currently open.")
            return

        export_dir = self._folder_edit.text().strip()
        if not export_dir:
            QtWidgets.QMessageBox.warning(self, "No Folder", "Please select an export folder.")
            return

        tasks, err = self._build_plan()
        if err:
            QtWidgets.QMessageBox.critical(self, "Plan Error", err)
            return
        if not tasks:
            QtWidgets.QMessageBox.information(self, "Nothing to Export", "No exportable skins found.")
            return

        os.makedirs(export_dir, exist_ok=True)

        # ── Pre-export validation warnings ───────────────────────────────────
        warnings = []
        if self._invert_idx.value() < 0:
            warnings.append(
                "Invert Filter Index is not set. The Invert filter state will NOT be "
                "toggled during export (Bright vs Normal/Worn). Set it via Scan first."
            )
        if not self._text_layer_edit.text().strip():
            warnings.append("Text Fill Layer name is empty — text effects will be skipped.")
        if not self._black_parts_edit.text().strip():
            warnings.append("Black Parts Folder name is empty — Default/Default Worn will be skipped.")
        if warnings:
            msg = "\n\n".join(f"• {w}" for w in warnings)
            reply = QtWidgets.QMessageBox.warning(
                self, "Export Warnings",
                f"Proceed anyway?\n\n{msg}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel
            )
            if reply != QtWidgets.QMessageBox.Yes:
                return

        # Collect all references we need during the export loop
        stack  = self._get_stack()
        roots  = _root_nodes(stack)
        ts_name = self._get_ts_name()

        sp_name     = self._skin_pack_edit.text().strip()
        bp_name     = self._black_parts_edit.text().strip()
        text_name   = self._text_layer_edit.text().strip()

        skin_pack   = _find_recursive(roots, sp_name)
        black_parts = _find_recursive(roots, bp_name)
        text_layer  = _find_recursive(roots, text_name)

        sp_kids          = _children(skin_pack) if skin_pack else []
        normal_folder    = _find_child(sp_kids, self._normal_edit.text().strip())
        worn_plas_folder = _find_child(sp_kids, self._worn_plas_edit.text().strip())
        worn_met_folder  = _find_child(sp_kids, self._worn_met_edit.text().strip())
        bright_folder    = _find_child(sp_kids, self._bright_edit.text().strip())

        # Black Parts children
        bp_kids     = _children(black_parts) if black_parts else []
        black_node  = None
        bworn_node  = None
        for child in bp_kids:
            cn = clean_skin_name(child.get_name())
            if cn == "Default"      and black_node  is None: black_node  = child
            if cn == "Default Worn" and bworn_node  is None: bworn_node  = child

        # Worn paint layer inside Text's mask
        worn_paint_node = None
        if text_layer:
            worn_paint_node = _find_child(
                _mask_nodes(text_layer),
                self._worn_paint_edit.text().strip()
            )

        invert_idx   = self._invert_idx.value()
        file_fmt     = self._fmt_combo.currentText()
        template     = self._template_edit.text().strip()
        should_restore = self._restore_check.isChecked()

        # ── Snapshot visibility BEFORE we start ─────────────────────────────
        snapshot: Dict = {}
        if should_restore:
            snapshot = _snapshot_visibility(roots)

        # ── Export loop ──────────────────────────────────────────────────────
        self._log_view.clear()
        self._export_btn.setEnabled(False)
        self._ulog(f"Starting export of {len(tasks)} texture(s) to:\n  {export_dir}\n")

        ok_count   = 0
        fail_count = 0

        # Build a progress dialog
        progress = QtWidgets.QProgressDialog(
            "Exporting textures…", "Cancel", 0, len(tasks), self
        )
        progress.setWindowTitle(PLUGIN_NAME)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        for i, task in enumerate(tasks):
            if progress.wasCanceled():
                self._ulog("\n⚠  Export cancelled by user.")
                break

            progress.setValue(i)
            progress.setLabelText(f"Exporting {task['filename']}.{file_fmt}  ({i+1}/{len(tasks)})")

            self._ulog(f"[{i+1}/{len(tasks)}] {task['filename']}.{file_fmt}  ({task['type']})")

            try:
                self._apply_state(
                    task,
                    skin_pack, normal_folder,
                    worn_plas_folder, worn_met_folder, bright_folder,
                    black_parts, black_node, bworn_node,
                    text_layer, invert_idx, worn_paint_node
                )

                exported = self._do_export(ts_name, template, file_fmt, export_dir, task["filename"])
                if exported:
                    ok_count += 1
                    self._ulog(f"  ✓ OK")
                else:
                    fail_count += 1
                    self._ulog(f"  ✗ Export returned no result")

            except Exception:
                fail_count += 1
                err_msg = traceback.format_exc()
                self._ulog(f"  ✗ Exception:\n{err_msg}", "error")

        progress.setValue(len(tasks))

        # ── Restore visibility ────────────────────────────────────────────────
        if should_restore and snapshot:
            _restore_visibility(snapshot)
            self._ulog("\nLayer state restored to original.")

        self._ulog(f"\n{'='*40}")
        self._ulog(f"Done — {ok_count} succeeded, {fail_count} failed.")
        self._export_btn.setEnabled(True)

    # ──────────────────────────────────────────────────────────────────────────
    #  LAYER STATE APPLICATION
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_state(
        self,
        task: Dict,
        skin_pack, normal_folder, worn_plas_folder, worn_met_folder, bright_folder,
        black_parts, black_node, bworn_node,
        text_layer, invert_idx: int, worn_paint_node
    ):
        """
        Set the correct visibility / filter state for this export task.

        ┌──────────────┬──────────────┬───────────┬─────────────┬───────────────┐
        │ Type         │ Invert Filter│ Worn Paint│ Black Worn  │ Active folder │
        ├──────────────┼──────────────┼───────────┼─────────────┼───────────────┤
        │ normal       │ ON           │ OFF       │ OFF         │ Normal        │
        │ worn         │ ON           │ ON        │ ON          │ Worn(P+M)     │
        │ bright       │ OFF          │ OFF       │ OFF         │ Bright        │
        │ default      │ ON           │ OFF       │ OFF         │ (none)        │
        │ default_worn │ ON           │ ON        │ ON          │ (none)        │
        └──────────────┴──────────────┴───────────┴─────────────┴───────────────┘
        """
        ttype = task["type"]
        is_worn = ttype in ("worn", "default_worn")
        is_default = ttype in ("default", "default_worn")

        # 1. Text layer: invert filter + worn paint layer
        if text_layer:
            # Guard: -1 means the user hasn't configured the index yet
            if invert_idx >= 0:
                _set_effect_enabled(text_layer, invert_idx, ttype != "bright")
            else:
                _log(
                    "Invert Filter Index is set to 'Not set' (-1). "
                    "The Invert filter was NOT toggled for this export. "
                    "Use the Scan button or type the index manually.",
                    "warn"
                )
            if worn_paint_node:
                _set_visible(worn_paint_node, is_worn)

        # 2. Black Parts: always visible; toggle Black Worn sub-layer
        if black_parts:
            _set_visible(black_parts, True)
            if black_node:
                _set_visible(black_node, True)
            if bworn_node:
                _set_visible(bworn_node, is_worn)

        # 3. Skin Pack folder
        if skin_pack:
            # Hide the whole folder for Default/Default Worn (Black Parts carries it)
            _set_visible(skin_pack, not is_default)

            if not is_default:
                # Helper: set one sub-folder's visibility and isolate one child
                def _activate_folder(folder, active_child_name):
                    if folder is None:
                        return
                    show = (active_child_name is not None)
                    _set_visible(folder, show)
                    if show:
                        for child in _children(folder):
                            _set_visible(child, child.get_name() == active_child_name)

                if ttype == "normal":
                    _activate_folder(normal_folder,    task["normal_layer"])
                    _activate_folder(worn_plas_folder, None)
                    _activate_folder(worn_met_folder,  None)
                    _activate_folder(bright_folder,    None)

                elif ttype == "worn":
                    # Normal folder is hidden entirely for worn exports (per user preference)
                    # Both Worn (Plastic) and Worn (Metal) folders may be active simultaneously
                    _activate_folder(normal_folder,    None)
                    _activate_folder(worn_plas_folder, task["worn_plas_layer"])
                    _activate_folder(worn_met_folder,  task["worn_met_layer"])
                    _activate_folder(bright_folder,    None)

                elif ttype == "bright":
                    _activate_folder(normal_folder,    None)
                    _activate_folder(worn_plas_folder, None)
                    _activate_folder(worn_met_folder,  None)
                    _activate_folder(bright_folder,    task["bright_layer"])

    # ──────────────────────────────────────────────────────────────────────────
    #  SP EXPORT CALL
    # ──────────────────────────────────────────────────────────────────────────

    def _do_export(
        self,
        ts_name:    str,
        template:   str,
        file_fmt:   str,
        export_dir: str,
        filename:   str,
    ) -> bool:
        """
        Call substance_painter.export.export_project_textures for the current
        layer visibility state, writing the result to export_dir/filename.file_fmt.

        SP may append the preset name to the filename (e.g. Tan_Base Color.png).
        We attempt to auto-rename any such output to the clean name.
        """
        config = {
            "exportPath":          export_dir,
            "defaultExportPreset": template,
            "exportList": [
                {"rootPath": ts_name}
            ],
            "exportParameters": [
                {
                    "rootPath": ts_name,
                    "filter":   {},
                    "parameters": {
                        "fileFormat":        file_fmt,
                        "bitDepth":          "8",
                        "dithering":         True,
                        "paddingAlgorithm":  "infinite",
                        "dilationDistance":  16,
                        # fileName: SP uses this as the base name.
                        # Tokens like $TextureSet are supported but we pass
                        # a literal string so output becomes filename.ext
                        "fileName":          filename,
                    }
                }
            ]
        }

        result = sp_export.export_project_textures(config)

        # ── Post-export rename guard ──────────────────────────────────────────
        # If SP appended the preset name (e.g. "Tan_Base Color.png") rename it
        expected = os.path.join(export_dir, f"{filename}.{file_fmt}")
        if not os.path.exists(expected):
            # Look for pattern: filename_*.ext  or  filename *.ext
            for pattern in (
                os.path.join(export_dir, f"{filename}_*.{file_fmt}"),
                os.path.join(export_dir, f"{filename} *.{file_fmt}"),
            ):
                matches = glob.glob(pattern)
                if len(matches) == 1:
                    os.rename(matches[0], expected)
                    _log(f"  Renamed '{os.path.basename(matches[0])}' → '{filename}.{file_fmt}'")
                    break
                elif len(matches) > 1:
                    # Multiple maps — keep SP's naming (user's preset exports >1 map)
                    _log(f"  Multiple maps exported: {[os.path.basename(m) for m in matches]}")
                    break

        return result is not None


# ═══════════════════════════════════════════════════════════════════════════════
#  PLUGIN ENTRY POINTS
#  SP calls start_plugin() when it loads the file and close_plugin() on unload.
# ═══════════════════════════════════════════════════════════════════════════════

_dock_widget: Optional[QtWidgets.QDockWidget] = None


def start_plugin():
    global _dock_widget

    # Wrap the widget in a scroll area so it works at any panel size
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(SkinExporterWidget())

    _dock_widget = QtWidgets.QDockWidget(PLUGIN_NAME)
    _dock_widget.setObjectName("SkinPackExporterDock")
    _dock_widget.setAllowedAreas(
        Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
    )
    _dock_widget.setWidget(scroll)

    sp_ui.add_dock_widget(_dock_widget)
    _log(f"{PLUGIN_NAME} v{PLUGIN_VERSION} loaded successfully.")


def close_plugin():
    global _dock_widget
    if _dock_widget is not None:
        sp_ui.delete_ui_element(_dock_widget)
        _dock_widget = None
    _log(f"{PLUGIN_NAME} unloaded.")


# Allow running standalone for quick syntax checks (SP not available outside)
if __name__ == "__main__":
    print("Run this file as a Substance Painter plugin, not standalone.")