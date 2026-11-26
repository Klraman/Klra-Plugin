"""
Texture Exporter – v7
Substance Painter 12.1.3  |  PySide6

v7 changes:
  1. Misc Export replaces Curvature + AO:
     - Renamed target folder from Curvature+AO to Misc (inside or sibling).
     - Exports 4 files per Texture Set when toggled:
         • AO_<TextureSet>                (meshMap: ambient_occlusion)
         • Curvature_<TextureSet>         (meshMap: curvature)
         • Overlay_Text_<TextureSet>      (documentMap: baseColor RGBA)
         • Overlay_Engraving_<TextureSet> (documentMap: baseColor RGBA)
  2. Overlay selection & soloing:
     - Added Engraving Layer/Folder picker (_ctx_engraving) supporting both Fill layers and Folders.
     - Auto-detection looks for layers/folders containing 'engrav' with manual dropdown override.
     - Contextual resolution matches Text, Engraving, Worn effect, and Black Parts across multi-TS stacks while respecting '— none —'.
     - Isolated/solo visibility when exporting Text and Engraving overlays, restoring scene state safely.
  3. Filename convention updated:
     - Added texture set name directly to all misc export files (e.g. AO_Barrel, Curvature_Barrel, Overlay_Text_Barrel, Overlay_Engraving_Barrel).
  4. Text & Engraving Folder Support:
     - Both Text and Engraving pickers support Folders (GroupLayerNode) and Fill layers (FillLayerNode).
     - Effect detection (invert filter, worn paint/fill) automatically inspects folder children when a folder is selected for Text.
  5. Mask Fill Effect Support for Worn Layer:
     - Worn paint picker supports both Paint effects (PaintEffectNode) and Fill effects (FillEffectNode) on layer masks.
     - Automatically discovers mask fill effects and auto-selects them when named 'worn'.
  6. Overall Progress Window & Reliable Cancellation:
     - Always-on-top floating overall progress dialog (_BatchProgressDialog) positioned near top-center so Substance Painter's per-export modal popup does not cover it.
     - Tracks accurate step counts across all skins, path buckets, and active Misc passes.
     - Detects ExportStatus.Cancelled from Substance Painter's native popup to immediately abort the entire batch/Misc run and restore layer visibility.
  7. Transparent Overlay Background:
     - build_overlay_export_config() exports RGBA (R, G, B, A from baseColor) with 'passthrough' padding (falling back to PNG when JPEG is chosen).
     - Overlay export solos the target layer by hiding all other root layers and ancestor siblings.
  8. Audit Bug Fixes:
     - Fixed SP Node/Stack wrapper comparisons to use .uid() and == instead of Python 'is'.
     - Excluded Texture Sets missing skin-pack structure from export buckets when skipped.
     - Fixed unchecking all Texture Sets ('None') so it returns an empty selection instead of all Texture Sets.
     - Synchronized contextual layer pickers with the primary export stack.
     - Respected '— none —' contextual selections on secondary Texture Sets and added Black Parts name matching across stacks.
     - Hardened categorize_level2() so equal-count Normal/Bright groups are never misclassified as Worn pairs.
     - Matched multi-TS skins by (category, filename) rather than raw list index.
     - Fixed EXR bitDepth ('16f') in Misc mesh-map and overlay export configs, plus UDIM wildcard support.
     - Preserved saved config paths and layer selections when closing the window with no project open or across different projects.
     - Ensured parent folder visibility for nested contextual layers during batch export.
     - Prevented _show_window() from rebuilding UI widgets while an export is running.
     - Fixed Dry Run Preview prefix mismatch, solo Worn display in Detect Structure, negative error counts in standalone Misc popup, and skin filename sanitization/deduplication.

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

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QFileDialog, QGroupBox, QMessageBox, QSizePolicy,
    QProgressBar, QTextEdit, QScrollArea,
    QCheckBox, QDialog,
)
from PySide6.QtCore import Qt, QCoreApplication

_plugin_widgets = []
_window         = None
sp_ls           = substance_painter.layerstack

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "texture_exporter_config.json"
)

# All export categories
ALL_CAT_TYPES = ("normal", "worn", "bright")

# Channel definitions (rgb=True -> R/G/B channels, rgb=False -> L greyscale)
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


# Build a single exportPresets map dictionary for an RGB or greyscale channel start
def _channel_map_entry(map_name: str, src_map_name: str, rgb: bool) -> dict:
    """Single exportPresets.maps entry. RGB->R/G/B, greyscale->L."""
    if rgb:
        ch = [{"destChannel": c, "srcChannel": c,
               "srcMapType": "documentMap", "srcMapName": src_map_name}
              for c in ("R", "G", "B")]
    else:
        ch = [{"destChannel": "L", "srcChannel": "L",
               "srcMapType": "documentMap", "srcMapName": src_map_name}]
    return {"fileName": map_name, "channels": ch}
# Build a single exportPresets map dictionary for an RGB or greyscale channel end


# Check if node is a group layer start
def _is_group(n):     return isinstance(n, sp_ls.GroupLayerNode)
# Check if node is a group layer end

# Check if node is a fill layer start
def _is_fill(n):      return isinstance(n, sp_ls.FillLayerNode)
# Check if node is a fill layer end

# Check if node is a paint effect start
def _is_paint_fx(n):  return isinstance(n, sp_ls.PaintEffectNode)
# Check if node is a paint effect end

# Check if node is a fill effect start
def _is_fill_fx(n):   return isinstance(n, sp_ls.FillEffectNode)
# Check if node is a fill effect end

# Check if node is a filter effect start
def _is_filter_fx(n): return isinstance(n, sp_ls.FilterEffectNode)
# Check if node is a filter effect end


# Check if node is a main layer-stack layer start
def _is_proper_layer(n):
    return isinstance(n, (sp_ls.GroupLayerNode, sp_ls.PaintLayerNode,
                           sp_ls.FillLayerNode,  sp_ls.InstanceLayerNode))
# Check if node is a main layer-stack layer end


# Compare two Substance Painter Node wrappers safely by internal uid start
def _same_node(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.uid() == b.uid()
    except Exception:
        return a == b
# Compare two Substance Painter Node wrappers safely by internal uid end


# Compare two Substance Painter Stack wrappers safely by value equality start
def _same_stack(a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return a == b
    except Exception:
        return str(a) == str(b)
# Compare two Substance Painter Stack wrappers safely by value equality end


# Return immediate child group layers of a node start
def _group_children(node):
    return [c for c in node.sub_layers() if _is_group(c)]
# Return immediate child group layers of a node end


# Recursively list all proper layers in a stack with their depth start
def _walk_all(stack):
    out = []
    def _r(nodes, d):
        for n in nodes:
            if _is_proper_layer(n): out.append((d, n))
            if _is_group(n):        _r(n.sub_layers(), d + 1)
    _r(sp_ls.get_root_layer_nodes(stack), 0)
    return out
# Recursively list all proper layers in a stack with their depth end

# List all fill layers in a stack with their depth start
def _walk_fills(stack):  return [(d, n) for d, n in _walk_all(stack) if _is_fill(n)]
# List all fill layers in a stack with their depth end

# List all group layers in a stack with their depth start
def _walk_groups(stack): return [(d, n) for d, n in _walk_all(stack) if _is_group(n)]
# List all group layers in a stack with their depth end


# Clean raw layer name into an export-safe skin filename start
def clean_skin_name(raw: str) -> str:
    s = raw.lstrip('!').strip()
    # Strip leading parenthetical tag like "(WIP) Red" -> "Red" before removing trailing "(...)" notes
    s = re.sub(r'^\([^)]*\)\s*', '', s).strip()
    s = re.sub(r'^[Ss]kin\s+', '', s).strip()
    stripped = re.sub(r'\s*\(.*', '', s).strip()
    if stripped:
        s = stripped
    else:
        s = s.strip('()').strip()
    s = re.sub(r'\bBlack\b', 'Default', s)
    # Remove characters illegal in Windows filenames
    s = re.sub(r'[<>:"/\\|?*]+', '_', s).strip().rstrip('.')
    return s or "Unnamed"
# Clean raw layer name into an export-safe skin filename end


# Find the 3-level skin pack root group in a layer stack start
def find_skin_pack_root(stack):
    """
    Root group with >=3 Level-2 category groups each having >=1 skin group.
    The >=3 requirement prevents matching smaller utility groups (e.g. Black Parts).
    """
    for node in sp_ls.get_root_layer_nodes(stack):
        if not _is_group(node): continue
        level2 = _group_children(node)
        if len(level2) < 3: continue
        if all(len(_group_children(c)) > 0 for c in level2):
            return node
    return None
# Find the 3-level skin pack root group in a layer stack end


# Classify Level-2 category folders into Normal, Bright, and Worn groups start
def categorize_level2(skin_pack_node):
    """
    Returns (normal_cat, bright_cats, worn_plastic, worn_metal).

    Worn detection priority:
      1. Groups matching worn/plastic/metal name hints (paired by matching count
         when >=2 exist, or solo worn_p when only 1 exists).
      2. Fallback classic pair: two non-normal/non-bright groups sharing the
         exact same child count (at most one pair; remaining groups stay standalone).
      3. Remaining standalone groups: group named 'normal' (or largest non-bright
         group) becomes Normal; all others become Bright.
    """
    level2 = _group_children(skin_pack_node)
    order  = {c.uid(): i for i, c in enumerate(level2)}

    def _order_pair(a, b):
        an, bn = a.get_name().lower(), b.get_name().lower()
        if 'plastic' in an or 'metal' in bn:
            return a, b
        if 'metal' in an or 'plastic' in bn:
            return b, a
        return (a, b) if order[a.uid()] < order[b.uid()] else (b, a)

    def _has_worn_hint(node):
        n = node.get_name().lower()
        if 'worn' in n:
            return True
        if ('plastic' in n or 'metal' in n) and 'normal' not in n and 'bright' not in n:
            return True
        return False

    worn_p = worn_m = None
    used_uids = set()

    # Step 1: Check explicit worn/plastic/metal name-hinted groups first
    hinted = [c for c in level2 if _has_worn_hint(c)]
    if len(hinted) >= 2:
        # Prefer two hinted groups that share the same child count
        by_cnt = defaultdict(list)
        for c in hinted:
            by_cnt[len(_group_children(c))].append(c)
        paired = None
        for grp in by_cnt.values():
            if len(grp) >= 2:
                paired = _order_pair(grp[0], grp[1])
                break
        if paired is None:
            paired = _order_pair(hinted[0], hinted[1])
        worn_p, worn_m = paired
        used_uids.update([worn_p.uid(), worn_m.uid()])
    elif len(hinted) == 1:
        # Solo worn group (e.g. Worn Metal was deleted)
        worn_p = hinted[0]
        used_uids.add(worn_p.uid())

    # Step 2: Classic equal-count pair fallback only if no worn group was identified yet
    if worn_p is None:
        by_count = defaultdict(list)
        for c in level2:
            n = c.get_name().lower()
            if 'normal' not in n and 'bright' not in n:
                by_count[len(_group_children(c))].append(c)
        for cats in by_count.values():
            if len(cats) == 2:
                worn_p, worn_m = _order_pair(cats[0], cats[1])
                used_uids.update([worn_p.uid(), worn_m.uid()])
                break

    standalone = [c for c in level2 if c.uid() not in used_uids]
    standalone.sort(key=lambda c: order[c.uid()])

    normal = None
    if standalone:
        # Prefer explicit 'normal' name, else largest non-'bright' group, else overall largest
        explicit_norm = [c for c in standalone if 'normal' in c.get_name().lower() and 'bright' not in c.get_name().lower()]
        if explicit_norm:
            normal = max(explicit_norm, key=lambda c: len(_group_children(c)))
        else:
            non_bright = [c for c in standalone if 'bright' not in c.get_name().lower()]
            pool = non_bright if non_bright else standalone
            normal = max(pool, key=lambda c: len(_group_children(c)))

    bright = [c for c in standalone if not _same_node(c, normal)]
    return normal, bright, worn_p, worn_m
# Classify Level-2 category folders into Normal, Bright, and Worn groups end


# Build ordered export jobs and collect skin/category nodes for a stack start
def build_export_jobs(skin_pack_node):
    """Returns (jobs, all_skin_nodes, all_cat_nodes). Deduplicates colliding filenames per category."""
    normal, bright_cats, worn_p, worn_m = categorize_level2(skin_pack_node)
    jobs = []; all_skins = []; all_cats = []
    seen_per_cat = defaultdict(set)

    def _unique_name(cat_type: str, base_name: str) -> str:
        candidate = base_name
        counter = 2
        while candidate.lower() in seen_per_cat[cat_type]:
            candidate = f"{base_name}_{counter}"
            counter += 1
        seen_per_cat[cat_type].add(candidate.lower())
        return candidate

    def _add(cat, cat_type):
        all_cats.append(cat)
        skins = _group_children(cat)
        all_skins.extend(skins)
        for skin in skins:
            fname = _unique_name(cat_type, clean_skin_name(skin.get_name()))
            jobs.append({"filename": fname,
                         "show": [skin], "all_in_category": skins,
                         "cat_type": cat_type})

    if normal: _add(normal, "normal")
    if worn_p and worn_m:
        all_cats.extend([worn_p, worn_m])
        ps, ms = _group_children(worn_p), _group_children(worn_m)
        all_skins.extend(ps + ms)
        all_worn = ps + ms
        for i in range(min(len(ps), len(ms))):
            fname = _unique_name("worn", clean_skin_name(ps[i].get_name()))
            jobs.append({"filename": fname,
                         "show": [ps[i], ms[i]], "all_in_category": all_worn,
                         "cat_type": "worn"})
    elif worn_p:
        _add(worn_p, "worn")
    for cat in bright_cats: _add(cat, "bright")
    return jobs, all_skins, all_cats
# Build ordered export jobs and collect skin/category nodes for a stack end


# Snapshot visibility of nodes keyed by uid to avoid wrapper duplication start
def _save_vis(nodes):
    state = {}
    for n in nodes:
        if n is None:
            continue
        try:
            uid = n.uid()
            if uid not in state:
                state[uid] = (n, n.is_visible())
        except Exception:
            pass
    return state
# Snapshot visibility of nodes keyed by uid to avoid wrapper duplication end


# Restore previously snapshotted node visibility start
def _restore_vis(st):
    for _, (n, v) in st.items():
        try: n.set_visible(v)
        except Exception: pass
# Restore previously snapshotted node visibility end


# Collect a node together with all of its parent folders up to root start
def _collect_with_ancestors(node) -> list:
    out = []
    p = node
    while p is not None:
        out.append(p)
        try:
            p = p.get_parent()
        except Exception:
            break
    return out
# Collect a node together with all of its parent folders up to root end


# Set a node and all of its ancestor folders visible start
def _ensure_ancestors_visible(node):
    for anc in reversed(_collect_with_ancestors(node)):
        try:
            anc.set_visible(True)
        except Exception:
            pass
# Set a node and all of its ancestor folders visible end


# Collect all stack nodes whose visibility will change when soloing target start
def _collect_solo_nodes(stack, target):
    """Return all nodes whose visibility will be touched when soloing `target`."""
    nodes = []
    try:
        nodes.extend(sp_ls.get_root_layer_nodes(stack))
    except Exception:
        pass
    p = target
    while p is not None:
        nodes.append(p)
        try:
            parent = p.get_parent()
        except Exception:
            parent = None
        if parent is not None and _is_group(parent):
            try:
                nodes.extend(parent.sub_layers())
            except Exception:
                pass
        p = parent
    return nodes
# Collect all stack nodes whose visibility will change when soloing target end


# Hide all unrelated stack layers so only target and its ancestors render start
def _solo_node(stack, target):
    """Hide all other layers in `stack` so ONLY `target` (and its ancestor chain) is visible."""
    try:
        for rn in sp_ls.get_root_layer_nodes(stack):
            try: rn.set_visible(False)
            except Exception: pass
    except Exception:
        pass
    if target is None:
        return
    chain = _collect_with_ancestors(target)
    for anc in chain[1:]:
        if _is_group(anc):
            try:
                for sib in anc.sub_layers():
                    try: sib.set_visible(False)
                    except Exception: pass
            except Exception:
                pass
    for node in reversed(chain):
        try: node.set_visible(True)
        except Exception: pass
# Hide all unrelated stack layers so only target and its ancestors render end


# Build export rootPath string for a Texture Set start
def _ts_root_path(ts) -> str:
    """Non-layered -> ts.name; layered -> ts.name/stack.name."""
    if ts.is_layered_material():
        return ts.name + "/" + ts.get_stack().name
    return ts.name
# Build export rootPath string for a Texture Set end


# Check if any Texture Set in a group uses UDIM UV tiles start
def _any_uv_tiles(ts_group: list) -> bool:
    for ts in ts_group:
        try:
            if ts.has_uv_tiles():
                return True
        except Exception:
            pass
    return False
# Check if any Texture Set in a group uses UDIM UV tiles end


# Build Substance Painter export configuration for skin batch maps start
def build_export_config(
    ts_root_paths: list,
    out_dir: str,
    skin_name: str,
    sp_format: str,
    enabled_channels: list,
    multi_ts: bool,
    use_udim: bool = False,
) -> dict:
    """
    Filename scheme:
      1 TS,  1 ch  ->  {skin}
      1 TS,  N ch  ->  {skin}_{ch_suffix}
      N TSes, 1 ch ->  $textureSet_{skin}
      N TSes, N ch ->  $textureSet_{skin}_{ch_suffix}
    Appends _$udim automatically if any exported TS uses UV Tiles.
    """
    bit_depth = "16f" if sp_format == "exr" else "8"
    multi_ch  = len(enabled_channels) > 1
    udim_sfx  = "_$udim" if use_udim else ""
    maps = []
    for key in enabled_channels:
        ch    = CHANNEL_DEFS[key]
        base  = f"$textureSet_{skin_name}" if multi_ts else skin_name
        fname = (f"{base}_{ch['suffix']}" if multi_ch else base) + udim_sfx
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
# Build Substance Painter export configuration for skin batch maps end


# Build Substance Painter export configuration for baked AO and Curvature maps start
def build_meshmap_export_config(
    ts_root_paths: list,
    out_dir: str,
    sp_format: str,
    multi_ts: bool = True,
    use_udim: bool = False,
) -> dict:
    """
    Export curvature and AO baked mesh maps (srcMapType: "meshMap") named
    Curvature_$textureSet and AO_$textureSet.
    """
    bit_depth = "16f" if sp_format == "exr" else "8"
    udim_sfx  = "_$udim" if use_udim else ""
    maps = [
        {
            "fileName": f"Curvature_$textureSet{udim_sfx}",
            "channels": [{"destChannel": "L", "srcChannel": "L",
                          "srcMapType": "meshMap", "srcMapName": "curvature"}],
        },
        {
            "fileName": f"AO_$textureSet{udim_sfx}",
            "channels": [{"destChannel": "L", "srcChannel": "L",
                          "srcMapType": "meshMap", "srcMapName": "ambient_occlusion"}],
        },
    ]
    return {
        "exportShaderParams": False,
        "exportPath":         out_dir,
        "exportPresets":      [{"name": "MiscMeshMaps", "maps": maps}],
        "defaultExportPreset": "MiscMeshMaps",
        "exportList":         [{"rootPath": rp} for rp in ts_root_paths],
        "exportParameters":   [{"parameters": {
            "fileFormat":       sp_format,
            "bitDepth":         bit_depth,
            "dithering":        False,
            "paddingAlgorithm": "infinite",
            "dilationDistance": 16,
        }}],
    }
# Build Substance Painter export configuration for baked AO and Curvature maps end


# Build Substance Painter export configuration for transparent RGBA overlays start
def build_overlay_export_config(
    ts_root_paths: list,
    out_dir: str,
    sp_format: str,
    map_fileName: str,
    use_udim: bool = False,
) -> dict:
    """
    Export an isolated overlay (Text or Engraving) as an RGBA Base Color
    document map with a fully transparent background ('passthrough' padding).
    Falls back to PNG if sp_format is JPEG (which does not support alpha).
    """
    overlay_fmt = "png" if sp_format == "jpeg" else sp_format
    bit_depth   = "16f" if overlay_fmt == "exr" else "8"
    udim_sfx    = "_$udim" if use_udim else ""
    channels = [
        {"destChannel": c, "srcChannel": c,
         "srcMapType": "documentMap", "srcMapName": "baseColor"}
        for c in ("R", "G", "B", "A")
    ]
    maps = [{"fileName": f"{map_fileName}{udim_sfx}", "channels": channels}]
    return {
        "exportShaderParams": False,
        "exportPath":         out_dir,
        "exportPresets":      [{"name": "MiscOverlay", "maps": maps}],
        "defaultExportPreset": "MiscOverlay",
        "exportList":         [{"rootPath": rp} for rp in ts_root_paths],
        "exportParameters":   [{"parameters": {
            "fileFormat":       overlay_fmt,
            "bitDepth":         bit_depth,
            "dithering":        False,
            "paddingAlgorithm": "passthrough",
        }}],
    }
# Build Substance Painter export configuration for transparent RGBA overlays end


# Walk upward from a directory path to locate an ancestor 'textures' folder start
def _find_textures_root(path: str) -> str | None:
    p = os.path.normpath(path)
    while True:
        if os.path.basename(p).lower() == "textures":
            return p.replace("\\", "/")
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
# Walk upward from a directory path to locate an ancestor 'textures' folder end


# Resolve the Misc output directory inside or adjacent to base_path start
def _resolve_misc_dir(base_path: str, sibling: bool) -> str:
    base   = base_path.rstrip("/").rstrip("\\")
    parent = os.path.dirname(base) if sibling else base
    return (parent + "/Misc").replace("\\", "/")
# Resolve the Misc output directory inside or adjacent to base_path end

# Backward-compatible alias for Misc directory resolution
_resolve_meshmap_dir = _resolve_misc_dir


# Normalize strings for deep folder matching by collapsing separators start
def _norm_for_match(s: str) -> str:
    return re.sub(r'[\s_\-/\\]+', ' ', s.lower()).strip()
# Normalize strings for deep folder matching by collapsing separators end


# Recursively collect normalized subfolder paths up to max_depth start
def _collect_subfolders(root: str, max_depth: int = 5) -> list:
    results: list = []

    def _walk(path: str, rel_parts: list, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
                if entry.is_dir(follow_symlinks=False):
                    new_parts = rel_parts + [entry.name]
                    norm_rel  = _norm_for_match(" ".join(new_parts))
                    results.append((norm_rel, entry.path.replace("\\", "/")))
                    _walk(entry.path, new_parts, depth + 1)
        except OSError:
            pass

    _walk(root, [], 0)
    return results
# Recursively collect normalized subfolder paths up to max_depth end


# Match a Texture Set name to the most specific subfolder path start
def _find_best_deep_match(ts_name: str, all_folders: list) -> tuple:
    ts_norm    = _norm_for_match(ts_name)
    best_abs   = None
    best_score = -1

    for norm_rel, abs_path in all_folders:
        if ts_norm == norm_rel or ts_norm.startswith(norm_rel + " "):
            score = len(norm_rel)
            if score > best_score:
                best_score = score
                best_abs   = abs_path

    if best_abs:
        short = "/".join(best_abs.split("/")[-2:])
        return best_abs, f"✅  {ts_name}\n        → …/{short}"
    return None, f"❌  {ts_name}  (no match)"
# Match a Texture Set name to the most specific subfolder path end


# Auto-detect Text, Engraving, and Black Parts contextual nodes in a stack start
def _auto_detect_ctx(stack, skin_root):
    result = {"fill": None, "bp_group": None, "bp_nonworn": None, "bp_worn": None, "engraving": None}
    best = fallback = None
    all_nodes = _walk_all(stack)

    # Check all fills and groups for text markings (effects or name hint)
    for _, node in all_nodes:
        if _same_node(node, skin_root): continue
        try:
            has_f = any(_is_filter_fx(fx) for fx in node.content_effects())
            has_p = any((_is_paint_fx(fx) or _is_fill_fx(fx)) for fx in node.mask_effects())
            if not has_p and _is_group(node):
                for ch in node.sub_layers():
                    if any((_is_paint_fx(fx) or _is_fill_fx(fx)) for fx in ch.mask_effects()):
                        has_p = True; break
        except Exception: continue
        if has_f and has_p: best = node; break
        if has_f and not fallback: fallback = node
    if not best:
        for _, node in all_nodes:
            if _same_node(node, skin_root): continue
            if "text" in node.get_name().lower():
                best = node; break
    result["fill"] = best or fallback

    # Auto-detect engraving (fill layer or folder containing 'engrav')
    try:
        for _, node in all_nodes:
            if _same_node(node, skin_root): continue
            if _same_node(node, result["fill"]): continue
            if "engrav" in node.get_name().lower():
                result["engraving"] = node
                break
    except Exception: pass

    # Auto-detect Black Parts group and its non-worn / worn children
    try:
        for node in sp_ls.get_root_layer_nodes(stack):
            if not _is_group(node): continue
            if _same_node(node, skin_root): continue
            if _same_node(node, result["fill"]): continue
            if _same_node(node, result["engraving"]): continue
            children = [c for c in node.sub_layers() if _is_proper_layer(c)]
            if len(children) < 2:
                continue
            child_names = [c.get_name().lower() for c in children]
            if not any("worn" in n or "black" in n for n in child_names):
                continue
            result["bp_group"] = node
            worn_ch = nonworn_ch = None
            for ch in children:
                if "worn" in ch.get_name().lower():
                    if not worn_ch:    worn_ch    = ch
                else:
                    if not nonworn_ch: nonworn_ch = ch
            if not nonworn_ch and children:        nonworn_ch = children[0]
            if not worn_ch and len(children) >= 2: worn_ch    = children[1]
            result["bp_nonworn"] = nonworn_ch
            result["bp_worn"]    = worn_ch
            break
    except Exception: pass
    return result
# Auto-detect Text, Engraving, and Black Parts contextual nodes in a stack end


class _BatchProgressDialog(QDialog):
    """
    Compact always-on-top overall progress window shown during batch/Misc exports.
    Positioned near top-center so Substance Painter's centered per-export modal
    dialog does not cover it.
    """

    # Initialize the floating overall progress dialog start
    def __init__(self, title: str, total: int, on_cancel, parent=None):
        super().__init__(parent)
        self._on_cancel = on_cancel
        self._finished  = False
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(430)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        self._status = QLabel("Starting…")
        self._status.setStyleSheet("font-weight:bold;font-size:12px;")
        lay.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setMinimum(0)
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(0)
        self._bar.setFormat("%v / %m  (%p%)")
        lay.addWidget(self._bar)

        hint = QLabel("Tip: Clicking Cancel below OR on Substance Painter's export popup aborts the entire batch.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#aaa;font-size:11px;")
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("⏹ Cancel Batch")
        self._cancel_btn.setFixedHeight(28)
        self._cancel_btn.setStyleSheet(
            "QPushButton          {background:#8b2020;color:white;font-weight:bold;padding:0 14px;}"
            "QPushButton:hover    {background:#b03030;}"
            "QPushButton:disabled {background:#444;color:#888;}"
        )
        self._cancel_btn.clicked.connect(self._handle_cancel)
        btn_row.addWidget(self._cancel_btn)
        lay.addLayout(btn_row)

        try:
            scr = QApplication.primaryScreen()
            if scr:
                geo = scr.availableGeometry()
                self.adjustSize()
                self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + 60)
        except Exception:
            pass
    # Initialize the floating overall progress dialog end

    # Update progress bar value and status text start
    def update_progress(self, done: int, total: int, label: str):
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(done)
        self._status.setText(f"{label}  ({done}/{total})")
        QCoreApplication.processEvents()
    # Update progress bar value and status text end

    # Reflect cancellation state in the floating dialog start
    def set_cancelling(self):
        self._cancel_btn.setEnabled(False)
        self._status.setText("Cancelling — stopping batch…")
        QCoreApplication.processEvents()
    # Reflect cancellation state in the floating dialog end

    # Trigger cancellation callback from button or close action start
    def _handle_cancel(self):
        self.set_cancelling()
        if self._on_cancel:
            self._on_cancel()
    # Trigger cancellation callback from button or close action end

    # Close the progress dialog cleanly once work finishes start
    def mark_finished(self):
        self._finished = True
        self.close()
    # Close the progress dialog cleanly once work finishes end

    # Treat manual window close while running as batch cancellation start
    def closeEvent(self, event):
        if not self._finished:
            self._handle_cancel()
        event.accept()
    # Treat manual window close while running as batch cancellation end


class TextureExporterWindow(QWidget):

    FORMATS = {"PNG": "png", "JPG": "jpeg", "TIFF": "tiff", "EXR": "exr", "TGA": "tga"}

    # Initialize main Texture Exporter window state and UI start
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Texture Exporter")
        self.setMinimumWidth(580)
        self.setMinimumHeight(640)
        self.setWindowFlags(Qt.Window)
        self._last_dir         = ""
        self._batch_running    = False
        self._cancel_requested = False
        self._prog_dlg         = None
        self._ctx_stack        = None
        self._ts_widgets       = {}
        self._channel_checks   = {}
        self._cat_checks       = {}
        self._saved_ctx        = {}
        self._mm_enabled       = None
        self._mm_location      = None
        self._build_ui()
        self._load_config()
    # Initialize main Texture Exporter window state and UI end

    # Create top-level window layout start
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_batch_tab())
    # Create top-level window layout end

    # Build scrollable batch export controls, pickers, progress bar, and log start
    def _build_batch_tab(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(8); lay.setContentsMargins(8, 8, 8, 8)

        # Top cancel button mirrors the bottom cancel button
        top_cancel_row = QHBoxLayout()
        self._b_cancel_btn_top = QPushButton("⏹ Cancel")
        self._b_cancel_btn_top.setFixedHeight(28)
        self._b_cancel_btn_top.setEnabled(False)
        self._b_cancel_btn_top.setStyleSheet(
            "QPushButton          {background:#8b2020;color:white;}"
            "QPushButton:hover    {background:#b03030;}"
            "QPushButton:disabled {background:#444;color:#888;}"
        )
        self._b_cancel_btn_top.clicked.connect(self._request_cancel)
        top_cancel_row.addStretch()
        top_cancel_row.addWidget(self._b_cancel_btn_top)
        lay.addLayout(top_cancel_row)

        # Global format, worn suffix, and default output path
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

        gpr = QHBoxLayout(); gpr.addWidget(QLabel("Default Output Folder:"))
        self._b_path = QLineEdit(); self._b_path.setPlaceholderText("Used for any Texture Set with no path set")
        self._b_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed); gpr.addWidget(self._b_path)
        gbb = QPushButton("Browse…"); gbb.setFixedWidth(68)
        gbb.clicked.connect(self._browse_default_and_find)
        gpr.addWidget(gbb)
        fg.addLayout(gpr)
        lay.addWidget(fmt_grp)

        # Texture Sets and per-TS output paths
        ts_grp = QGroupBox("Texture Sets  &  Output Paths"); tg = QVBoxLayout(ts_grp)
        note = QLabel(
            "Each Texture Set can export to its own folder. "
            "Leave blank to use the Default Output Folder above.\n"
            "TSes sharing the same path are batched in one export call."
        )
        note.setWordWrap(True); note.setStyleSheet("color:#aaa;font-size:11px;"); tg.addWidget(note)

        ts_btn_row = QHBoxLayout()
        sel_all_btn  = QPushButton("☑ All");  sel_all_btn.setFixedWidth(54)
        sel_none_btn = QPushButton("☐ None"); sel_none_btn.setFixedWidth(54)
        sel_all_btn.setToolTip("Check all Texture Sets")
        sel_none_btn.setToolTip("Uncheck all Texture Sets")
        sel_all_btn.clicked.connect(lambda: self._set_all_ts_checked(True))
        sel_none_btn.clicked.connect(lambda: self._set_all_ts_checked(False))
        ts_btn_row.addWidget(sel_all_btn)
        ts_btn_row.addWidget(sel_none_btn)
        ts_btn_row.addSpacing(6)

        clr_btn = QPushButton("🗑 Clear Paths")
        clr_btn.setToolTip("Clear all per-TS output path fields\n(they will fall back to the Default Output Folder)")
        clr_btn.clicked.connect(self._clear_all_ts_paths)
        ts_btn_row.addWidget(clr_btn)
        ts_btn_row.addSpacing(6)

        find_btn = QPushButton("🔍  Find Folders")
        find_btn.setToolTip(
            "Recursively searches subfolders under the detected 'textures' root\n"
            "and matches each Texture Set name to the most specific subfolder.\n\n"
            "A progress window shows every decision in real time."
        )
        find_btn.clicked.connect(self._find_ts_folders)
        ts_btn_row.addWidget(find_btn)
        ts_btn_row.addStretch()

        btn_ref_ts = QPushButton("↺  Refresh")
        btn_ref_ts.setFixedWidth(80)
        btn_ref_ts.clicked.connect(self._refresh_ts_list)
        ts_btn_row.addWidget(btn_ref_ts)
        tg.addLayout(ts_btn_row)

        self._ts_container = QWidget()
        self._ts_layout    = QVBoxLayout(self._ts_container)
        self._ts_layout.setContentsMargins(0, 0, 0, 0); self._ts_layout.setSpacing(3)
        tg.addWidget(self._ts_container)
        lay.addWidget(ts_grp)

        # Export channels
        ch_grp = QGroupBox("Export Channels"); cg = QGridLayout(ch_grp); cg.setSpacing(4)
        cn = QLabel("1 channel selected → no suffix.  Multiple → filename_{suffix} per channel.")
        cn.setStyleSheet("color:#aaa;font-size:11px;"); cn.setWordWrap(True); cg.addWidget(cn, 0, 0, 1, 3)
        for i, (key, ch) in enumerate(CHANNEL_DEFS.items()):
            cb = QCheckBox(f"{ch['label']}  [{ch['suffix']}]")
            cb.setChecked(key == "baseColor")
            cb.stateChanged.connect(self._update_job_count)
            self._channel_checks[key] = cb
            cg.addWidget(cb, 1 + i // 3, i % 3)
        lay.addWidget(ch_grp)

        # Misc export (AO, Curvature, Text & Engraving Overlays)
        mm_grp = QGroupBox("Misc Export"); mmg = QVBoxLayout(mm_grp); mmg.setSpacing(6)
        mm_note = QLabel(
            "Exports AO, Curvature, Text Overlay, and Engraving Overlay (4 files) once per Texture Set "
            "(not per-skin). Overlays use transparent backgrounds and the layers configured below."
        )
        mm_note.setWordWrap(True); mm_note.setStyleSheet("color:#aaa;font-size:11px;"); mmg.addWidget(mm_note)

        mm_opt_row = QHBoxLayout()
        self._mm_enabled = QCheckBox("Run automatically after batch (AO, Curvature & Overlays)")
        self._mm_enabled.setChecked(False)
        mm_opt_row.addWidget(self._mm_enabled)
        mm_opt_row.addStretch()
        mmg.addLayout(mm_opt_row)

        mm_loc_row = QHBoxLayout()
        mm_loc_row.addWidget(QLabel("Output folder:"))
        self._mm_location = QComboBox()
        self._mm_location.addItem("Subfolder inside output  →  …/Misc/",  "inside")
        self._mm_location.addItem("Sibling of output (one level up)  →  ../Misc/", "sibling")
        self._mm_location.setToolTip(
            "Inside:  <your output path>/Misc/\n"
            "Sibling: one folder above your output path, named Misc"
        )
        mm_loc_row.addWidget(self._mm_location, 1)
        mmg.addLayout(mm_loc_row)

        mm_btn_row = QHBoxLayout()
        mm_now_btn = QPushButton("🗺  Export Misc Maps Now")
        mm_now_btn.setToolTip("Export Misc maps (AO, Curvature, Text & Engraving overlays) immediately without running the full skin batch.")
        mm_now_btn.clicked.connect(self._export_misc_maps_standalone)
        mm_btn_row.addStretch(); mm_btn_row.addWidget(mm_now_btn)
        mmg.addLayout(mm_btn_row)
        lay.addWidget(mm_grp)

        # Export categories
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

        # Contextual layers
        ctx_grp = QGroupBox("Contextual Layers  (all optional)"); ctg = QVBoxLayout(ctx_grp); ctg.setSpacing(6)
        self._ctx_note_lbl = QLabel(
            "Toggled automatically per category. Leave any picker on '— none —' to skip."
        )
        self._ctx_note_lbl.setWordWrap(True); self._ctx_note_lbl.setStyleSheet("color:#aaa;font-size:11px;"); ctg.addWidget(self._ctx_note_lbl)
        btn_ref_ctx = QPushButton("↺  Refresh All Layer Pickers"); btn_ref_ctx.clicked.connect(self._refresh_ctx_pickers); ctg.addWidget(btn_ref_ctx)
        LW = 200
        def _crow(label, attr, tip=""):
            r = QHBoxLayout(); l = QLabel(label); l.setFixedWidth(LW); r.addWidget(l)
            c = QComboBox(); c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if tip: c.setToolTip(tip)
            setattr(self, attr, c); r.addWidget(c); ctg.addLayout(r)
        _crow("Text Layer / Folder:",    "_ctx_fill",     "FillLayerNode or GroupLayerNode containing text markings.")
        _crow("Engraving Layer/Folder:", "_ctx_engraving","FillLayerNode or GroupLayerNode containing engravings/markings.")
        _crow("Worn Mask Effect / Layer:","_ctx_worn_paint","Paint or Fill effect in mask_effects(). Toggled ON for Worn category and OFF otherwise.")
        _crow("Black Parts Group:",      "_ctx_bp_group", "GroupLayerNode containing non-worn and worn child layers.")
        _crow("Black Parts — Non-Worn:", "_ctx_bp_nonworn","Visible for Normal and Bright exports.")
        _crow("Black Parts — Worn:",     "_ctx_bp_worn",  "Visible for Worn exports.")
        self._ctx_fill.currentIndexChanged.connect(self._on_fill_changed)
        self._ctx_bp_group.currentIndexChanged.connect(self._on_bp_changed)
        lay.addWidget(ctx_grp)

        # Skin pack structure detection & preview
        sp_grp = QGroupBox("Skin Pack Structure"); spg = QVBoxLayout(sp_grp)
        self._b_struct_label = QLabel("Click 'Detect' or 'Preview' to inspect.")
        self._b_struct_label.setWordWrap(True); spg.addWidget(self._b_struct_label)
        btn_row = QHBoxLayout()
        btn_d = QPushButton("Detect Structure"); btn_d.clicked.connect(self._detect_structure)
        btn_p = QPushButton("🔍 Preview All Jobs"); btn_p.clicked.connect(self._preview_jobs)
        btn_row.addWidget(btn_d); btn_row.addWidget(btn_p); spg.addLayout(btn_row)
        lay.addWidget(sp_grp)

        # Progress bar
        prog_grp = QGroupBox("Progress"); pg = QVBoxLayout(prog_grp)
        self._b_progress = QProgressBar(); self._b_progress.setValue(0); self._b_progress.setFormat("%v / %m"); pg.addWidget(self._b_progress)
        self._b_current = QLabel(""); self._b_current.setAlignment(Qt.AlignCenter); pg.addWidget(self._b_current)
        lay.addWidget(prog_grp)

        # Log output
        log_grp = QGroupBox("Log"); lg = QVBoxLayout(log_grp)
        self._b_log = QTextEdit(); self._b_log.setReadOnly(True); self._b_log.setMinimumHeight(100); self._b_log.setMaximumHeight(180); lg.addWidget(self._b_log)
        bc = QPushButton("Clear"); bc.setFixedWidth(60); bc.clicked.connect(self._b_log.clear); lg.addWidget(bc, alignment=Qt.AlignRight)
        lay.addWidget(log_grp)

        # Run / Cancel buttons
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
    # Build scrollable batch export controls, pickers, progress bar, and log end

    # Rebuild the Texture Set list rows while preserving typed paths and check states start
    def _refresh_ts_list(self):
        saved_paths  = {name: w["path"].text() for name, w in self._ts_widgets.items()}
        saved_checks = {name: w["check"].isChecked() for name, w in self._ts_widgets.items()}

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

                cb = QCheckBox(ts.name)
                cb.setChecked(saved_checks.get(ts.name, True))
                cb.setMinimumWidth(140); cb.setMaximumWidth(160)
                cb.stateChanged.connect(self._on_ts_check_changed)
                row_lay.addWidget(cb)

                path_edit = QLineEdit()
                path_edit.setPlaceholderText("← uses Default Output Folder")
                path_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                if ts.name in saved_paths and saved_paths[ts.name]:
                    path_edit.setText(saved_paths[ts.name])
                row_lay.addWidget(path_edit)

                browse_btn = QPushButton("…"); browse_btn.setFixedWidth(28)
                browse_btn.clicked.connect(lambda checked=False, pe=path_edit: self._browse(pe))
                row_lay.addWidget(browse_btn)

                self._ts_layout.addWidget(row_w)
                # Store explicit Python references on _ts_widgets so PySide6 does not GC signal wrappers
                self._ts_widgets[ts.name] = {"check": cb, "path": path_edit, "browse": browse_btn}

            if not ts_list:
                self._ts_layout.addWidget(QLabel("(no texture sets found)"))
        except Exception as e:
            self._ts_layout.addWidget(QLabel(f"Error: {e}"))

        if not self._b_path.text().strip():
            default = self._get_project_default_path()
            if default:
                self._b_path.setText(default)
    # Rebuild the Texture Set list rows while preserving typed paths and check states end

    # Keep job counts and contextual pickers synced when TS selection changes start
    def _on_ts_check_changed(self, _=None):
        if self._batch_running:
            return
        self._update_job_count()
        new_primary = self._get_primary_stack()
        if new_primary is not None and not _same_stack(new_primary, self._ctx_stack):
            # Preserve current picker names before re-populating for the new primary stack
            self._snapshot_current_ctx_names()
            self._refresh_ctx_pickers()
    # Keep job counts and contextual pickers synced when TS selection changes end

    # Return checked TextureSet objects without falling back to all when none are checked start
    def _selected_ts(self):
        """Return list of checked TextureSet objects."""
        if not substance_painter.project.is_open(): return []
        try:
            all_ts = substance_painter.textureset.all_texture_sets()
            if not self._ts_widgets:
                return all_ts
            return [ts for ts in all_ts
                    if ts.name in self._ts_widgets
                    and self._ts_widgets[ts.name]["check"].isChecked()]
        except Exception: return []
    # Return checked TextureSet objects without falling back to all when none are checked end

    # Resolve output folder path for a given Texture Set name start
    def _resolved_path_for(self, ts_name: str) -> str:
        """Per-TS path if set, otherwise the global default path."""
        if ts_name in self._ts_widgets:
            p = self._ts_widgets[ts_name]["path"].text().strip().replace("\\", "/")
            if p: return p
        return self._b_path.text().strip().replace("\\", "/")
    # Resolve output folder path for a given Texture Set name end

    # Group selected Texture Sets by their resolved output path start
    def _ts_groups_by_path(self, sel_ts: list) -> dict:
        buckets = defaultdict(list)
        for ts in sel_ts:
            buckets[self._resolved_path_for(ts.name)].append(ts)
        return dict(buckets)
    # Group selected Texture Sets by their resolved output path end

    # Return the set of currently checked export category names start
    def _enabled_cat_types(self) -> set:
        return {ct for ct, cb in self._cat_checks.items() if cb.isChecked()}
    # Return the set of currently checked export category names end

    # Update the structure summary file-count label when checkboxes change start
    def _update_job_count(self):
        if not substance_painter.project.is_open(): return
        try:
            stack = self._get_primary_stack()
            if stack is None: return
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
    # Update the structure summary file-count label when checkboxes change end

    # Snapshot current contextual dropdown names into _saved_ctx start
    def _snapshot_current_ctx_names(self):
        if self._ctx_fill.count() <= 1 and self._ctx_engraving.count() <= 1:
            return
        self._saved_ctx = {
            "fill":      (self._ctx_fill.currentData().get_name()       if self._ctx_fill.currentData()       else None),
            "engraving": (self._ctx_engraving.currentData().get_name()  if self._ctx_engraving.currentData()  else None),
            "wp":        (self._ctx_worn_paint.currentData().get_name() if self._ctx_worn_paint.currentData() else None),
            "wp_label":  (self._ctx_worn_paint.currentText()            if self._ctx_worn_paint.currentData() else None),
            "bp":        (self._ctx_bp_group.currentData().get_name()   if self._ctx_bp_group.currentData()   else None),
            "bpnw":      (self._ctx_bp_nonworn.currentData().get_name() if self._ctx_bp_nonworn.currentData() else None),
            "bpw":       (self._ctx_bp_worn.currentData().get_name()    if self._ctx_bp_worn.currentData()    else None),
        }
    # Snapshot current contextual dropdown names into _saved_ctx end

    # Refresh all contextual layer dropdowns from the primary stack start
    def _refresh_ctx_pickers(self):
        if not substance_painter.project.is_open(): return
        try:
            stack = self._get_primary_stack()
            if stack is None: return
            self._ctx_stack = stack
            skin_root = find_skin_pack_root(stack)
            auto      = _auto_detect_ctx(stack, skin_root)
            self._populate_fill_picker(stack, auto["fill"])
            self._populate_engraving_picker(stack, auto["engraving"])
            self._populate_bp_group_picker(stack, skin_root, auto["bp_group"])
            self._auto_select_bp_children(auto["bp_nonworn"], auto["bp_worn"])
            self._try_restore_ctx_by_name()
        except Exception as e: self._sp_log(f"Ctx picker refresh: {e}", warn=True)
    # Refresh all contextual layer dropdowns from the primary stack end

    # Populate the Text layer/folder dropdown start
    def _populate_fill_picker(self, stack, auto_select=None):
        self._ctx_fill.blockSignals(True)
        self._ctx_fill.clear()
        self._ctx_fill.addItem("— none —", None)
        best = 0
        idx = 0
        for d, n in _walk_all(stack):
            if _is_group(n):
                idx += 1
                self._ctx_fill.addItem("  " * d + f"📁 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
            elif _is_fill(n):
                idx += 1
                self._ctx_fill.addItem("  " * d + f"📄 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
            elif _is_proper_layer(n):
                idx += 1
                self._ctx_fill.addItem("  " * d + f"🎨 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
        self._ctx_fill.blockSignals(False)
        self._ctx_fill.setCurrentIndex(best)
        self._populate_worn_paint_picker(auto_first=(best > 0))
    # Populate the Text layer/folder dropdown end

    # Populate the Engraving layer/folder dropdown start
    def _populate_engraving_picker(self, stack, auto_select=None):
        self._ctx_engraving.blockSignals(True)
        self._ctx_engraving.clear()
        self._ctx_engraving.addItem("— none —", None)
        best = 0
        idx = 0
        for d, n in _walk_all(stack):
            if _is_group(n):
                idx += 1
                self._ctx_engraving.addItem("  " * d + f"📁 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
            elif _is_fill(n):
                idx += 1
                self._ctx_engraving.addItem("  " * d + f"📄 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
            elif _is_proper_layer(n):
                idx += 1
                self._ctx_engraving.addItem("  " * d + f"🎨 {n.get_name()}", n)
                if _same_node(auto_select, n): best = idx
        self._ctx_engraving.blockSignals(False)
        self._ctx_engraving.setCurrentIndex(best)
    # Populate the Engraving layer/folder dropdown end

    # Populate the Worn mask paint/fill effect dropdown from the selected Text node start
    def _populate_worn_paint_picker(self, auto_first=False):
        self._ctx_worn_paint.blockSignals(True); self._ctx_worn_paint.clear(); self._ctx_worn_paint.addItem("— none —", None)
        best = 0; best_had_worn = False; fill = self._ctx_fill.currentData()
        if fill is not None:
            try:
                cnt = 0
                for fx in fill.mask_effects():
                    if _is_paint_fx(fx) or _is_fill_fx(fx):
                        cnt += 1
                        self._ctx_worn_paint.addItem(fx.get_name(), fx)
                        is_worn = "worn" in fx.get_name().lower()
                        if auto_first:
                            if best == 0 or (is_worn and not best_had_worn):
                                best = cnt
                                if is_worn: best_had_worn = True
                if _is_group(fill):
                    for ch in fill.sub_layers():
                        try:
                            for fx in ch.mask_effects():
                                if _is_paint_fx(fx) or _is_fill_fx(fx):
                                    cnt += 1
                                    self._ctx_worn_paint.addItem(f"{ch.get_name()} → {fx.get_name()}", fx)
                                    is_worn = "worn" in fx.get_name().lower()
                                    if auto_first:
                                        if best == 0 or (is_worn and not best_had_worn):
                                            best = cnt
                                            if is_worn: best_had_worn = True
                        except Exception: pass
                        if "worn" in ch.get_name().lower():
                            cnt += 1
                            self._ctx_worn_paint.addItem(f"{ch.get_name()} (layer)", ch)
                            if auto_first:
                                if best == 0 or not best_had_worn:
                                    best = cnt
                                    best_had_worn = True
            except Exception as e: self._sp_log(f"Worn paint picker: {e}", warn=True)
        self._ctx_worn_paint.blockSignals(False); self._ctx_worn_paint.setCurrentIndex(best)
    # Populate the Worn mask paint/fill effect dropdown from the selected Text node end

    # Populate the Black Parts group dropdown start
    def _populate_bp_group_picker(self, stack, skin_root=None, auto_select=None):
        self._ctx_bp_group.blockSignals(True); self._ctx_bp_group.clear(); self._ctx_bp_group.addItem("— none —", None)
        best = 0
        for i, (d, n) in enumerate(_walk_groups(stack)):
            self._ctx_bp_group.addItem("  " * d + n.get_name(), n)
            if _same_node(auto_select, n): best = i + 1
        self._ctx_bp_group.blockSignals(False); self._ctx_bp_group.setCurrentIndex(best)
        self._populate_bp_child_pickers()
    # Populate the Black Parts group dropdown end

    # Populate Non-Worn and Worn child dropdowns from the selected Black Parts group start
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
    # Populate Non-Worn and Worn child dropdowns from the selected Black Parts group end

    # Select auto-detected Non-Worn and Worn Black Parts children by uid start
    def _auto_select_bp_children(self, nonworn, worn):
        for combo, target in ((self._ctx_bp_nonworn, nonworn), (self._ctx_bp_worn, worn)):
            if target is None: continue
            for i in range(combo.count()):
                if _same_node(combo.itemData(i), target):
                    combo.setCurrentIndex(i)
                    break
    # Select auto-detected Non-Worn and Worn Black Parts children by uid end

    # Re-populate Worn mask picker when Text layer selection changes start
    def _on_fill_changed(self, _): self._populate_worn_paint_picker(auto_first=True)
    # Re-populate Worn mask picker when Text layer selection changes end

    # Re-populate Black Parts child pickers when Black Parts group changes start
    def _on_bp_changed(self, _):
        self._populate_bp_child_pickers()
        bp = self._ctx_bp_group.currentData()
        if bp is not None:
            try:
                children = [c for c in bp.sub_layers() if _is_proper_layer(c)]
                worn_ch = nonworn_ch = None
                for ch in children:
                    if "worn" in ch.get_name().lower():
                        if not worn_ch: worn_ch = ch
                    else:
                        if not nonworn_ch: nonworn_ch = ch
                if not nonworn_ch and children:        nonworn_ch = children[0]
                if not worn_ch and len(children) >= 2: worn_ch    = children[1]
                self._auto_select_bp_children(nonworn_ch, worn_ch)
            except Exception:
                pass
    # Re-populate Black Parts child pickers when Black Parts group changes end

    # Collect currently selected contextual nodes for the primary stack start
    def _ctx_nodes(self):
        fill = self._ctx_fill.currentData()
        inv  = None
        if fill is not None:
            try:
                for fx in fill.content_effects():
                    if _is_filter_fx(fx): inv = fx; break
            except Exception: pass
            if not inv and _is_group(fill):
                try:
                    for ch in fill.sub_layers():
                        for fx in ch.content_effects():
                            if _is_filter_fx(fx): inv = fx; break
                        if inv: break
                except Exception: pass
        return {"fill": fill, "invert": inv,
                "worn_paint": self._ctx_worn_paint.currentData(),
                "bp_group":   self._ctx_bp_group.currentData(),
                "bp_nonworn": self._ctx_bp_nonworn.currentData(),
                "bp_worn":    self._ctx_bp_worn.currentData(),
                "engraving":  self._ctx_engraving.currentData()}
    # Collect currently selected contextual nodes for the primary stack end

    # Resolve matching contextual nodes for any stack while respecting '— none —' start
    def _resolve_ctx_for_stack(self, st, skin_root, primary_ctx: dict) -> dict:
        if _same_stack(st, self._ctx_stack):
            return primary_ctx

        auto = _auto_detect_ctx(st, skin_root)
        all_nodes = _walk_all(st)

        # Text layer / folder (skip if primary picker is set to '— none —')
        fill = inv = wp = None
        if primary_ctx.get("fill") is not None:
            target_fill = primary_ctx["fill"].get_name()
            for _, fn in all_nodes:
                if fn.get_name() == target_fill:
                    fill = fn; break
            if fill is None:
                fill = auto["fill"]

        if fill is not None:
            try:
                for fx in fill.content_effects():
                    if _is_filter_fx(fx): inv = fx; break
            except Exception: pass
            if not inv and _is_group(fill):
                try:
                    for ch in fill.sub_layers():
                        for fx in ch.content_effects():
                            if _is_filter_fx(fx): inv = fx; break
                        if inv: break
                except Exception: pass

            # Worn mask effect / layer (skip if primary picker is set to '— none —')
            if primary_ctx.get("worn_paint") is not None:
                target_wp = primary_ctx["worn_paint"].get_name()
                try:
                    for fx in fill.mask_effects():
                        if fx.get_name() == target_wp: wp = fx; break
                except Exception: pass
                if not wp and _is_group(fill):
                    try:
                        for ch in fill.sub_layers():
                            if ch.get_name() == target_wp: wp = ch; break
                            for fx in ch.mask_effects():
                                if fx.get_name() == target_wp: wp = fx; break
                            if wp: break
                    except Exception: pass
                if not wp:
                    try:
                        for fx in fill.mask_effects():
                            if _is_paint_fx(fx) or _is_fill_fx(fx):
                                if "worn" in fx.get_name().lower() or wp is None:
                                    wp = fx
                                    if "worn" in fx.get_name().lower(): break
                    except Exception: pass
                if not wp and _is_group(fill):
                    try:
                        for ch in fill.sub_layers():
                            for fx in ch.mask_effects():
                                if _is_paint_fx(fx) or _is_fill_fx(fx):
                                    if "worn" in fx.get_name().lower() or wp is None:
                                        wp = fx
                                        if "worn" in fx.get_name().lower(): break
                            if wp and "worn" in wp.get_name().lower(): break
                            if "worn" in ch.get_name().lower():
                                wp = ch; break
                    except Exception: pass

        # Engraving layer / folder (skip if primary picker is set to '— none —')
        eng = None
        if primary_ctx.get("engraving") is not None:
            target_eng = primary_ctx["engraving"].get_name()
            for _, en in all_nodes:
                if en.get_name() == target_eng:
                    eng = en; break
            if eng is None:
                eng = auto["engraving"]

        # Black Parts group & children (skip if primary picker is set to '— none —')
        bp_grp = bp_nw = bp_w = None
        if primary_ctx.get("bp_group") is not None:
            target_bp = primary_ctx["bp_group"].get_name()
            for _, gn in _walk_groups(st):
                if gn.get_name() == target_bp:
                    bp_grp = gn; break
            if bp_grp is None:
                bp_grp = auto["bp_group"]

            if bp_grp is not None:
                try:
                    children = [c for c in bp_grp.sub_layers() if _is_proper_layer(c)]
                except Exception:
                    children = []

                if primary_ctx.get("bp_nonworn") is not None:
                    tnw = primary_ctx["bp_nonworn"].get_name()
                    for ch in children:
                        if ch.get_name() == tnw:
                            bp_nw = ch; break
                    if bp_nw is None:
                        bp_nw = auto["bp_nonworn"] if _same_node(bp_grp, auto["bp_group"]) else (
                            next((c for c in children if "worn" not in c.get_name().lower()), children[0] if children else None)
                        )

                if primary_ctx.get("bp_worn") is not None:
                    tw = primary_ctx["bp_worn"].get_name()
                    for ch in children:
                        if ch.get_name() == tw:
                            bp_w = ch; break
                    if bp_w is None:
                        bp_w = auto["bp_worn"] if _same_node(bp_grp, auto["bp_group"]) else (
                            next((c for c in children if "worn" in c.get_name().lower()), children[1] if len(children) >= 2 else None)
                        )

        return {"fill": fill, "invert": inv, "worn_paint": wp,
                "bp_group": bp_grp, "bp_nonworn": bp_nw,
                "bp_worn": bp_w, "engraving": eng}
    # Resolve matching contextual nodes for any stack while respecting '— none —' end

    # Toggle contextual layer visibilities for Normal, Worn, or Bright category start
    def _apply_ctx(self, cat_type: str, ctx: dict):
        """
        Normal : invert=ON,  worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF
        Worn   : invert=ON,  worn_paint=ON,  bp_nonworn=OFF, bp_worn=ON
        Bright : invert=OFF, worn_paint=OFF, bp_nonworn=ON,  bp_worn=OFF
        """
        inv_on = cat_type in ("normal", "worn")
        wp_on  = (cat_type == "worn")
        nw_on  = cat_type in ("normal", "bright")
        def _s(n, v):
            if n is not None:
                try: n.set_visible(v)
                except Exception as e: self._sp_log(f"set_visible on {n.get_name()}: {e}", warn=True)
        _s(ctx["invert"],     inv_on)
        _s(ctx["worn_paint"], wp_on)
        _s(ctx["bp_nonworn"], nw_on)
        _s(ctx["bp_worn"],    not nw_on)
    # Toggle contextual layer visibilities for Normal, Worn, or Bright category end

    # Persist settings to disk without wiping saved project data when no project is open start
    def _save_config(self):
        existing = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = {}

        proj_open = substance_painter.project.is_open() and bool(self._ts_widgets)
        merged_paths  = dict(existing.get("ts_paths", {}))
        merged_checks = dict(existing.get("ts_checks", {}))
        if proj_open:
            for name, w in self._ts_widgets.items():
                merged_paths[name]  = w["path"].text()
                merged_checks[name] = w["check"].isChecked()

        cfg = {
            "output_path": self._b_path.text() or existing.get("output_path", ""),
            "format":      self._b_fmt.currentText(),
            "worn_suffix": self._b_worn_suffix.text(),
            "channels":    [k for k, cb in self._channel_checks.items() if cb.isChecked()],
            "categories":  [ct for ct, cb in self._cat_checks.items() if cb.isChecked()],
            "ts_paths":    merged_paths,
            "ts_checks":   merged_checks,
            "mm_enabled":  self._mm_enabled.isChecked(),
            "mm_location": self._mm_location.currentData(),
        }

        if proj_open:
            cfg.update({
                "ctx_fill_name":      (self._ctx_fill.currentData().get_name()       if self._ctx_fill.currentData()       else None),
                "ctx_engraving_name": (self._ctx_engraving.currentData().get_name()  if self._ctx_engraving.currentData()  else None),
                "ctx_wp_name":        (self._ctx_worn_paint.currentData().get_name() if self._ctx_worn_paint.currentData() else None),
                "ctx_wp_label":       (self._ctx_worn_paint.currentText()            if self._ctx_worn_paint.currentData() else None),
                "ctx_bp_name":        (self._ctx_bp_group.currentData().get_name()   if self._ctx_bp_group.currentData()   else None),
                "ctx_bpnw_name":      (self._ctx_bp_nonworn.currentData().get_name() if self._ctx_bp_nonworn.currentData() else None),
                "ctx_bpw_name":       (self._ctx_bp_worn.currentData().get_name()    if self._ctx_bp_worn.currentData()    else None),
            })
        else:
            for k in ("ctx_fill_name", "ctx_engraving_name", "ctx_wp_name", "ctx_wp_label",
                      "ctx_bp_name", "ctx_bpnw_name", "ctx_bpw_name"):
                cfg[k] = existing.get(k)

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            self._sp_log(f"Could not save config: {e}", warn=True)
    # Persist settings to disk without wiping saved project data when no project is open end

    # Load saved configuration from disk on startup start
    def _load_config(self):
        if not os.path.exists(CONFIG_PATH): return
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f: cfg = json.load(f)
        except Exception: return
        if cfg.get("output_path"):                   self._b_path.setText(cfg["output_path"])
        if cfg.get("format") in self.FORMATS:        self._b_fmt.setCurrentText(cfg["format"])
        if cfg.get("worn_suffix") is not None:       self._b_worn_suffix.setText(cfg["worn_suffix"])
        if "mm_enabled" in cfg:
            self._mm_enabled.setChecked(bool(cfg["mm_enabled"]))
        if cfg.get("mm_location") in ("inside", "sibling"):
            idx = 0 if cfg["mm_location"] == "inside" else 1
            self._mm_location.setCurrentIndex(idx)
        if cfg.get("channels"):
            for key, cb in self._channel_checks.items(): cb.setChecked(key in cfg["channels"])
        if cfg.get("categories"):
            for ct, cb in self._cat_checks.items(): cb.setChecked(ct in cfg["categories"])
        self._saved_ts_paths  = cfg.get("ts_paths", {})
        self._saved_ts_checks = cfg.get("ts_checks", {})
        self._saved_ctx = {
            "fill":      cfg.get("ctx_fill_name"),
            "engraving": cfg.get("ctx_engraving_name"),
            "wp":        cfg.get("ctx_wp_name"),
            "wp_label":  cfg.get("ctx_wp_label"),
            "bp":        cfg.get("ctx_bp_name"),
            "bpnw":      cfg.get("ctx_bpnw_name"),
            "bpw":       cfg.get("ctx_bpw_name"),
        }
    # Load saved configuration from disk on startup end

    # Restore saved contextual layer selections by name and label start
    def _try_restore_ctx_by_name(self):
        saved = getattr(self, "_saved_ctx", {})
        if not saved:
            return

        def _match(combo, name):
            if not name: return
            for i in range(combo.count()):
                d = combo.itemData(i)
                if d is not None and d.get_name() == name:
                    combo.setCurrentIndex(i)
                    return

        _match(self._ctx_fill,      saved.get("fill"))
        _match(self._ctx_engraving, saved.get("engraving"))

        # Prefer exact combo label (e.g. "ChildLayer → Worn") before falling back to node name
        wp_label = saved.get("wp_label")
        matched_wp = False
        if wp_label:
            for i in range(self._ctx_worn_paint.count()):
                if self._ctx_worn_paint.itemData(i) is not None and self._ctx_worn_paint.itemText(i) == wp_label:
                    self._ctx_worn_paint.setCurrentIndex(i)
                    matched_wp = True
                    break
        if not matched_wp:
            _match(self._ctx_worn_paint, saved.get("wp"))

        _match(self._ctx_bp_group,   saved.get("bp"))
        _match(self._ctx_bp_nonworn, saved.get("bpnw"))
        _match(self._ctx_bp_worn,    saved.get("bpw"))
    # Restore saved contextual layer selections by name and label end

    # Apply saved per-Texture-Set paths and checkbox states after list refresh start
    def _apply_saved_ts_data(self):
        saved_paths  = getattr(self, "_saved_ts_paths",  {})
        saved_checks = getattr(self, "_saved_ts_checks", {})
        for name, w in self._ts_widgets.items():
            if name in saved_paths and saved_paths[name] and not w["path"].text().strip():
                w["path"].setText(saved_paths[name])
            if name in saved_checks:
                w["check"].blockSignals(True)
                w["check"].setChecked(saved_checks[name])
                w["check"].blockSignals(False)
    # Apply saved per-Texture-Set paths and checkbox states after list refresh end

    # Determine default output folder from the open Substance Painter project path start
    def _get_project_default_path(self) -> str:
        try:
            if not substance_painter.project.is_open():
                return ""
            fp = substance_painter.project.file_path()
            if not fp:
                return ""
            project_dir = os.path.dirname(os.path.abspath(fp))
            textures_dir = os.path.join(project_dir, "Textures")
            if os.path.isdir(textures_dir):
                return textures_dir.replace("\\", "/")
            return project_dir.replace("\\", "/")
        except Exception:
            return ""
    # Determine default output folder from the open Substance Painter project path end

    # Check or uncheck all Texture Set rows at once start
    def _set_all_ts_checked(self, state: bool):
        for w in self._ts_widgets.values():
            w["check"].blockSignals(True)
            w["check"].setChecked(state)
            w["check"].blockSignals(False)
        self._on_ts_check_changed()
    # Check or uncheck all Texture Set rows at once end

    # Clear all per-Texture-Set output path fields start
    def _clear_all_ts_paths(self):
        for w in self._ts_widgets.values():
            w["path"].clear()
    # Clear all per-Texture-Set output path fields end

    # Browse for Default Output Folder and silently auto-match empty per-TS paths start
    def _browse_default_and_find(self):
        self._browse(self._b_path)
        if self._b_path.text().strip():
            self._find_ts_folders(show_dialog=False, overwrite_existing=False)
    # Browse for Default Output Folder and silently auto-match empty per-TS paths end

    # Scan subfolders under 'textures' root and match each Texture Set path start
    def _find_ts_folders(self, show_dialog: bool = True, overwrite_existing: bool = True):
        base = self._b_path.text().strip()
        if not base:
            if show_dialog:
                QMessageBox.warning(self, "No Default Path",
                    "Set a Default Output Folder first so the plugin knows\n"
                    "where to start looking for a 'textures' folder.")
            return

        textures_root = _find_textures_root(base)
        if textures_root is None:
            if show_dialog:
                QMessageBox.warning(self, "No 'textures' Folder Found",
                    f"Could not find a folder named 'textures' (case-insensitive)\n"
                    f"anywhere above:\n  {base}\n\n"
                    f"Make sure the Default Output Folder is inside a folder\n"
                    f"called 'textures'.")
            return

        if show_dialog:
            dlg = QDialog(self)
            dlg.setWindowTitle("🔍 Finding Folders…")
            dlg.setMinimumWidth(580)
            dlg.setMinimumHeight(380)
            dlg_lay = QVBoxLayout(dlg)

            dlg_info = QLabel(f"Textures root:  {textures_root}")
            dlg_info.setStyleSheet("font-size:11px; color:#bbb;")
            dlg_lay.addWidget(dlg_info)

            dlg_scan_lbl = QLabel("Scanning subfolders…")
            dlg_scan_lbl.setStyleSheet("font-size:11px; color:#aaa;")
            dlg_lay.addWidget(dlg_scan_lbl)

            dlg_bar = QProgressBar()
            dlg_bar.setRange(0, 0)
            dlg_bar.setFormat("Scanning…")
            dlg_lay.addWidget(dlg_bar)

            dlg_log = QTextEdit()
            dlg_log.setReadOnly(True)
            dlg_log.setStyleSheet("font-family: monospace; font-size:11px;")
            dlg_log.setMinimumHeight(220)
            dlg_lay.addWidget(dlg_log)

            dlg_close = QPushButton("Close")
            dlg_close.setEnabled(False)
            dlg_close.clicked.connect(dlg.accept)
            dlg_lay.addWidget(dlg_close, alignment=Qt.AlignRight)

            dlg.show()
            QCoreApplication.processEvents()

        all_folders = _collect_subfolders(textures_root, max_depth=5)

        if show_dialog:
            dlg_scan_lbl.setText(
                f"Found {len(all_folders)} subfolders (max depth 5) — matching {len(self._ts_widgets)} Texture Sets…"
            )
            dlg_bar.setRange(0, max(1, len(self._ts_widgets)))
            dlg_bar.setValue(0)
            dlg_bar.setFormat("%v / %m  TSes")
            QCoreApplication.processEvents()

        if not all_folders:
            if show_dialog:
                dlg_log.append("⚠  No subfolders found under the textures root.")
                dlg_close.setEnabled(True)
            return

        matched = skipped = unmatched = 0

        for i, (ts_name, w) in enumerate(self._ts_widgets.items()):
            current = w["path"].text().strip()

            if not overwrite_existing and current:
                if show_dialog:
                    dlg_log.append(f"⏭  {ts_name}  (already set — skipped)")
                    dlg_bar.setValue(i + 1)
                    QCoreApplication.processEvents()
                skipped += 1
                continue

            abs_path, reason = _find_best_deep_match(ts_name, all_folders)

            if show_dialog:
                dlg_log.append(reason)
                dlg_bar.setValue(i + 1)
                QCoreApplication.processEvents()

            if abs_path:
                w["path"].setText(abs_path)
                matched += 1
            else:
                unmatched += 1

        if show_dialog:
            dlg_bar.setValue(len(self._ts_widgets))
            dlg_bar.setFormat("Done")
            dlg_log.append(
                f"\nSummary:\n"
                f"✅  Matched  : {matched}\n"
                f"❌  No match : {unmatched}\n"
                f"⏭  Skipped  : {skipped}  (already had a path)"
            )
            dlg_close.setEnabled(True)
    # Scan subfolders under 'textures' root and match each Texture Set path end

    # Open folder picker dialog and populate target QLineEdit start
    def _browse(self, target: QLineEdit):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Output Folder",
            target.text().strip() or self._last_dir
        )
        if folder:
            folder = folder.replace("\\", "/")
            target.setText(folder); self._last_dir = folder
    # Open folder picker dialog and populate target QLineEdit end

    # Write message to Substance Painter's application log start
    def _sp_log(self, msg, warn=False):
        substance_painter.logging.log(
            substance_painter.logging.WARNING if warn else substance_painter.logging.INFO,
            "TextureExporter", msg
        )
    # Write message to Substance Painter's application log end

    # Append message to both UI log box and Substance Painter log start
    def _blog(self, msg): self._b_log.append(msg); self._sp_log(msg)
    # Append message to both UI log box and Substance Painter log end

    # Advance progress bars on both main window and floating progress dialog start
    def _tick(self, done, total, name):
        self._b_progress.setMaximum(max(1, total))
        self._b_progress.setValue(done)
        self._b_current.setText(f"Exporting: {name}  ({done}/{total})")
        if self._prog_dlg is not None:
            self._prog_dlg.update_progress(done, total, f"Exporting: {name}")
        else:
            QCoreApplication.processEvents()
    # Advance progress bars on both main window and floating progress dialog end

    # Return list of currently checked channel keys start
    def _enabled_channels(self):
        return [k for k, cb in self._channel_checks.items() if cb.isChecked()]
    # Return list of currently checked channel keys end

    # Return the primary Stack (first checked TS, or active stack if none checked) start
    def _get_primary_stack(self):
        if not substance_painter.project.is_open():
            return None
        sel = self._selected_ts()
        if sel:
            return sel[0].get_stack()
        try:
            return substance_painter.textureset.get_active_stack()
        except Exception:
            return None
    # Return the primary Stack (first checked TS, or active stack if none checked) end

    # Inspect and display the detected 3-level skin pack structure start
    def _detect_structure(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack = self._get_primary_stack()
            if stack is None:
                self._b_struct_label.setText("⚠ No stack available."); return
            root = find_skin_pack_root(stack)
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
            elif worn_p:
                lines.append(f"  [Worn]    \"{worn_p.get_name()}\" (solo)  →  {len(_group_children(worn_p))} skins")
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
    # Inspect and display the detected 3-level skin pack structure end

    # Preview all output filenames in the log without exporting start
    def _preview_jobs(self):
        if not substance_painter.project.is_open():
            self._b_struct_label.setText("⚠ No project open."); return
        try:
            stack = self._get_primary_stack()
            if stack is None:
                self._b_struct_label.setText("⚠ No stack available."); return
            root = find_skin_pack_root(stack)
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
                    ts_prefix = "$textureSet_" if multi_ts else ""
                    udim_sfx  = "_$udim" if _any_uv_tiles(ts_group) else ""
                    for ch_key in channels:
                        ch_sfx = f"_{CHANNEL_DEFS[ch_key]['suffix']}" if multi_ch else ""
                        self._b_log.append(
                            f"  [{job['cat_type']:6}]  {out_path}/  {ts_prefix}{name}{ch_sfx}{udim_sfx}"
                        )
            total = sum(
                len([j for j in jobs if j["cat_type"] in enabled])
                * len(ts_group) * max(1, len(channels))
                for ts_group in buckets.values()
            )
            self._b_log.append(f"─── {total} files total ───")
            self._b_struct_label.setText(f"Preview: {total} files across {len(buckets)} output path(s).")
        except Exception as e: self._b_log.append(f"❌ Preview error: {e}")
    # Preview all output filenames in the log without exporting end

    # Flag batch cancellation and update buttons immediately start
    def _request_cancel(self):
        self._cancel_requested = True
        self._b_cancel_btn.setEnabled(False)
        self._b_cancel_btn_top.setEnabled(False)
        self._b_current.setText("Cancelling after current export…")
        if self._prog_dlg is not None:
            self._prog_dlg.set_cancelling()
        QCoreApplication.processEvents()
    # Flag batch cancellation and update buttons immediately end

    # Count actual Misc export calls that will execute for the given buckets start
    def _count_misc_steps(self, buckets: dict, stack_infos_by_ts: dict = None) -> int:
        primary_ctx = self._ctx_nodes()
        steps_per_bucket = 1
        if primary_ctx.get("fill") is not None:
            steps_per_bucket += 1
        if primary_ctx.get("engraving") is not None:
            steps_per_bucket += 1
        return len(buckets) * steps_per_bucket
    # Count actual Misc export calls that will execute for the given buckets end

    # Export AO, Curvature, Text Overlay, and Engraving Overlay into Misc folders start
    def _export_misc_maps(self, buckets: dict, sp_fmt: str, stack_infos_by_ts: dict = None, tick_cb=None) -> tuple:
        """
        Export Misc files for every path bucket.
        Returns (errors: list[str], failed_paths: set[str]).
        """
        sibling      = (self._mm_location.currentData() == "sibling")
        errors       = []
        failed_paths = set()
        ES           = substance_painter.export.ExportStatus
        primary_ctx  = self._ctx_nodes()

        for out_path, ts_group in buckets.items():
            if self._cancel_requested: break
            misc_dir = _resolve_misc_dir(out_path, sibling)
            use_udim = _any_uv_tiles(ts_group)

            try:
                os.makedirs(misc_dir, exist_ok=True)
            except Exception as e:
                msg = f"Could not create {misc_dir}: {e}"
                self._blog(f"❌  [misc]  {msg}")
                errors.append(msg); failed_paths.add(out_path); continue

            # 1. Export Mesh Maps (AO & Curvature)
            if tick_cb: tick_cb("Misc: AO & Curvature")
            ts_root_paths = [_ts_root_path(ts) for ts in ts_group]
            config_mesh = build_meshmap_export_config(
                ts_root_paths = ts_root_paths,
                out_dir       = misc_dir,
                sp_format     = sp_fmt,
                multi_ts      = True,
                use_udim      = use_udim,
            )
            try:
                r = substance_painter.export.export_project_textures(config_mesh)
                QCoreApplication.processEvents()
                if r.status in (ES.Success, ES.Warning):
                    n = sum(len(v) for v in r.textures.values())
                    suffix = f" ⚠ {r.message}" if r.status == ES.Warning else ""
                    self._blog(f"✅  [misc]  AO + Curvature → {misc_dir}  ({n} files){suffix}")
                elif r.status == ES.Cancelled:
                    self._cancel_requested = True
                    self._blog("⚠  [misc]  Cancelled by user.")
                    break
                else:
                    msg = f"{out_path} (mesh maps): {r.message}"
                    self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)
            except Exception as e:
                QCoreApplication.processEvents()
                msg = f"{out_path} (mesh maps): {e}"
                self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)

            if self._cancel_requested: break

            # Resolve stack ctx and roots for each TS in this bucket
            ts_data = []
            touched_nodes = []
            for ts in ts_group:
                try:
                    st = ts.get_stack()
                    r = find_skin_pack_root(st)
                    if r is not None: touched_nodes.append(r)
                    if stack_infos_by_ts and ts.name in stack_infos_by_ts:
                        ctx = stack_infos_by_ts[ts.name]["ctx"]
                    else:
                        ctx = self._resolve_ctx_for_stack(st, r, primary_ctx)

                    touched_nodes.extend(_collect_solo_nodes(st, ctx.get("fill")))
                    touched_nodes.extend(_collect_solo_nodes(st, ctx.get("engraving")))
                    for k in ("invert", "worn_paint"):
                        if ctx.get(k) is not None:
                            touched_nodes.append(ctx[k])

                    ts_data.append({"ts": ts, "stack": st, "root": r, "ctx": ctx})
                except Exception as e:
                    self._sp_log(f"Misc export ctx setup for {ts.name}: {e}", warn=True)

            vis_state = _save_vis(touched_nodes)
            try:
                # 2. Export Text Overlay (transparent background)
                ts_with_text = [d for d in ts_data if d["ctx"].get("fill") is not None]
                if ts_with_text and not self._cancel_requested:
                    if tick_cb: tick_cb("Misc: Overlay Text")
                    for d in ts_data:
                        ctx = d["ctx"]
                        f   = ctx.get("fill")
                        _solo_node(d["stack"], f)
                        if f is not None:
                            if ctx.get("invert") is not None:
                                try: ctx["invert"].set_visible(True)
                                except Exception: pass
                            if ctx.get("worn_paint") is not None:
                                try: ctx["worn_paint"].set_visible(False)
                                except Exception: pass

                    config_text = build_overlay_export_config(
                        ts_root_paths = [_ts_root_path(d["ts"]) for d in ts_with_text],
                        out_dir       = misc_dir,
                        sp_format     = sp_fmt,
                        map_fileName  = "Overlay_Text_$textureSet",
                        use_udim      = _any_uv_tiles([d["ts"] for d in ts_with_text]),
                    )
                    try:
                        r = substance_painter.export.export_project_textures(config_text)
                        QCoreApplication.processEvents()
                        if r.status in (ES.Success, ES.Warning):
                            n = sum(len(v) for v in r.textures.values())
                            suffix = f" ⚠ {r.message}" if r.status == ES.Warning else ""
                            self._blog(f"✅  [misc]  Overlay Text → {misc_dir}  ({n} files){suffix}")
                        elif r.status == ES.Cancelled:
                            self._cancel_requested = True
                            self._blog("⚠  [misc]  Cancelled by user.")
                            break
                        else:
                            msg = f"{out_path} (Text overlay): {r.message}"
                            self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)
                    except Exception as e:
                        QCoreApplication.processEvents()
                        msg = f"{out_path} (Text overlay): {e}"
                        self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)

                if self._cancel_requested: break

                # 3. Export Engraving Overlay (transparent background)
                ts_with_eng = [d for d in ts_data if d["ctx"].get("engraving") is not None]
                if ts_with_eng and not self._cancel_requested:
                    if tick_cb: tick_cb("Misc: Overlay Engraving")
                    for d in ts_data:
                        ctx = d["ctx"]
                        eng = ctx.get("engraving")
                        _solo_node(d["stack"], eng)

                    config_eng = build_overlay_export_config(
                        ts_root_paths = [_ts_root_path(d["ts"]) for d in ts_with_eng],
                        out_dir       = misc_dir,
                        sp_format     = sp_fmt,
                        map_fileName  = "Overlay_Engraving_$textureSet",
                        use_udim      = _any_uv_tiles([d["ts"] for d in ts_with_eng]),
                    )
                    try:
                        r = substance_painter.export.export_project_textures(config_eng)
                        QCoreApplication.processEvents()
                        if r.status in (ES.Success, ES.Warning):
                            n = sum(len(v) for v in r.textures.values())
                            suffix = f" ⚠ {r.message}" if r.status == ES.Warning else ""
                            self._blog(f"✅  [misc]  Overlay Engraving → {misc_dir}  ({n} files){suffix}")
                        elif r.status == ES.Cancelled:
                            self._cancel_requested = True
                            self._blog("⚠  [misc]  Cancelled by user.")
                            break
                        else:
                            msg = f"{out_path} (Engraving overlay): {r.message}"
                            self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)
                    except Exception as e:
                        QCoreApplication.processEvents()
                        msg = f"{out_path} (Engraving overlay): {e}"
                        self._blog(f"❌  [misc]  {msg}"); errors.append(msg); failed_paths.add(out_path)

            finally:
                _restore_vis(vis_state)

        return errors, failed_paths
    # Export AO, Curvature, Text Overlay, and Engraving Overlay into Misc folders end

    _export_mesh_maps = _export_misc_maps

    # Run Misc maps export standalone with floating progress and cancel support start
    def _export_misc_maps_standalone(self):
        if self._batch_running:
            QMessageBox.warning(self, "Busy", "A batch export is already running."); return
        if not substance_painter.project.is_open():
            QMessageBox.warning(self, "No Project", "Open a project first."); return

        sel_ts = self._selected_ts()
        if not sel_ts:
            QMessageBox.warning(self, "No Texture Sets", "Select at least one Texture Set."); return

        buckets = self._ts_groups_by_path(sel_ts)
        if "" in buckets:
            missing = ", ".join(ts.name for ts in buckets[""])
            QMessageBox.warning(self, "Missing Output Path",
                f"No output path set for: {missing}"); return

        sp_fmt  = self.FORMATS[self._b_fmt.currentText()]
        sibling = (self._mm_location.currentData() == "sibling")
        dirs    = [_resolve_misc_dir(p, sibling) for p in buckets]
        reply   = QMessageBox.question(
            self, "Export Misc Maps",
            f"Export Misc maps (AO, Curvature, Text Overlay, Engraving Overlay) to:\n"
            + "\n".join(f"  {d}" for d in dirs)
            + f"\n\nFormat: {sp_fmt.upper()}\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes: return

        self._b_log.append("─── Misc export (AO, Curvature, Overlays) ───")
        total_steps = max(1, self._count_misc_steps(buckets))
        step_idx    = [0]
        self._batch_running    = True
        self._cancel_requested = False
        self._b_run_btn.setEnabled(False)
        self._b_cancel_btn.setEnabled(True)
        self._b_cancel_btn_top.setEnabled(True)
        self._b_progress.setMaximum(total_steps)
        self._b_progress.setValue(0)
        self._prog_dlg = _BatchProgressDialog("Misc Export Progress", total_steps, self._request_cancel, self)
        self._prog_dlg.show()
        QCoreApplication.processEvents()

        def _misc_tick(lbl):
            step_idx[0] += 1
            self._tick(step_idx[0], total_steps, lbl)

        errors, failed_paths = [], set()
        try:
            errors, failed_paths = self._export_misc_maps(buckets, sp_fmt, tick_cb=_misc_tick)
            if not self._cancel_requested:
                self._tick(total_steps, total_steps, "Done")
        finally:
            self._batch_running = False
            self._b_run_btn.setEnabled(True)
            self._b_cancel_btn.setEnabled(False)
            self._b_cancel_btn_top.setEnabled(False)
            if self._prog_dlg is not None:
                self._prog_dlg.mark_finished()
                self._prog_dlg = None

        if self._cancel_requested:
            self._b_current.setText("Misc export cancelled.")
            QMessageBox.information(self, "Misc Export Cancelled", "Misc export was cancelled by user.")
        elif errors:
            ok_paths = max(0, len(buckets) - len(failed_paths))
            QMessageBox.warning(self, "Misc Export — Errors",
                f"{ok_paths}/{len(buckets)} paths succeeded.\n"
                f"Errors:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Misc Export Complete",
                f"Misc maps exported to {len(buckets)} location(s):\n" + "\n".join(dirs))
    # Run Misc maps export standalone with floating progress and cancel support end

    _export_mesh_maps_standalone = _export_misc_maps_standalone

    # Execute the full multi-TS skin pack batch export and optional Misc export start
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

        raw_buckets = self._ts_groups_by_path(sel_ts)
        if "" in raw_buckets:
            missing = ", ".join(ts.name for ts in raw_buckets[""])
            QMessageBox.warning(self, "Missing Output Path",
                f"No output path set for: {missing}\n\n"
                "Set a Default Output Folder or a per-TS path for each selected Texture Set."
            ); return

        enabled_cats = self._enabled_cat_types()
        if not enabled_cats:
            QMessageBox.warning(self, "No Categories", "Select at least one category (Normal / Worn / Bright)."); return

        sp_fmt      = self.FORMATS[self._b_fmt.currentText()]
        worn_sfx    = self._b_worn_suffix.text()
        primary_ctx = self._ctx_nodes()

        # Build per-stack visibility data and filter out TSes missing skin pack structure
        stack_infos    = []
        missing_struct = []
        for ts in sel_ts:
            try:
                st = ts.get_stack()
                r  = find_skin_pack_root(st)
                if r is None:
                    missing_struct.append(ts.name); continue
                j_all, skins, cats = build_export_jobs(r)
                j_filt = [jb for jb in j_all if jb["cat_type"] in enabled_cats]
                ctx_st = self._resolve_ctx_for_stack(st, r, primary_ctx)

                # Index jobs by (cat_type, lowercase filename) and per-category order
                by_key = {}
                by_cat = defaultdict(list)
                for jb in j_filt:
                    by_key[(jb["cat_type"], jb["filename"].lower())] = jb
                    by_cat[jb["cat_type"]].append(jb)

                stack_infos.append({
                    "ts": ts, "root": r, "jobs": j_filt,
                    "by_key": by_key, "by_cat": by_cat,
                    "all_skins": skins, "all_cats": cats, "ctx": ctx_st,
                })
            except Exception as e:
                self._sp_log(f"Stack setup for {ts.name}: {e}", warn=True)
                missing_struct.append(ts.name)

        if missing_struct:
            QMessageBox.warning(self, "Missing Structure",
                f"Skin pack structure not found in:\n{', '.join(missing_struct)}\n\n"
                "Those Texture Sets will be skipped.")
        if not stack_infos:
            QMessageBox.critical(self, "No Valid Texture Sets",
                "No selected Texture Set has a recognizable skin pack structure."); return

        # Rebuild active TS list and path buckets using ONLY valid Texture Sets
        valid_ts = [si["ts"] for si in stack_infos]
        buckets  = self._ts_groups_by_path(valid_ts)
        stack_infos_by_ts = {si["ts"].name: si for si in stack_infos}

        # Use the first valid stack's filtered job list as the canonical skin order
        jobs = stack_infos[0]["jobs"]
        if not jobs:
            QMessageBox.warning(self, "No Jobs",
                "No skins match the selected categories."); return

        # Precompute per-category ordinal index for safe positional fallback
        cat_ordinals = []
        cat_seen_cnt = defaultdict(int)
        for jb in jobs:
            ct = jb["cat_type"]
            cat_ordinals.append(cat_seen_cnt[ct])
            cat_seen_cnt[ct] += 1
        primary_cat_totals = {ct: len(stack_infos[0]["by_cat"][ct]) for ct in ALL_CAT_TYPES}

        def _match_job_in_stack(si: dict, job: dict, cat_ord: int):
            key = (job["cat_type"], job["filename"].lower())
            if key in si["by_key"]:
                return si["by_key"][key]
            # Positional fallback only when both stacks have identical count in this category
            ct = job["cat_type"]
            si_cat_jobs = si["by_cat"][ct]
            if len(si_cat_jobs) == primary_cat_totals.get(ct, -1) and cat_ord < len(si_cat_jobs):
                return si_cat_jobs[cat_ord]
            return None

        # Confirmation dialog
        cat_str  = ", ".join(c.capitalize() for c in sorted(enabled_cats))
        ts_str   = ", ".join(ts.name for ts in valid_ts)
        ch_str   = ", ".join(CHANNEL_DEFS[k]["label"] for k in channels)
        path_str = "\n".join(f"  {p}  ({', '.join(t.name for t in tg)})"
                             for p, tg in buckets.items())
        n_files  = len(jobs) * sum(len(tg) for tg in buckets.values()) * len(channels)
        reply = QMessageBox.question(
            self, "Run Batch Export",
            f"Export {len(jobs)} skins\n\n"
            f"Categories   : {cat_str}\n"
            f"Texture Sets : {ts_str}\n"
            f"Channels     : {ch_str}\n"
            f"Format       : {sp_fmt.upper()}\n"
            f"Output paths :\n{path_str}\n\n"
            f"Total files  : {n_files}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes: return

        run_misc    = self._mm_enabled.isChecked()
        misc_ticks  = self._count_misc_steps(buckets, stack_infos_by_ts) if run_misc else 0
        total_ticks = len(jobs) * len(buckets) + misc_ticks
        self._batch_running    = True
        self._cancel_requested = False
        self._b_run_btn.setEnabled(False)
        self._b_cancel_btn.setEnabled(True)
        self._b_cancel_btn_top.setEnabled(True)
        self._b_progress.setMaximum(total_ticks)
        self._b_progress.setValue(0)
        self._b_log.clear()
        self._prog_dlg = _BatchProgressDialog("Batch Export Progress", total_ticks, self._request_cancel, self)
        self._prog_dlg.show()
        QCoreApplication.processEvents()

        # Snapshot visibility for every node (including ancestors of nested ctx nodes) across all stacks
        all_vis_nodes = []
        for si in stack_infos:
            c = si["ctx"]
            all_vis_nodes += _collect_with_ancestors(si["root"]) + si["all_cats"] + si["all_skins"]
            for base_node in (c["fill"], c["bp_group"]):
                if base_node is not None:
                    all_vis_nodes += _collect_with_ancestors(base_node)
            if c["worn_paint"] is not None and _is_proper_layer(c["worn_paint"]):
                all_vis_nodes += _collect_with_ancestors(c["worn_paint"])
            all_vis_nodes += [n for n in [c["invert"], c["worn_paint"],
                                           c["bp_nonworn"], c["bp_worn"]]
                              if n is not None]
        vis_state   = _save_vis(all_vis_nodes)
        errors      = []
        mm_errors   = []
        exported_ok = 0
        tick_idx    = 0
        ES          = substance_painter.export.ExportStatus

        try:
            try:
                # Initialise every stack: root+cats+ctx ancestors visible, all skins hidden
                for si in stack_infos:
                    _ensure_ancestors_visible(si["root"])
                    for n in si["all_cats"]: n.set_visible(True)
                    c = si["ctx"]
                    if c["fill"]:     _ensure_ancestors_visible(c["fill"])
                    if c["bp_group"]: _ensure_ancestors_visible(c["bp_group"])
                    for n in si["all_skins"]: n.set_visible(False)

                for idx, job in enumerate(jobs):
                    if self._cancel_requested:
                        self._blog("⚠ Cancelled by user."); break

                    name = job["filename"]
                    if job["cat_type"] == "worn" and worn_sfx and "worn" not in name.lower():
                        name = name + worn_sfx

                    cat_ord = cat_ordinals[idx]
                    matched_jobs = {}
                    for si in stack_infos:
                        self._apply_ctx(job["cat_type"], si["ctx"])
                        si_job = _match_job_in_stack(si, job, cat_ord)
                        matched_jobs[si["ts"].name] = si_job
                        if si_job:
                            for n in si_job["show"]: n.set_visible(True)

                    skin_failed = False
                    for out_path, ts_group in buckets.items():
                        if self._cancel_requested: break

                        tick_idx += 1
                        self._tick(tick_idx, total_ticks, f"[{job['cat_type'].capitalize()}] {name}")

                        active_group = [ts for ts in ts_group if matched_jobs.get(ts.name) is not None]
                        if not active_group:
                            self._blog(f"⏭  [{job['cat_type']:6}]  {name}  → skipped for {out_path} (skin not in stack)")
                            continue

                        ts_root_paths = [_ts_root_path(ts) for ts in active_group]
                        multi_ts      = len(ts_group) > 1
                        config = build_export_config(
                            ts_root_paths    = ts_root_paths,
                            out_dir          = out_path,
                            skin_name        = name,
                            sp_format        = sp_fmt,
                            enabled_channels = channels,
                            multi_ts         = multi_ts,
                            use_udim         = _any_uv_tiles(active_group),
                        )
                        try:
                            r = substance_painter.export.export_project_textures(config)
                            QCoreApplication.processEvents()
                            if r.status in (ES.Success, ES.Warning):
                                n_files_written = sum(len(v) for v in r.textures.values())
                                suffix = f" ⚠ {r.message}" if r.status == ES.Warning else ""
                                self._blog(f"✅  [{job['cat_type']:6}]  {name}  → {out_path}  ({n_files_written} files){suffix}")
                            elif r.status == ES.Cancelled:
                                self._cancel_requested = True
                                self._blog(f"⚠  [{job['cat_type']:6}]  {name}  — Cancelled by user.")
                                break
                            else:
                                self._blog(f"❌  [{job['cat_type']:6}]  {name}  → {out_path}: {r.message}")
                                errors.append(name)
                                skin_failed = True
                        except Exception as e:
                            QCoreApplication.processEvents()
                            self._blog(f"❌  [{job['cat_type']:6}]  {name}  → {out_path}: {e}")
                            errors.append(name)
                            skin_failed = True

                    for si in stack_infos:
                        si_job = matched_jobs.get(si["ts"].name)
                        if si_job:
                            for n in si_job["show"]: n.set_visible(False)

                    if not self._cancel_requested and not skin_failed:
                        exported_ok += 1

                    if self._cancel_requested:
                        break
            finally:
                _restore_vis(vis_state)

            # Auto-run Misc export if enabled and batch wasn't cancelled
            if run_misc and not self._cancel_requested:
                self._b_log.append("─── Misc export (AO, Curvature, Overlays) ───")

                def _batch_misc_tick(lbl):
                    nonlocal tick_idx
                    tick_idx += 1
                    self._tick(tick_idx, total_ticks, lbl)

                mm_errors, _ = self._export_misc_maps(buckets, sp_fmt, stack_infos_by_ts, tick_cb=_batch_misc_tick)

            if not self._cancel_requested:
                self._tick(total_ticks, total_ticks, "Done")

        finally:
            self._batch_running = False
            self._b_run_btn.setEnabled(True)
            self._b_cancel_btn.setEnabled(False)
            self._b_cancel_btn_top.setEnabled(False)
            if self._prog_dlg is not None:
                self._prog_dlg.mark_finished()
                self._prog_dlg = None
            self._save_config()

        n_skins   = len(jobs)
        cancelled = self._cancel_requested
        status    = f"{'Cancelled — ' if cancelled else 'Complete — '}{exported_ok}/{n_skins} skins exported"
        if errors: status += f", {len(set(errors))} failed"
        self._b_current.setText(status)

        if cancelled:
            QMessageBox.information(self, "Batch Export Cancelled",
                                    f"Batch export was cancelled by user.\n"
                                    f"{exported_ok}/{n_skins} skins exported before stopping.")
        elif errors:
            QMessageBox.warning(self, "Batch Complete with Errors",
                                f"{exported_ok}/{n_skins} skins exported.\n"
                                f"Failed: {', '.join(dict.fromkeys(errors))}"
                                + (f"\n\nMisc export errors:\n" + "\n".join(mm_errors) if mm_errors else ""))
        elif mm_errors:
            QMessageBox.warning(self, "Batch Complete — Misc Export Errors",
                                f"All {exported_ok} skins exported.\n\n"
                                f"Misc export failed:\n" + "\n".join(mm_errors))
        else:
            mm_note = "\nMisc maps (AO, Curvature, Overlays) also exported." if run_misc else ""
            QMessageBox.information(self, "Batch Complete",
                                    f"All {exported_ok} skins exported successfully.{mm_note}")
    # Execute the full multi-TS skin pack batch export and optional Misc export end

    # Save configuration and hide window on close start
    def closeEvent(self, event):
        self._save_config(); event.ignore(); self.hide()
    # Save configuration and hide window on close end


# Register plugin window and Window menu action on Substance Painter startup start
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
# Register plugin window and Window menu action on Substance Painter startup end


# Clean up UI elements when Substance Painter unloads the plugin start
def close_plugin():
    global _window
    for w in _plugin_widgets: substance_painter.ui.delete_ui_element(w)
    _plugin_widgets.clear()
    if _window: _window.deleteLater(); _window = None
    substance_painter.logging.log(
        substance_painter.logging.INFO, "TextureExporter", "Unloaded."
    )
# Clean up UI elements when Substance Painter unloads the plugin end


# Show the Texture Exporter window without resetting widgets during an active export start
def _show_window():
    if _window:
        _window.show(); _window.raise_(); _window.activateWindow()
        if not _window._batch_running:
            _window._refresh_ts_list()
            _window._apply_saved_ts_data()
            _window._refresh_ctx_pickers()
# Show the Texture Exporter window without resetting widgets during an active export end


if __name__ == "__main__":
    start_plugin()
