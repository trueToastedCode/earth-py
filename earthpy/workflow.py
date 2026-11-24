"""Reading a workflow export (*.workflow.json), with the same checks as webapp/lib/workflow.js."""

import json
import re

from .octant import box_of_path

TYPE = "earth-tile-workflow"
FORMAT_VERSION = 1
LEVELS = (2, 20)
PATH_RE = re.compile(r"^[0-7]{2,20}$")


class WorkflowError(Exception):
    pass


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise WorkflowError("Could not read %s: %s" % (path, e))
    return normalize(data)


def normalize(w):
    if not isinstance(w, dict):
        raise WorkflowError("A workflow must be a JSON object.")
    if "type" in w and w["type"] != TYPE:
        raise WorkflowError('Not a workflow file (type is "%s").' % w["type"])
    fv = w.get("formatVersion")
    if isinstance(fv, (int, float)) and not isinstance(fv, bool) and fv > FORMAT_VERSION:
        raise WorkflowError("This workflow was made by a newer version (format %s); update the app first." % fv)
    out = {}
    for key, limit in (("name", 200), ("version", 100), ("description", 20000)):
        if key in w:
            if not isinstance(w[key], str):
                raise WorkflowError('"%s" must be text.' % key)
            out[key] = w[key][:limit]
    out["name"] = (out.get("name") or "").strip() or "Untitled workflow"
    if "tiles" not in w:
        raise WorkflowError('The workflow has no "tiles" list.')
    if not isinstance(w["tiles"], list):
        raise WorkflowError('"tiles" must be a list of tile paths.')
    tiles, seen = [], set()
    for p in w["tiles"]:
        if not isinstance(p, str) or not PATH_RE.match(p) or not box_of_path(p):
            raise WorkflowError("Invalid tile path %s." % json.dumps(p))
        if p not in seen:
            seen.add(p)
            tiles.append(p)
    out["tiles"] = tiles  # file order, like [...new Set(tiles)] in the webapp
    out.setdefault("version", "")
    out.setdefault("description", "")
    return out
