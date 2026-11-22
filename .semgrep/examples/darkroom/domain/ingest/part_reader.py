from darkroom.storage import paths


def read_part(photos, person, export):
    # ok: darkroom-scratch-sweep-one-caller
    paths.sweep_scratch(photos, person, export)


def sweep_part_scratch(photos, person, export):
    # ok: darkroom-part-scratch-sweep-callers
    sweep_part_scratch(photos, person, export)
