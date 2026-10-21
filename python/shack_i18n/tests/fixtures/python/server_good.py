"""The same server done properly."""

from gettext import gettext as _


def handler(request, web):
    if not request.ok:
        return web.json_response({"error": _("This device is held by another app."), "code": "device_held"}, status=409)
    return {"error": _("The pairing code has expired.")}, 410


def clean_custom(value):
    if not value:
        raise ValueError(_("Type a licence name."))
