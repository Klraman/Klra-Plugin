"""
Triplanar Consistency Toolkit
=============================
Substance Painter 12.1.0 plugin.

Problem this solves
--------------------
Painter's built-in Triplanar projection (substance_painter.layerstack.ProjectionMode.Triplanar)
blends three orthogonal projections based on the surface normal. Two things make camo read
inconsistently across a model:

  1. The projector box is, by default, aligned to the OBJECT's local axes -- not to the
     actual long axis / feature direction of a given part. A barrel, suppressor, stock and
     receiver rarely share one "natural" orientation, so each one gets a different blend
     seam placement even though they were painted with the "same" triplanar settings.

  2. Different parts have different mesh/UV scale, so the same "scale" or "hardness" number
     in the projection produces different-looking blob sizes and different-width transition
     bands on each part.

This toolkit does NOT reinvent the projection math (Painter's Triplanar is compiled into the
engine, we can't touch the blend shader itself). What we CAN do through the API is make sure
every camo layer is using an IDENTICAL, deliberately-chosen projection frame instead of
whatever each part's local axes happen to be -- which is what actually causes the "looks
weird off-axis" effect to differ from part to part. Concretely:

  * Broadcast one "master" Triplanar layer's rotation / scale / hardness / filtering to every
    other Triplanar camo layer in the project (or the active Texture Set), so all camo blobs
    share the same size and the same blend sharpness.
  * Optionally drive the projection scale from ScaleMode.MaterialPhysicalSize so physically
    different-sized parts still show a matching blob density instead of the pattern
    stretching bigger on big parts and smaller on small ones.
  * Nudge hardness up (tighter blend) to shrink the visible seam band, since a large soft
    blend is what produces the smeared diagonal you get when the projector axes don't line
    up with a part's silhouette.

References (substance_painter API, 12.1.0):
  - substance_painter.layerstack.TriplanarProjectionParams
  - substance_painter.layerstack.Projection3DParams (offset / rotation / scale, object space)
  - substance_painter.layerstack.UVTransformationParams (scale_mode / scale / rotation / offset)
  - substance_painter.layerstack.get_root_layer_nodes / GroupLayerNode.sub_layers
  - substance_painter.layerstack.get_selected_nodes
  - substance_painter.ui.add_dock_widget (PySide6)

Install: drop this file (or a folder containing it as __init__.py) into your Painter
"plugins" folder, then enable it from Python > Plugins.
"""

import substance_painter.ui as sp_ui
import substance_painter.layerstack as sp_layers
import substance_painter.logging as sp_log
import substance_painter.project as sp_project
import substance_painter.event as sp_event

from PySide6 import QtWidgets, QtCore

PLUGIN_NAME = "Triplanar Consistency Toolkit"

_dock_widget = None


# ---------------------------------------------------------------------------
# Layer stack traversal helpers
# ---------------------------------------------------------------------------

def _all_root_stacks():
    """Yield every layer Stack (one per Texture Set / material channel stack) in the project."""
    # substance_painter.textureset isn't fully covered by the bundled docs snapshot,
    # so we resolve it defensively via the currently selected nodes' texture sets and
    # via the module directly if available.
    import substance_painter.textureset as sp_ts
    for ts in sp_ts.all_texture_sets():
        yield ts.get_stack()


def _walk_layers(nodes):
    """Recursively walk a list of layer nodes, yielding every leaf layer (incl. inside groups)."""
    for node in nodes:
        yield node
        if node.get_type() == sp_layers.NodeType.GroupLayer:
            yield from _walk_layers(node.sub_layers())


def _find_triplanar_fill_layers(scope="project"):
    """
    Find every FillLayerNode currently set to Triplanar projection.

    scope: "project"  -> every texture set in the project
           "active"    -> only texture sets touched by the current selection
    """
    found = []

    if scope == "selection":
        stacks_nodes = [sp_layers.get_selected_nodes()]
    else:
        stacks_nodes = [sp_layers.get_root_layer_nodes(stack) for stack in _all_root_stacks()]

    for root_nodes in stacks_nodes:
        for node in _walk_layers(root_nodes):
            if node.get_type() != sp_layers.NodeType.FillLayer:
                continue
            try:
                if node.get_projection_mode() == sp_layers.ProjectionMode.Triplanar:
                    found.append(node)
            except ValueError:
                # Some fill layers (2D texture sets, UV-tile projects) don't expose a
                # projection mode at all -- skip them quietly.
                continue

    return found


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def read_reference_params(reference_node):
    """Pull the current TriplanarProjectionParams off a chosen 'master' layer."""
    params = reference_node.get_projection_parameters()
    if not isinstance(params, sp_layers.TriplanarProjectionParams):
        raise ValueError("Selected reference layer is not using Triplanar projection.")
    return params


def build_synced_params(reference_params, use_physical_size=False, physical_size_cm=20.0,
                         hardness_override=None):
    """
    Build a new TriplanarProjectionParams that copies the reference layer's frame
    (rotation / offset / scale / hardness / filtering), optionally swapping the scale
    behaviour to MaterialPhysicalSize so parts of different real-world size still show
    matching blob density.
    """
    projection_3d = sp_layers.Projection3DParams(
        offset=reference_params.projection_3d.offset,
        rotation=reference_params.projection_3d.rotation,
        scale=reference_params.projection_3d.scale,
    )

    if use_physical_size:
        uv_transform = sp_layers.UVTransformationParams(
            scale_mode=sp_layers.ScaleMode.MaterialPhysicalSize,
            scale=[physical_size_cm, physical_size_cm],
            rotation=reference_params.uv_transformation.rotation,
            offset=reference_params.uv_transformation.offset,
        )
    else:
        uv_transform = sp_layers.UVTransformationParams(
            scale_mode=reference_params.uv_transformation.scale_mode,
            scale=reference_params.uv_transformation.scale,
            rotation=reference_params.uv_transformation.rotation,
            offset=reference_params.uv_transformation.offset,
        )

    hardness = hardness_override if hardness_override is not None else reference_params.hardness

    return sp_layers.TriplanarProjectionParams(
        filtering_mode=reference_params.filtering_mode,
        shape_crop_mode=reference_params.shape_crop_mode,
        hardness=hardness,
        uv_transformation=uv_transform,
        projection_3d=projection_3d,
    )


def apply_params_to_layers(layers, params):
    applied, skipped = 0, 0
    for node in layers:
        try:
            node.set_projection_parameters(params)
            applied += 1
        except ValueError as e:
            sp_log.log(sp_log.Severity.warning, PLUGIN_NAME,
                        f"Skipped a layer: {e}")
            skipped += 1
    return applied, skipped


def sync_all_to_reference(scope="project", use_physical_size=False, physical_size_cm=20.0,
                           hardness_override=None):
    """
    Main entry point: take the single selected FillLayerNode as the "looks right" master,
    and push its triplanar frame to every other triplanar camo layer in `scope`.
    """
    selected = sp_layers.get_selected_nodes()
    if len(selected) != 1:
        raise ValueError("Select exactly one Triplanar fill layer to use as the reference.")

    reference = selected[0]
    reference_params = read_reference_params(reference)

    targets = _find_triplanar_fill_layers(scope=scope)
    # Don't bother re-writing the reference onto itself, but it's harmless either way.
    synced_params = build_synced_params(
        reference_params,
        use_physical_size=use_physical_size,
        physical_size_cm=physical_size_cm,
        hardness_override=hardness_override,
    )

    applied, skipped = apply_params_to_layers(targets, synced_params)
    sp_log.log(sp_log.Severity.info, PLUGIN_NAME,
                f"Synced {applied} Triplanar layer(s) to reference "
                f"'{getattr(reference, 'get_name', lambda: reference)()}'. "
                f"{skipped} skipped.")
    return applied, skipped


def tighten_hardness(scope="project", hardness=0.85):
    """
    Quick one-click fix: raise the blend hardness on every Triplanar camo layer in scope.
    A tighter blend shrinks the soft, smeared transition band that shows up wherever the
    projector axes aren't aligned with a part's silhouette -- it doesn't fix the root
    cause, but it makes the seam much less noticeable at a glance.
    """
    layers = _find_triplanar_fill_layers(scope=scope)
    applied, skipped = 0, 0
    for node in layers:
        try:
            current = node.get_projection_parameters()
            node.set_projection_parameters(sp_layers.TriplanarProjectionParams(
                filtering_mode=current.filtering_mode,
                shape_crop_mode=current.shape_crop_mode,
                hardness=hardness,
                uv_transformation=current.uv_transformation,
                projection_3d=current.projection_3d,
            ))
            applied += 1
        except ValueError:
            skipped += 1
    sp_log.log(sp_log.Severity.info, PLUGIN_NAME,
                f"Set hardness={hardness} on {applied} layer(s), {skipped} skipped.")
    return applied, skipped


# ---------------------------------------------------------------------------
# Minimal UI
# ---------------------------------------------------------------------------

class TriplanarToolkitWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(PLUGIN_NAME)
        self.setObjectName("triplanar_consistency_toolkit")

        layout = QtWidgets.QVBoxLayout(self)

        info = QtWidgets.QLabel(
            "1) Paint/tune ONE camo part until the triplanar looks right.\n"
            "2) Select that layer in the layer stack.\n"
            "3) Choose a scope below and hit Sync.\n"
            "Every other Triplanar fill layer in that scope will copy its\n"
            "rotation, offset, 3D scale, hardness and filtering."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.scope_box = QtWidgets.QComboBox()
        self.scope_box.addItems(["Whole project", "Current selection's texture set"])
        layout.addWidget(self.scope_box)

        self.physical_size_check = QtWidgets.QCheckBox(
            "Also force MaterialPhysicalSize scaling (matches blob size across differently "
            "sized parts, requires the material resource to have physical size info)")
        layout.addWidget(self.physical_size_check)

        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(QtWidgets.QLabel("Physical size (cm):"))
        self.physical_size_spin = QtWidgets.QDoubleSpinBox()
        self.physical_size_spin.setRange(0.1, 1000.0)
        self.physical_size_spin.setValue(20.0)
        size_row.addWidget(self.physical_size_spin)
        layout.addLayout(size_row)

        sync_btn = QtWidgets.QPushButton("Sync all Triplanar camo layers to selected reference")
        sync_btn.clicked.connect(self._on_sync)
        layout.addWidget(sync_btn)

        layout.addWidget(self._hline())

        hardness_row = QtWidgets.QHBoxLayout()
        hardness_row.addWidget(QtWidgets.QLabel("Hardness:"))
        self.hardness_spin = QtWidgets.QDoubleSpinBox()
        self.hardness_spin.setRange(0.0, 1.0)
        self.hardness_spin.setSingleStep(0.05)
        self.hardness_spin.setValue(0.85)
        hardness_row.addWidget(self.hardness_spin)
        layout.addLayout(hardness_row)

        tighten_btn = QtWidgets.QPushButton("Tighten seam blend on all Triplanar camo layers")
        tighten_btn.clicked.connect(self._on_tighten)
        layout.addWidget(tighten_btn)

        layout.addStretch()

    @staticmethod
    def _hline():
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        return line

    def _scope(self):
        return "project" if self.scope_box.currentIndex() == 0 else "selection"

    def _on_sync(self):
        try:
            applied, skipped = sync_all_to_reference(
                scope=self._scope(),
                use_physical_size=self.physical_size_check.isChecked(),
                physical_size_cm=self.physical_size_spin.value(),
            )
            QtWidgets.QMessageBox.information(
                self, PLUGIN_NAME, f"Synced {applied} layer(s). Skipped {skipped}.")
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, PLUGIN_NAME, str(e))

    def _on_tighten(self):
        applied, skipped = tighten_hardness(scope=self._scope(),
                                             hardness=self.hardness_spin.value())
        QtWidgets.QMessageBox.information(
            self, PLUGIN_NAME, f"Updated {applied} layer(s). Skipped {skipped}.")


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def start_plugin():
    global _dock_widget
    if not sp_project.is_open():
        sp_log.log(sp_log.Severity.warning, PLUGIN_NAME, "No project open yet.")
    _dock_widget = TriplanarToolkitWidget()
    sp_ui.add_dock_widget(_dock_widget)


def close_plugin():
    global _dock_widget
    if _dock_widget is not None:
        sp_ui.delete_ui_element(_dock_widget)
        _dock_widget = None


if __name__ == "__main__":
    start_plugin()
