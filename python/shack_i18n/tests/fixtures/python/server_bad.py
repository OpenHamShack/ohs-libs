"""A small HTTP server with user sentences in its responses and errors."""


class LicenceError(Exception):
    pass


def handler(request, web):
    if not request.ok:
        return web.json_response({"error": "This device is held by another app.", "code": "device_held"}, status=409)  # expect-finding
    if request.gone:
        return {"error": "The pairing code has expired."}, 410  # expect-finding
    if request.bad:
        return {"error": "bad_request"}  # an identifier, not a sentence
    return web.json_response({"ok": True, "message": "fine"})


def clean_custom(value):
    if not value:
        raise ValueError("Type a licence name.")  # expect-finding
    if value == "x":
        raise LicenceError(f"{value} is not a licence you hold.")  # expect-finding
    raise KeyError("Not user-facing, so not checked.")
