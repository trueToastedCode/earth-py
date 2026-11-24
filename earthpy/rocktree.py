"""
Google Earth RockTree lookups (port of re/exporter/lib/utils.js + convert-lat-long-to-octant.js
locate()): planetoid / bulk metadata (memory cache, parallel requests de-duplicated) and the
NodeData URL of a tile.
"""

import threading
from collections import OrderedDict
from concurrent.futures import Future

from . import bulk as B
from .jsfmt import js_num

URL_PREFIX = "https://kh.google.com/rt/earth/"
CACHE_MAX_ENTRIES = 4000


class RockTree:
    def __init__(self, limiter, is_cancelled=lambda: False):
        self.limiter = limiter
        self.is_cancelled = is_cancelled
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.inflight = {}
        self.root_epoch = None

    def fetch(self, url):
        return self.limiter.get(URL_PREFIX + url, self.is_cancelled)

    def _decode(self, url):
        with self.lock:
            if url in self.cache:
                self.cache.move_to_end(url)
                return self.cache[url]
            fut = self.inflight.get(url)
            owner = fut is None
            if owner:
                fut = self.inflight[url] = Future()
        if not owner:
            return fut.result()
        try:
            res = B.decode_bulk(self.fetch(url))
        except BaseException as e:
            with self.lock:
                del self.inflight[url]
            fut.set_exception(e)
            raise
        with self.lock:
            self.cache[url] = res
            while len(self.cache) > CACHE_MAX_ENTRIES:
                self.cache.popitem(last=False)
            del self.inflight[url]
        fut.set_result(res)
        return res

    def get_root_epoch(self):
        if self.root_epoch is None:
            p = self._decode("PlanetoidMetadata")
            self.root_epoch = p.bulkMetadataEpoch[0]
        return self.root_epoch

    def get_bulk(self, path, epoch):
        return self._decode("BulkMetadata/pb=!1m2!1s%s!2u%s" % (path, js_num(epoch)))

    def locate(self, node_path):
        """(bulk, index) of a node path, or None if the node does not exist."""
        epoch = self.get_root_epoch()
        blk = None
        index = -1
        i = 4
        while i < len(node_path) + 4:
            bulk_path = node_path[:i - 4]
            sub_path = node_path[:i]
            if blk is not None and B.has_bulk_metadata_at_index(blk, B.get_index_by_path(blk, bulk_path)):
                return None
            blk = self.get_bulk(bulk_path, epoch)
            index = B.get_index_by_path(blk, sub_path)
            if index is None:
                raise ValueError("unexpected bulk metadata for %s" % sub_path)
            if index < 0:
                return None
            epoch = B._at(blk.bulkMetadataEpoch, index)
            if epoch is None:
                raise ValueError("unexpected bulk metadata for %s" % sub_path)
            i += 4
        if index < 0 or not B.has_node_at_index(blk, index):
            return None
        return blk, index

    @staticmethod
    def node_url_part(path, blk, index):
        node_epoch = blk.epoch[index]
        img = blk.imageryEpochArray[index] if blk.imageryEpochArray is not None else blk.defaultImageryEpoch
        tex = blk.textureFormatArray[index] if blk.textureFormatArray is not None else blk.defaultTextureFormat
        img_part = "!3u%s" % js_num(img) if blk.flags[index] & 16 else ""
        return "!1m2!1s%s!2u%s!2e%s%s!4b0" % (path, js_num(node_epoch), js_num(tex), img_part)

    def get_node_raw(self, path, blk, index):
        """Raw (undecoded) NodeData bytes, as stored in the library."""
        return self.fetch("NodeData/pb=" + self.node_url_part(path, blk, index))
