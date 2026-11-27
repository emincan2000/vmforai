"""vmforai cp: copying files and folders to and from a fake VM over tar/ssh."""
import os
import time
import unittest

from tests.helpers import SandboxTest, listening_port


class CpTests(SandboxTest):
    def setUp(self):
        super().setUp()
        self.sb.run("add", "vm", "--host", "192.0.2.7", "--user", "dev", "--port", "2222", "--os", "linux", check=0)
        self.env = {"STUB_SSH_EXEC": "1"}

    def test_copy_file_and_folder_to_vm(self):
        src_file = self.sb.tmp / "hello.txt"
        src_file.write_text("hello world\n")  # 12 bytes
        dest_dir = self.sb.remote_home / "work"
        dest_dir.mkdir()

        r = self.sb.run("cp", "vm", str(src_file), "~/work", env=self.env, check=0)
        self.assertEqual((dest_dir / "hello.txt").read_text(), "hello world\n")
        self.assertEqual(r.stdout.strip(), "Copied 1 file, 12 bytes to vm:~/work")

        # SSH connection details match vmforai shell (user, port, host key alias, known_hosts).
        argv = self.sb.calls("ssh")[-1]["argv"]
        self.assertEqual(argv[argv.index("-l") + 1], "dev")
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertIn("HostKeyAlias=vmforai-vm", argv)
        self.assertIn(f"UserKnownHostsFile={self.sb.conf}/known_hosts", argv)

        # Recursive folder copy, including nested subdirectories and dotfiles.
        proj = self.sb.tmp / "proj"
        (proj / "sub").mkdir(parents=True)
        (proj / "a.c").write_text("int a;\n")          # 7 bytes
        (proj / "sub" / "b.c").write_text("int b;\n")  # 7 bytes
        (proj / ".hidden").write_text("ok")            # 2 bytes

        r = self.sb.run("cp", "vm", str(proj), "~/work", env=self.env, check=0)
        self.assertEqual((dest_dir / "proj" / "a.c").read_text(), "int a;\n")
        self.assertEqual((dest_dir / "proj" / "sub" / "b.c").read_text(), "int b;\n")
        self.assertEqual((dest_dir / "proj" / ".hidden").read_text(), "ok")
        self.assertEqual(r.stdout.strip(), "Copied 3 files, 16 bytes to vm:~/work")

    def test_copy_multiple_sources_and_rename_to_vm(self):
        d1 = self.sb.tmp / "d1"
        d2 = self.sb.tmp / "d2"
        d1.mkdir()
        d2.mkdir()
        f1 = d1 / "one.txt"
        f2 = d2 / "two.txt"
        f1.write_text("111")
        f2.write_text("2222")

        # Multiple sources into a new directory on the VM.
        r = self.sb.run("cp", "vm", str(f1), str(f2), "~/bundle", env=self.env, check=0)
        self.assertEqual((self.sb.remote_home / "bundle" / "one.txt").read_text(), "111")
        self.assertEqual((self.sb.remote_home / "bundle" / "two.txt").read_text(), "2222")
        self.assertEqual(r.stdout.strip(), "Copied 2 files, 7 bytes to vm:~/bundle")

        # Single file copied to a new filename on the VM.
        r = self.sb.run("cp", "vm", str(f1), "~/renamed.txt", env=self.env, check=0)
        self.assertEqual((self.sb.remote_home / "renamed.txt").read_text(), "111")
        self.assertEqual(r.stdout.strip(), "Copied 1 file, 3 bytes to vm:~/renamed.txt")

        # Single folder copied to a new folder name on the VM.
        r = self.sb.run("cp", "vm", str(d1), "~/d1-copy", env=self.env, check=0)
        self.assertEqual((self.sb.remote_home / "d1-copy" / "one.txt").read_text(), "111")

    def test_copy_from_vm(self):
        rdir = self.sb.remote_home / "out"
        (rdir / "nested").mkdir(parents=True)
        (rdir / "result.bin").write_bytes(b"\x00\x01\x02hello")  # 8 bytes
        (rdir / "nested" / "log.txt").write_text("done\n")       # 5 bytes

        local_dir = self.sb.tmp / "dest"
        local_dir.mkdir()

        # Single file from VM into an existing local folder.
        r = self.sb.run("cp", "vm", "--from", "~/out/result.bin", str(local_dir), env=self.env, check=0)
        self.assertEqual((local_dir / "result.bin").read_bytes(), b"\x00\x01\x02hello")
        self.assertEqual(r.stdout.strip(), f"Copied 1 file, 8 bytes from vm to {local_dir}")

        # Recursive folder from VM into a new local folder name.
        local_copy = self.sb.tmp / "out-copy"
        r = self.sb.run("cp", "vm", "--from", "~/out", str(local_copy), env=self.env, check=0)
        self.assertEqual((local_copy / "result.bin").read_bytes(), b"\x00\x01\x02hello")
        self.assertEqual((local_copy / "nested" / "log.txt").read_text(), "done\n")
        self.assertEqual(r.stdout.strip(), f"Copied 2 files, 13 bytes from vm to {local_copy}")

        # Single file from VM to a new local filename.
        local_file = self.sb.tmp / "renamed.bin"
        r = self.sb.run("cp", "vm", "--from", "~/out/result.bin", str(local_file), env=self.env, check=0)
        self.assertEqual(local_file.read_bytes(), b"\x00\x01\x02hello")
        self.assertEqual(r.stdout.strip(), f"Copied 1 file, 8 bytes from vm to {local_file}")

    def test_preserves_mtime_and_touch_sets_now_both_directions(self):
        old_time = int(time.time()) - 7200  # 2 hours ago
        build_time = int(time.time()) - 60  # 1 minute ago (newer than source, older than now)

        # Host -> VM: without --touch keeps old mtime.
        src = self.sb.tmp / "main.c"
        src.write_text("int main(void) { return 0; }\n")
        os.utime(src, (old_time, old_time))
        vm_dir = self.sb.remote_home / "build"
        vm_dir.mkdir()

        self.sb.run("cp", "vm", str(src), "~/build", env=self.env, check=0)
        vm_file = vm_dir / "main.c"
        self.assertAlmostEqual(vm_file.stat().st_mtime, old_time, delta=2)

        # Host -> VM: with --touch sets mtime to now, even when overwriting a newer file.
        os.utime(vm_file, (build_time, build_time))
        before = time.time() - 2
        self.sb.run("cp", "vm", "--touch", str(src), "~/build", env=self.env, check=0)
        self.assertGreaterEqual(vm_file.stat().st_mtime, before)

        # VM -> Host: without --touch keeps old mtime; with --touch sets mtime to now.
        os.utime(vm_file, (old_time, old_time))
        local_dir = self.sb.tmp / "pulled"
        local_dir.mkdir()

        self.sb.run("cp", "vm", "--from", "~/build/main.c", str(local_dir), env=self.env, check=0)
        pulled = local_dir / "main.c"
        self.assertAlmostEqual(pulled.stat().st_mtime, old_time, delta=2)

        before = time.time() - 2
        self.sb.run("cp", "vm", "--from", "--touch", "~/build/main.c", str(local_dir), env=self.env, check=0)
        self.assertGreaterEqual(pulled.stat().st_mtime, before)

    def test_missing_source_fails_both_directions(self):
        # Host -> VM: missing local source fails before touching SSH.
        r = self.sb.run("cp", "vm", str(self.sb.tmp / "nope.txt"), "~/work", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("source not found", r.stderr)
        self.assertEqual(self.sb.calls("ssh"), [])

        # VM -> Host: missing remote source fails clearly and prints no summary.
        local_dir = self.sb.tmp / "out"
        local_dir.mkdir()
        r = self.sb.run("cp", "vm", "--from", "~/does-not-exist", str(local_dir), env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("source not found on VM", r.stderr)
        self.assertNotIn("Copied", r.stdout)

    def test_unwritable_destination_fails_both_directions(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")

        # Host -> VM: read-only directory on VM.
        src = self.sb.tmp / "a.txt"
        src.write_text("hi")
        ro_vm = self.sb.remote_home / "readonly"
        ro_vm.mkdir()
        ro_vm.chmod(0o555)
        self.addCleanup(lambda: ro_vm.chmod(0o755))

        r = self.sb.run("cp", "vm", str(src), "~/readonly", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not writable", r.stderr)
        self.assertNotIn("Copied", r.stdout)

        # VM -> Host: read-only directory on host fails before SSH.
        (self.sb.remote_home / "b.txt").write_text("hi")
        ro_local = self.sb.tmp / "ro-local"
        ro_local.mkdir()
        ro_local.chmod(0o555)
        self.addCleanup(lambda: ro_local.chmod(0o755))
        self.sb.clear_log()

        r = self.sb.run("cp", "vm", "--from", "~/b.txt", str(ro_local), env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not writable", r.stderr)
        self.assertEqual(self.sb.calls("ssh"), [])

    def test_starts_stopped_libvirt_vm(self):
        listener, port = listening_port()
        self.addCleanup(listener.close)
        self.sb.run("add", "libvm", "--domain", "testvm", "--port", str(port), "--os", "linux", check=0)
        self.sb.set_vm("shut off", ip="127.0.0.1", polls=0)

        src = self.sb.tmp / "note.txt"
        src.write_text("ok\n")
        r = self.sb.run("cp", "libvm", str(src), "~/note.txt", env=self.env, check=0)
        self.assertEqual((self.sb.remote_home / "note.txt").read_text(), "ok\n")
        virsh_cmds = [c["argv"][2] for c in self.sb.calls("virsh")]
        self.assertIn("start", virsh_cmds)
        self.assertNotIn("shutdown", virsh_cmds)  # like single-command shell, stays running
        self.assertIn("Copied 1 file, 3 bytes to libvm:~/note.txt", r.stdout)

    def test_help_and_usage_errors(self):
        r = self.sb.run("help", "cp", check=0)
        self.assertIn("vmforai cp NAME", r.stdout)
        self.assertIn("--from", r.stdout)
        self.assertIn("--touch", r.stdout)
        self.assertIn("Examples:", r.stdout)
        self.assertEqual(self.sb.run("cp", "--help", check=0).stdout, r.stdout)
        self.assertIn("cp NAME SRC... DEST", self.sb.run("help", check=0).stdout)

        self.assertEqual(self.sb.run("cp").returncode, 2)
        self.assertEqual(self.sb.run("cp", "vm").returncode, 2)
        self.assertEqual(self.sb.run("cp", "vm", "only-src").returncode, 2)
        self.assertEqual(self.sb.run("cp", "vm", "--bogus", "a", "b").returncode, 2)


if __name__ == "__main__":
    unittest.main()
