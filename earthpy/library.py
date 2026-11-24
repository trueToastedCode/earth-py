"""
The Python tool's own caches, laid out like the webapp's library so the exported fill-tiles.sh
finds them (it looks for <project>/library/glb-v2):
  <cache>/tiles/<path>.raw      raw NodeData responses (unconverted, as the webapp stores them)
  <cache>/glb-v2/<path>.glb     converted tiles + <path>.json (centre, extent, texture width)
"""

import os
import threading

from .glb import GLB_VERSION


def write_atomic(path, data):
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


class Library:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.tiles_dir = os.path.join(self.root, "tiles")
        self.glb_dir = os.path.join(self.root, "glb-v%d" % GLB_VERSION)
        os.makedirs(self.tiles_dir, exist_ok=True)
        os.makedirs(self.glb_dir, exist_ok=True)

    def raw_file(self, p):
        return os.path.join(self.tiles_dir, p + ".raw")

    def has(self, p):
        return os.path.exists(self.raw_file(p))

    def read(self, p):
        try:
            with open(self.raw_file(p), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def add(self, p, raw):
        write_atomic(self.raw_file(p), raw)

    def glb_file(self, p):
        return os.path.join(self.glb_dir, p + ".glb")

    def meta_file(self, p):
        return os.path.join(self.glb_dir, p + ".json")

    def glb_cached(self, p):
        return os.path.exists(self.glb_file(p)) and os.path.exists(self.meta_file(p))
