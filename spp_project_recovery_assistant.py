"""SPP Project Recovery Assistant 0.1.0 — Substance 3D Painter 12.1+

This plugin is intentionally conservative.  It inventories the *currently open*
source project, creates a durable recovery job (manifest, log and optional baked
texture backup), compares source texture sets to a clean project, and creates a
new project from a revised mesh.

Important API limitation (Painter Python API 0.3.5): the documented API does
not expose Smart Material export, Smart Material import/application, or layer
stack serialization/copying between projects.  This plugin therefore never
claims to migrate editable layer stacks.  It makes the manual Smart Material
bridge auditable and much less error-prone, while automating only documented
operations.

Install: place this file in Painter's python/plugins folder, enable it under
Python > Plugins, then open Window > SPP Project Recovery Assistant.
"""

import datetime
import json
import os
import re
import traceback

import substance_painter.application as sp_application
import substance_painter.export as sp_export
import substance_painter.layerstack as sp_layerstack
import substance_painter.logging as sp_log
import substance_painter.project as sp_project
import substance_painter.resource as sp_resource
import substance_painter.textureset as sp_textureset
import substance_painter.ui as sp_ui

from PySide6 import QtCore, QtWidgets
from PySide6.QtGui import QAction


PLUGIN_NAME = "SPP Project Recovery Assistant"
PLUGIN_VERSION = "0.1.0"
_window = None
_ui_elements = []


def _safe_name(value):
    """Make an individual path segment safe without changing the source name."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unnamed").strip("._") or "unnamed"


def _json_default(value):
    return str(value)


class RecoveryJob:
    """Owns the on-disk, resumable records for one recovery attempt."""

    def __init__(self, root, source_path):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        stem = _safe_name(os.path.splitext(os.path.basename(source_path or "unsaved"))[0])
        self.path = os.path.join(root, "spp_rebuild", "%s_%s" % (stem, timestamp))
        self.source_dir = os.path.join(self.path, "source")
        self.exports_dir = os.path.join(self.path, "exported_textures")
        self.manifests_dir = os.path.join(self.path, "manifests")
        self.logs_dir = os.path.join(self.path, "logs")
        self.rebuilt_dir = os.path.join(self.path, "rebuilt")
        self.manifest_path = os.path.join(self.manifests_dir, "recovery_manifest.json")
        self.log_path = os.path.join(self.logs_dir, "spp_rebuild.log")

    def create(self):
        for directory in (self.path, self.source_dir, self.exports_dir,
                          self.manifests_dir, self.logs_dir, self.rebuilt_dir):
            os.makedirs(directory, exist_ok=True)

    def write_manifest(self, manifest):
        with open(self.manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False,
                      default=_json_default)

    def log(self, message):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (stamp, message)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        sp_log.log(sp_log.Severity.info, PLUGIN_NAME, message)
        return line


def _walk_nodes(nodes):
    for node in nodes:
        yield node
        if node.get_type() == sp_layerstack.NodeType.GroupLayer:
            for child in _walk_nodes(node.sub_layers()):
                yield child


def _node_snapshot(node):
    """Capture documented, read-only node details.  No layer is modified."""
    result = {
        "name": node.get_name(),
        "type": str(node.get_type()),
        "visible": node.is_visible(),
    }
    if node.get_type() == sp_layerstack.NodeType.GroupLayer:
        result["children"] = [_node_snapshot(child) for child in node.sub_layers()]
    return result


def _texture_set_snapshot(texture_set):
    stack = texture_set.get_stack()
    roots = sp_layerstack.get_root_layer_nodes(stack)
    nodes = list(_walk_nodes(roots))
    resolution = texture_set.get_resolution()
    return {
        "name": texture_set.name,
        "original_name": texture_set.original_name,
        "description": texture_set.description,
        "is_layered_material": texture_set.is_layered_material(),
        "resolution": str(resolution),
        "mesh_names": list(texture_set.all_mesh_names()),
        "root_layer_count": len(roots),
        "layer_count": len(nodes),
        "has_user_work": bool(nodes),
        "layers": [_node_snapshot(node) for node in roots],
        "export_status": "not_exported",
        "migration_status": "manual_smart_material_bridge_required",
    }


def build_manifest():
    """Analyze only the open project; Painter does not expose offline SPP reading."""
    if not sp_project.is_open():
        raise RuntimeError("Open the source .spp in Painter before analyzing it.")
    texture_sets = [_texture_set_snapshot(ts) for ts in sp_textureset.all_texture_sets()]
    try:
        resource_ids = [str(item) for item in sp_resource.list_project_resources()]
    except Exception as error:
        resource_ids = []
        resource_error = str(error)
    else:
        resource_error = None
    return {
        "schema_version": 1,
        "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_project": {
            "path": sp_project.file_path(),
            "name": sp_project.name(),
            "uuid": str(sp_project.get_uuid()),
            "last_saved_painter_version": sp_project.last_saved_substance_painter_version(),
            "last_imported_mesh": sp_project.last_imported_mesh_path(),
        },
        "painter_version": str(sp_application.version()),
        "texture_sets": texture_sets,
        "project_resource_ids": resource_ids,
        "resource_audit_error": resource_error,
        "capabilities": capabilities(),
        "limitations": [
            "Offline reading of a selected .spp is not documented; analysis requires it to be open.",
            "Smart Material export is not exposed by the documented Python API.",
            "Smart Material import/application is not exposed by the documented Python API.",
            "Layer-stack serialization/copying between projects is not exposed by the documented Python API.",
            "The optional texture backup is baked output, not editable material data.",
        ],
    }


def capabilities():
    """A deliberately explicit capability report: only documented APIs are true."""
    return {
        "enumerate_texture_sets": True,
        "enumerate_layer_nodes": True,
        "inspect_project_resources": True,
        "export_baked_textures": True,
        "create_clean_project": True,
        "open_project": True,
        "save_project": True,
        "smart_material_export": False,
        "smart_material_import_apply": False,
        "copy_layer_stack_between_projects": False,
        "offline_spp_analysis": False,
    }


def exact_mapping(source_sets, target_sets):
    """Exact, case-insensitive mapping only.  Near matches remain ambiguous."""
    target_by_key = {item.lower(): item for item in target_sets}
    mappings, missing = [], []
    used = set()
    for source in source_sets:
        target = target_by_key.get(source.lower())
        if target and target not in used:
            mappings.append({"source": source, "target": target, "status": "exact_match"})
            used.add(target)
        else:
            missing.append({"source": source, "target": None, "status": "missing_or_ambiguous"})
    new_sets = [item for item in target_sets if item not in used]
    return mappings + missing, new_sets


class RecoveryWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("spp_project_recovery_assistant")
        self.setWindowTitle(PLUGIN_NAME)
        self.manifest = None
        self.job = None
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        warning = QtWidgets.QLabel(
            "Safe recovery assistant — it never reloads the source mesh or overwrites the source .spp. "
            "Painter's API cannot export/apply Smart Materials or copy editable layer stacks, so those "
            "steps are clearly marked manual."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        source_box = QtWidgets.QGroupBox("1. Source project (must already be open)")
        source_layout = QtWidgets.QGridLayout(source_box)
        self.source_path = QtWidgets.QLineEdit()
        self.source_path.setReadOnly(True)
        source_layout.addWidget(QtWidgets.QLabel("Open source .spp:"), 0, 0)
        source_layout.addWidget(self.source_path, 0, 1)
        analyze = QtWidgets.QPushButton("Analyze open source / create recovery job")
        analyze.clicked.connect(self.analyze_source)
        source_layout.addWidget(analyze, 1, 0, 1, 2)
        layout.addWidget(source_box)

        self.set_table = QtWidgets.QTableWidget(0, 4)
        self.set_table.setHorizontalHeaderLabels(["Texture Set", "Layers", "Mesh names", "Status"])
        self.set_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.set_table)

        action_box = QtWidgets.QGroupBox("2. Backup and clean project")
        action_layout = QtWidgets.QGridLayout(action_box)
        self.dry_run = QtWidgets.QCheckBox("Dry run (write analysis only; do not export or create a project)")
        self.dry_run.setChecked(True)
        action_layout.addWidget(self.dry_run, 0, 0, 1, 3)
        export_backup = QtWidgets.QPushButton("Export baked PBR backup for selected sets")
        export_backup.clicked.connect(self.export_backup)
        action_layout.addWidget(export_backup, 1, 0, 1, 3)
        self.mesh_path = QtWidgets.QLineEdit()
        browse_mesh = QtWidgets.QPushButton("Browse revised mesh…")
        browse_mesh.clicked.connect(self.browse_mesh)
        action_layout.addWidget(QtWidgets.QLabel("Revised mesh:"), 2, 0)
        action_layout.addWidget(self.mesh_path, 2, 1)
        action_layout.addWidget(browse_mesh, 2, 2)
        create = QtWidgets.QPushButton("Create clean rebuilt project")
        create.clicked.connect(self.create_clean_project)
        action_layout.addWidget(create, 3, 0, 1, 3)
        layout.addWidget(action_box)

        mapping_box = QtWidgets.QGroupBox("3. Mapping after clean project is open")
        mapping_layout = QtWidgets.QVBoxLayout(mapping_box)
        compare = QtWidgets.QPushButton("Compare open clean project to source manifest")
        compare.clicked.connect(self.compare_projects)
        mapping_layout.addWidget(compare)
        self.mapping = QtWidgets.QTextEdit()
        self.mapping.setReadOnly(True)
        self.mapping.setMinimumHeight(120)
        mapping_layout.addWidget(self.mapping)
        layout.addWidget(mapping_box)

        self.log_view = QtWidgets.QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        layout.addWidget(self.log_view)

    def _write(self, message):
        self.log_view.append(message)
        if self.job:
            self.job.log(message)

    def _selected_manifest_sets(self):
        return [self.manifest["texture_sets"][row]
                for row in range(self.set_table.rowCount())
                if self.set_table.item(row, 0).checkState() == QtCore.Qt.Checked]

    def _populate_sets(self):
        entries = self.manifest["texture_sets"]
        self.set_table.setRowCount(len(entries))
        for row, item in enumerate(entries):
            name = QtWidgets.QTableWidgetItem(item["name"])
            name.setFlags(name.flags() | QtCore.Qt.ItemIsUserCheckable)
            name.setCheckState(QtCore.Qt.Checked if item["has_user_work"] else QtCore.Qt.Unchecked)
            self.set_table.setItem(row, 0, name)
            self.set_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(item["layer_count"])))
            self.set_table.setItem(row, 2, QtWidgets.QTableWidgetItem(", ".join(item["mesh_names"])))
            self.set_table.setItem(row, 3, QtWidgets.QTableWidgetItem(item["migration_status"]))

    def analyze_source(self):
        try:
            manifest = build_manifest()
            source_path = manifest["source_project"]["path"]
            if not source_path:
                raise RuntimeError("Save the source project once before creating a recovery job.")
            root = os.path.dirname(source_path)
            self.job = RecoveryJob(root, source_path)
            self.job.create()
            self.manifest = manifest
            self.job.write_manifest(self.manifest)
            self.source_path.setText(source_path)
            self._populate_sets()
            self._write("Analyzed %d texture sets. Job: %s" %
                        (len(manifest["texture_sets"]), self.job.path))
            self._write("Dependency audit found %d project resource IDs." %
                        len(manifest["project_resource_ids"]))
        except Exception as error:
            self._handle_error("Analysis failed", error)

    def browse_mesh(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select revised mesh", "", "Meshes (*.fbx *.obj *.gltf *.glb *.usd *.usda *.usdc);;All files (*)")
        if path:
            self.mesh_path.setText(path)

    def export_backup(self):
        if not self._require_job():
            return
        selected = self._selected_manifest_sets()
        if not selected:
            QtWidgets.QMessageBox.warning(self, PLUGIN_NAME, "Select at least one Texture Set.")
            return
        if self.dry_run.isChecked():
            self._write("DRY RUN: would export baked PBR backup for %d Texture Set(s)." % len(selected))
            return
        # This is the documented export API.  Baked maps are a safety copy only;
        # they are never presented as an editable Smart Material replacement.
        config = {
            "exportPath": self.job.exports_dir,
            "exportList": [{"rootPath": item["name"]} for item in selected],
            "exportPresets": [{
                "name": "Recovery PBR Backup",
                "maps": [
                    {"fileName": "$textureSet_BaseColor", "channels": [{"destChannel": "R", "srcChannel": "R", "srcMapType": "documentMap", "srcMapName": "baseColor"}]},
                    {"fileName": "$textureSet_Roughness", "channels": [{"destChannel": "R", "srcChannel": "R", "srcMapType": "documentMap", "srcMapName": "roughness"}]},
                    {"fileName": "$textureSet_Metallic", "channels": [{"destChannel": "R", "srcChannel": "R", "srcMapType": "documentMap", "srcMapName": "metallic"}]},
                    {"fileName": "$textureSet_Normal", "channels": [{"destChannel": "R", "srcChannel": "R", "srcMapType": "documentMap", "srcMapName": "normal"}]},
                ],
            }],
            "exportParameters": [{"parameters": {"fileFormat": "png", "bitDepth": "16", "dithering": False,
                                                       "paddingAlgorithm": "infinite", "sizeLog2": 11}}],
        }
        try:
            result = sp_export.export_project_textures(config)
            ok = result.status in (sp_export.ExportStatus.Success, sp_export.ExportStatus.Warning)
            for item in selected:
                item["export_status"] = "exported_baked_backup" if ok else "export_failed"
            self.manifest["baked_export"] = {"status": str(result.status), "message": result.message,
                                                "textures": result.textures}
            self.job.write_manifest(self.manifest)
            self._write("Baked backup %s: %s" % ("completed" if ok else "returned non-success", result.message))
        except Exception as error:
            self._handle_error("Baked backup export failed", error)

    def create_clean_project(self):
        if not self._require_job():
            return
        mesh = self.mesh_path.text().strip()
        if not mesh or not os.path.isfile(mesh):
            QtWidgets.QMessageBox.warning(self, PLUGIN_NAME, "Choose an existing revised mesh first.")
            return
        output = os.path.join(self.job.rebuilt_dir, _safe_name(os.path.splitext(os.path.basename(mesh))[0]) + "_REBUILT.spp")
        if self.dry_run.isChecked():
            self._write("DRY RUN: would create clean project: %s" % output)
            return
        reply = QtWidgets.QMessageBox.question(
            self, PLUGIN_NAME,
            "Painter will close the currently open source project to create a new project.\n\n"
            "The source will not be saved, reloaded, or overwritten by this plugin. Continue?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if reply != QtWidgets.QMessageBox.Yes:
            return
        try:
            settings = sp_project.Settings(default_save_path=output)
            sp_project.create(mesh, settings=settings)
            # create() is documented; save() ensures a separately named .spp exists.
            sp_project.save_as(output)
            self.manifest["rebuilt_project"] = {"mesh": mesh, "path": output, "created": True}
            self.job.write_manifest(self.manifest)
            self._write("Created clean project: %s" % output)
            self._write("Next: use Painter's UI to apply manually exported Smart Materials, then compare mapping here.")
        except Exception as error:
            self._handle_error("Clean project creation failed", error)

    def compare_projects(self):
        if not self._require_job():
            return
        if not sp_project.is_open():
            QtWidgets.QMessageBox.warning(self, PLUGIN_NAME, "Open the clean project first.")
            return
        source_names = [item["name"] for item in self._selected_manifest_sets()]
        target_names = [item.name for item in sp_textureset.all_texture_sets()]
        mappings, new_sets = exact_mapping(source_names, target_names)
        rows = ["OLD PROJECT -> NEW PROJECT -> ACTION"]
        for item in mappings:
            rows.append("%s -> %s -> %s" % (item["source"], item["target"] or "—", item["status"]))
        for name in new_sets:
            rows.append("— -> %s -> new_texture_set" % name)
        rows.append("\nOnly exact matches are automatic. Rename/ambiguous cases require manual confirmation.")
        self.mapping.setPlainText("\n".join(rows))
        self.manifest["mapping"] = {"target_project": sp_project.file_path(),
                                    "mappings": mappings, "new_texture_sets": new_sets}
        self.job.write_manifest(self.manifest)
        self._write("Compared %d source set(s) against %d new set(s)." % (len(source_names), len(target_names)))

    def _require_job(self):
        if self.job and self.manifest:
            return True
        QtWidgets.QMessageBox.warning(self, PLUGIN_NAME, "Analyze the open source project first.")
        return False

    def _handle_error(self, heading, error):
        details = "%s: %s" % (heading, error)
        self._write(details)
        if self.job:
            self.job.log(traceback.format_exc())
        QtWidgets.QMessageBox.critical(self, PLUGIN_NAME, details)


def _show_window():
    global _window
    if _window is None:
        _window = RecoveryWidget()
        sp_ui.add_dock_widget(_window)
    _window.show()
    _window.raise_()
    _window.activateWindow()


def start_plugin():
    action = QAction("SPP Project Recovery Assistant…", sp_ui.get_main_window())
    action.triggered.connect(_show_window)
    sp_ui.add_action(sp_ui.ApplicationMenu.Window, action)
    _ui_elements.append(action)
    sp_log.log(sp_log.Severity.info, PLUGIN_NAME, "Loaded version %s." % PLUGIN_VERSION)


def close_plugin():
    global _window
    for element in _ui_elements:
        sp_ui.delete_ui_element(element)
    _ui_elements.clear()
    if _window is not None:
        sp_ui.delete_ui_element(_window)
        _window = None


if __name__ == "__main__":
    start_plugin()
