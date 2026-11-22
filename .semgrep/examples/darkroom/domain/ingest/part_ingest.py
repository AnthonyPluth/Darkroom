from darkroom.domain.ingest import part_reader


def release_scratch(photos, person, export):
    # ok: darkroom-part-scratch-sweep-callers
    part_reader.sweep_part_scratch(photos, person, export)
