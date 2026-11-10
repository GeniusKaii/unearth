"""Minimal read-only ext4 parser, ported from photo-digger.

Browse an Android ``/data`` partition image without mounting it and without
installing anything - list directories, walk a tree, export a file.  The
typical use is digging through emulator / handset images for chat databases
(``EnMicroMsg.db``, ``SQLite format 3`` ...).

Supports extent trees and classic direct blocks; when the image itself has
holes (a recovery that only got part of the inode table) it does best effort:
high block groups may raise, low ones still read.
"""

from __future__ import annotations

import struct


class Ext4Error(Exception):
    pass


class Ext4:
    def __init__(self, path):
        self.f = open(path, "rb")
        sb = self.read_at(1024, 1024)
        if len(sb) < 1024 or sb[56:58] != b"\x53\xef":
            raise Ext4Error("不是 ext2/3/4 文件系统（超级块魔数不匹配）")
        self.inodes_count = struct.unpack("<I", sb[0x00:0x04])[0]
        self.blocks_count = struct.unpack("<I", sb[0x04:0x08])[0]
        self.first_data_block = struct.unpack("<I", sb[0x14:0x18])[0]
        log_bs = struct.unpack("<I", sb[0x18:0x1C])[0]
        self.bs = 1024 << log_bs
        self.blocks_per_group = struct.unpack("<I", sb[0x20:0x24])[0]
        self.inodes_per_group = struct.unpack("<I", sb[0x28:0x2C])[0]
        self.inode_size = struct.unpack("<H", sb[0x58:0x5A])[0]
        self.feat_incompat = struct.unpack("<I", sb[0x60:0x64])[0]
        self.desc_size = struct.unpack("<H", sb[0xFE:0x100])[0] or 32
        self.is64 = bool(self.feat_incompat & 0x80)
        self.extents = bool(self.feat_incompat & 0x40)
        self.gdt_offset = (self.first_data_block + 1) * self.bs

    # -- raw access -----------------------------------------------------------
    def read_at(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- structure ------------------------------------------------------------
    def group_desc(self, g):
        """Return the inode-table start block of block group ``g``."""
        off = self.gdt_offset + g * self.desc_size
        d = self.read_at(off, 64)
        if len(d) < 12:
            raise Ext4Error("block group %d descriptor unreadable" % g)
        blk = struct.unpack("<I", d[0x08:0x0C])[0]
        if self.is64 and self.desc_size >= 64 and len(d) >= 0x2C:
            blk |= struct.unpack("<I", d[0x28:0x2C])[0] << 32
        return blk

    def inode(self, ino):
        g = (ino - 1) // self.inodes_per_group
        idx = (ino - 1) % self.inodes_per_group
        table = self.group_desc(g)
        off = table * self.bs + idx * self.inode_size
        d = self.read_at(off, self.inode_size)
        if len(d) < 0x64:
            raise Ext4Error("inode %d unreadable" % ino)
        mode = struct.unpack("<H", d[0x00:0x02])[0]
        size = struct.unpack("<I", d[0x04:0x08])[0]
        if self.inode_size > 0x6C and len(d) >= 0x70:
            size |= struct.unpack("<I", d[0x6C:0x70])[0] << 32
        flags = struct.unpack("<I", d[0x20:0x24])[0]
        return {"mode": mode, "size": size, "flags": flags,
                "iblock": d[0x28:0x28 + 60]}

    def _extents(self, node, depth_limit=6):
        """Walk the extent tree, yielding (logical block, physical block, len)."""
        if len(node) < 12:
            return
        magic, entries, _maxe, depth = struct.unpack("<HHHH", node[0:8])
        if magic != 0xF30A:
            return
        pos = 12
        for _ in range(entries):
            if pos + 12 > len(node):
                break
            if depth == 0:
                ee_block, ee_len, hi, lo = struct.unpack("<IHHI", node[pos:pos + 12])
                yield ee_block, (hi << 32) | lo, ee_len
            else:
                if depth_limit <= 0:
                    return
                _ei_block, leaf_lo, leaf_hi = struct.unpack("<IIH", node[pos:pos + 10])
                child = (leaf_hi << 32) | leaf_lo
                data = self.read_at(child * self.bs, self.bs)
                yield from self._extents(data, depth_limit - 1)
            pos += 12

    # -- file access ------------------------------------------------------------
    def read_file(self, ino, limit=None):
        """Read file content by inode number; ``limit`` caps the bytes read."""
        info = self.inode(ino)
        size = info["size"] if limit is None else min(info["size"], limit)
        if not self.extents:
            # classic direct blocks (ext2 / very old ext4)
            out = bytearray()
            ptrs = struct.unpack("<15I", info["iblock"])
            for p in ptrs[:12]:
                if len(out) >= size:
                    break
                if p:
                    out += self.read_at(p * self.bs, self.bs)
            return bytes(out[:size])
        out = bytearray()
        for _lb, pb, ln in self._extents(info["iblock"]):
            if ln > 32768:  # unwritten-extent flag
                ln -= 32768
            if len(out) >= size:
                break
            n = min(ln * self.bs, size - len(out))
            out += self.read_at(pb * self.bs, n)
        return bytes(out[:size])

    def export_file(self, ino, dest_path, chunk=4 * 1024 * 1024):
        """Stream a file to ``dest_path`` without holding it in memory."""
        info = self.inode(ino)
        size = info["size"]
        written = 0
        with open(dest_path, "wb") as fh:
            if not self.extents:
                data = self.read_file(ino, limit=chunk)
                while data and written < size:
                    fh.write(data[: size - written])
                    written += len(data)
                    if written < size:
                        data = self.read_file(ino, limit=chunk)
                return written, written >= size
            for _lb, pb, ln in self._extents(info["iblock"]):
                if ln > 32768:
                    ln -= 32768
                if written >= size:
                    break
                take = min(ln * self.bs, size - written)
                pos = 0
                while pos < take:
                    block = self.read_at(pb * self.bs + pos, min(chunk, take - pos))
                    if not block:
                        return written, False
                    fh.write(block)
                    written += len(block)
                    pos += len(block)
        return written, written >= size

    # -- directory access -------------------------------------------------------
    def listdir(self, ino):
        """[(name, inode, filetype)], filetype 2 = directory."""
        data = self.read_file(ino, limit=8 * 1024 * 1024)
        out = []
        pos = 0
        while pos + 8 <= len(data):
            e_ino, rec_len, name_len, ftype = struct.unpack("<IHBB", data[pos:pos + 8])
            if rec_len == 0:
                break
            name = data[pos + 8:pos + 8 + name_len]
            if e_ino and name not in (b".", b".."):
                out.append((name.decode("utf-8", "replace"), e_ino, ftype))
            pos += rec_len
        return out

    def lookup(self, path):
        """Resolve an absolute path to an inode number, or None."""
        ino = 2
        for part in [p for p in path.split("/") if p]:
            found = None
            for name, i, _t in self.listdir(ino):
                if name == part:
                    found = i
                    break
            if not found:
                return None
            ino = found
        return ino

    def walk(self, path="/", maxdepth=10, _depth=0):
        """Yield (path, inode, filetype) recursively."""
        ino = self.lookup(path)
        if ino is None:
            return
        for name, i, t in self.listdir(ino):
            p = path.rstrip("/") + "/" + name
            yield p, i, t
            if t == 2 and _depth < maxdepth:
                yield from self.walk(p, maxdepth, _depth + 1)
