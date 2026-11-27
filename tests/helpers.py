import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests import stubs

ROOT = Path(__file__).resolve().parent.parent
VMFORAI = ROOT / "vmforai"

# Tools the remote scripts may use; the fake VM gets only these.
REMOTE_TOOLS = ["sh", "bash", "cat", "grep", "mkdir", "touch", "tail", "wc", "chmod",
                "awk", "rm", "basename", "id", "env", "head", "cp", "dirname", "ls",
                "sed", "tr", "uname", "sleep", "du", "cut", "date", "mv", "ln", "tar"]


def write_exe(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Sandbox:
    """A throwaway $HOME with fake ssh/virsh on PATH and a fake remote VM home."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmforai-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.log = self.tmp / "log.jsonl"
        self.log.touch()
        self.remote_home = self.tmp / "remote"
        self.remote_home.mkdir()
        self.remote_bin = self.tmp / "remote-bin"
        self.remote_bin.mkdir()
        self.remote_log = self.tmp / "remote.log"
        self.remote_log.touch()
        self.recorder = write_exe(self.tmp / "recorder", stubs.RECORDER)
        self.virsh_state = self.tmp / "virsh.state"
        write_exe(self.bin / "ssh", stubs.SSH)
        write_exe(self.bin / "virsh", stubs.VIRSH)
        write_exe(self.bin / "scp", stubs.SCP)
        for tool in REMOTE_TOOLS:
            src = shutil.which(tool)
            if src:
                (self.remote_bin / tool).symlink_to(src)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "STUB_LOG": str(self.log),
            "STUB_VIRSH_STATE": str(self.virsh_state),
            "STUB_REMOTE_HOME": str(self.remote_home),
            "STUB_REMOTE_PATH": str(self.remote_bin),
            "STUB_REMOTE_LOG": str(self.remote_log),
            "STUB_RECORDER": str(self.recorder),
            "VMFORAI_BOOT_TIMEOUT": "10",
            "VMFORAI_NO_DEPS": "1",  # no installing on the first run (test_deps tries that)
        }

    @property
    def conf(self):
        return self.home / ".config" / "vmforai"

    def remote_tool(self, name, text=None):
        """Put a recording stub (or a custom script) on the fake VM's PATH."""
        return write_exe(self.remote_bin / name, text or stubs.RECORDER)

    def remote_calls(self):
        return self.remote_log.read_text().splitlines()

    def set_vm(self, state, ip="127.0.0.1", polls=0):
        self.virsh_state.write_text(f"{state}|{ip}|{polls}")

    def run(self, *args, input=None, env=None, timeout=30, check=None):
        e = dict(self.env, **(env or {}))
        # Never inherit stdin: an open pipe there would block the fake ssh.
        stdin = None if input is not None else subprocess.DEVNULL
        r = subprocess.run([str(VMFORAI), *args], input=input, stdin=stdin, env=e, cwd=self.tmp,
                           capture_output=True, text=True, timeout=timeout)
        if check is not None and r.returncode != check:
            raise AssertionError(f"exit {r.returncode} != {check}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return r

    def calls(self, prog=None):
        out = [json.loads(l) for l in self.log.read_text().splitlines() if l]
        return [c for c in out if prog is None or c["prog"] == prog]

    def clear_log(self):
        self.log.write_text("")
        self.remote_log.write_text("")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)


def listening_port(banner=b"SSH-2.0-OpenSSH_test\r\n"):
    """A TCP server on 127.0.0.1 that greets like sshd (or says nothing,
    with banner=None). Returns (server, port); call server.close()."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)

    class Server:
        closed = False

        def close(self):
            self.closed = True
            try:
                s.shutdown(socket.SHUT_RDWR)  # wakes the accept() in the thread
            except OSError:
                pass
            s.close()

    srv = Server()

    def serve():
        conns = []
        while not srv.closed:
            try:
                c, _ = s.accept()
            except OSError:
                break
            if banner:
                c.sendall(banner)
            conns.append(c)  # keep open, like a server waiting for the client
        for c in conns:
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, s.getsockname()[1]


def free_port():
    """A port nothing listens on (bound once, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
