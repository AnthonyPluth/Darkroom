def read(body):
    # ruleid: darkroom-validate-bodies
    count = int(body["count"])
    # ruleid: darkroom-validate-bodies
    flag = bool(body.get("flag"))
    return count, flag


def read_validated(body, validate):
    # ok: darkroom-validate-bodies
    return validate.whole_number(body, "count")
