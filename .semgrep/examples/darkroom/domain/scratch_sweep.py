from darkroom.storage import paths
from darkroom.storage.paths import sweep_scratch


def sweep_from_cleanup(photos, person, export):
    # ruleid: darkroom-scratch-sweep-one-caller
    paths.sweep_scratch(photos, person, export)


def sweep_by_name(photos, person, export):
    # ruleid: darkroom-scratch-sweep-one-caller
    sweep_scratch(photos, person, export)


def name_a_scratch_file(photos, person, export):
    # ok: darkroom-scratch-sweep-one-caller
    return paths.scratch_file(photos, person, export, "1.jpg")
