"""File shares and SFTP servers: folders of documents, scans, imagery and exports.

Each top-level folder becomes one item, so progress and verification are reported per
folder. Every file is hashed with SHA-256 as it streams, and the hash goes into the S3
object's metadata so it can be checked later without trusting the transfer.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

from ..model import Item


class FileSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.patterns = cfg.get("include") or ["*"]
        self.exclude = cfg.get("exclude") or ["~$*", "Thumbs.db", ".DS_Store", "*.tmp"]
        self.sftp = None
        if cfg["type"] == "sftp":
            import paramiko
            host, port = cfg["host"], int(cfg.get("port", 22))
            user = cfg.get("username") or os.environ.get(cfg.get("username_env", ""), "")
            password = os.environ.get(cfg.get("password_env", ""), None) if cfg.get("password_env") else None
            keyfile = cfg.get("key_file")
            self.transport = paramiko.Transport((host, port))
            pkey = paramiko.RSAKey.from_private_key_file(keyfile) if keyfile else None
            if cfg.get("host_key"):
                hk = paramiko.RSAKey(data=bytes.fromhex(cfg["host_key"]))
                self.transport.connect(hostkey=hk, username=user, password=password, pkey=pkey)
            else:
                self.transport.connect(username=user, password=password, pkey=pkey)
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
            self.root = PurePosixPath(cfg.get("path", "/"))
        else:
            self.root = Path(cfg["path"])

    def close(self):
        if self.sftp:
            self.sftp.close()
            self.transport.close()

    def _keep(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in self.patterns) and not any(fnmatch.fnmatch(name, p) for p in self.exclude)

    def _walk(self):
        """Yield (relative path, size, mtime)."""
        if self.sftp:
            stack = [self.root]
            while stack:
                d = stack.pop()
                for e in self.sftp.listdir_attr(str(d)):
                    p = d / e.filename
                    if stat.S_ISDIR(e.st_mode):
                        stack.append(p)
                    elif self._keep(e.filename):
                        yield str(p.relative_to(self.root)), e.st_size, e.st_mtime
        else:
            for p in sorted(self.root.rglob("*")):
                if p.is_file() and self._keep(p.name):
                    st = p.stat()
                    yield p.relative_to(self.root).as_posix(), st.st_size, int(st.st_mtime)

    def discover(self) -> list[Item]:
        groups: dict[str, list] = {}
        for rel, size, mtime in self._walk():
            top = rel.split("/")[0] if "/" in rel else "(root)"
            groups.setdefault(top, []).append({"path": rel, "size": size, "mtime": mtime})
        items = []
        for top, files in sorted(groups.items()):
            exts = {}
            for f in files:
                e = PurePosixPath(f["path"]).suffix.lower() or "(none)"
                exts[e] = exts.get(e, 0) + 1
            items.append(Item(source=self.name, name=top, kind="files", rows=len(files),
                              bytes=sum(f["size"] for f in files),
                              extra={"files": files, "extensions": exts, "largest": max(f["size"] for f in files)}))
        return items

    def open(self, rel: str):
        if self.sftp:
            return self.sftp.open(str(self.root / rel), "rb")
        return open(self.root / rel, "rb")

    def sha256(self, rel: str) -> str:
        h = hashlib.sha256()
        with self.open(rel) as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
