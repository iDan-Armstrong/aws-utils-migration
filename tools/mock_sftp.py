"""A read-only SFTP server over a local folder, for tests and demos (paramiko).

    python tools/mock_sftp.py ./folder --port 2222 --user field --password-env MOCK_SFTP_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
from pathlib import Path

import paramiko
from paramiko import ServerInterface, SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface


class _Server(ServerInterface):
    def __init__(self, user, password):
        self.user, self.password = user, password

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == (self.user, self.password) else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


def make_sftp(root: Path):
    class _SFTP(SFTPServerInterface):
        def _real(self, path):
            p = (root / path.lstrip("/")).resolve()
            if root.resolve() not in (p, *p.parents):
                raise PermissionError(path)
            return p

        def list_folder(self, path):
            p = self._real(path)
            out = []
            for f in sorted(p.iterdir()):
                a = SFTPAttributes.from_stat(f.stat())
                a.filename = f.name
                out.append(a)
            return out

        def stat(self, path):
            return SFTPAttributes.from_stat(self._real(path).stat())

        lstat = stat

        def open(self, path, flags, attr):
            if flags & (os.O_WRONLY | os.O_RDWR):
                return paramiko.SFTP_PERMISSION_DENIED
            h = _Handle(flags)
            h.readfile = open(self._real(path), "rb")
            h.filename = path
            return h
    return _SFTP


def serve(root, port=2222, user="field", password="field-pass"):
    root = Path(root)
    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(16)

    def accept():
        while True:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            t = paramiko.Transport(client)
            t.add_server_key(host_key)
            t.set_subsystem_handler("sftp", SFTPServer, make_sftp(root))
            t.start_server(server=_Server(user, password))

    threading.Thread(target=accept, daemon=True).start()
    return sock, host_key


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--port", type=int, default=2222)
    ap.add_argument("--user", default="field")
    ap.add_argument("--password-env", default="MOCK_SFTP_PASSWORD")
    a = ap.parse_args()
    serve(a.root, a.port, a.user, os.environ.get(a.password_env, "field-pass"))
    print(f"sftp://{a.user}@127.0.0.1:{a.port}")
    threading.Event().wait()
