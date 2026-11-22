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


def sweep_through_the_readers_wrapper(photos, person, export):
    from darkroom.domain.ingest import part_reader

    # ruleid: darkroom-part-scratch-sweep-callers
    part_reader.sweep_part_scratch(photos, person, export)


def hand_the_wrapper_on(run_later):
    from darkroom.domain.ingest import part_reader

    # ruleid: darkroom-part-scratch-sweep-callers
    run_later(part_reader.sweep_part_scratch)


def read_a_part(opened, photos, person, export, part, now):
    from darkroom.domain.ingest import part_reader

    # ok: darkroom-part-scratch-sweep-callers
    return part_reader.read_part(opened, photos, person, export, part, now)
