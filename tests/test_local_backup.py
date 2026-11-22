#!/usr/bin/env python3
"""Regression tests: no production home, real credentials, or evidence files."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_backup.py"
main = runpy.run_path(str(SCRIPT))["main"]
CODE = main.__globals__
STORE = ".claude-code-local-backup"
ITEMS = {
    "projects": ".claude/projects",
    "desktop-code": "Library/Application Support/Claude-3p/claude-code-sessions",
    "desktop-scratch": "Library/Application Support/Claude/scratch-workspaces",
    "desktop-agent": "Library/Application Support/Claude/local-agent-mode-sessions",
}
NAME = "synthetic-name-甲.bin"
DATA = b"synthetic-payload-do-not-print\x00\xff"


def invoke(home, *args):
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            result = main(args, home=home)
        except SystemExit as err:
            result = err.code
    return result, stdout.getvalue(), stderr.getvalue()


@unittest.skipUnless(sys.platform == "darwin" and sys.version_info >= (3, 9) and os.getuid(),
                     "requires macOS, Python 3.9+, and an unprivileged user")
class BackupTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="local-backup-synthetic-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.store = self.home / STORE
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def run_checked(self, expected, marker, *args):
        code, stdout, stderr = invoke(self.home, *args)
        combined = stdout + stderr
        self.assertEqual(code, expected, combined)
        self.assertIn(marker, combined)
        self.assertNotIn(str(self.home), combined)
        self.assertNotIn(NAME, combined)
        self.assertNotIn("synthetic-payload-do-not-print", combined)
        self.assertNotIn(sha256(DATA).hexdigest(), combined)
        return combined

    def fixture(self, item="projects"):
        path = self.home / ITEMS[item] / "fixture-subdir" / NAME
        path.parent.mkdir(parents=True)
        path.write_bytes(DATA)
        return path

    def plan(self, item="projects", marker="PLAN item="):
        output = self.run_checked(0, marker, "plan", "--item", item)
        return re.search(r"\bCONFIRM=([0-9a-f]{64})$", output, re.M)[1]

    def backup(self, token, item="projects"):
        output = self.run_checked(0, "BACKED_UP item=" + item,
                                  "backup", "--item", item, "--confirm", token)
        return re.search(r"^BATCH=([0-9a-f]{16})$", output, re.M)[1]

    def test_four_categories_plan_backup_verify_and_open(self):
        sources = {item: self.fixture(item) for item in ITEMS}
        tokens = {}
        for item in ITEMS:
            with patch.dict(CODE, {"stream_file": lambda *args: self.fail("plan read bytes")}):
                tokens[item] = self.plan(item, f"PLAN item={item} files=1 bytes={len(DATA)} subdirs=1")
        self.assertFalse(self.store.exists())
        seen = set()
        for item, source in sources.items():
            with self.subTest(item=item):
                source.chmod(0o700)
                # Fixture mode changed; the previously approved version cannot be copied.
                self.run_checked(2, "PLAN_MISMATCH", "backup", "--item", item, "--confirm", tokens[item])
                token = self.plan(item)
                batch = self.backup(token, item)
                self.assertNotIn(batch, seen)
                seen.add(batch)
                base = self.store / batch
                copy = base / "copy" / "fixture-subdir" / NAME
                self.run_checked(0, "VERIFIED item=" + item, "verify", "--backup", batch)
                self.assertEqual(source.read_bytes(), DATA)
                self.assertEqual(copy.read_bytes(), DATA)  # Automated open, not a human check.
                for dirpath in (self.store, base, base / "copy", copy.parent):
                    self.assertEqual(stat.S_IMODE(dirpath.stat().st_mode), 0o700)
                for filepath in (copy, base / "complete", base / "manifest.json"):
                    self.assertEqual(stat.S_IMODE(filepath.stat().st_mode), 0o600)
                manifest = json.loads((base / "manifest.json").read_text())
                row = manifest["entries"][f"fixture-subdir/{NAME}"]
                self.assertEqual(row, ["file", len(DATA), sha256(DATA).hexdigest()])
                # Source edits after success do not affect verification of the copy.
                source.write_bytes(b"different synthetic source")
                self.run_checked(0, "VERIFIED item=" + item, "verify", "--backup", batch)
                copy.write_bytes(b"X" * len(DATA))  # Same-size damage must still fail.
                self.run_checked(2, "BAD_COPY_OR_MANIFEST", "verify", "--backup", batch)

    def test_missing_empty_zero_bytes_and_custom_config(self):
        self.run_checked(3, "MISSING_SOURCE", "plan", "--item", "projects")
        self.run_checked(3, "MISSING_BATCH", "verify", "--backup", "0" * 16)
        folder = self.home / ITEMS["projects"]
        folder.mkdir(parents=True)
        (folder / "empty-dir").mkdir()
        self.run_checked(3, "EMPTY_SOURCE", "plan", "--item", "projects")
        self.run_checked(3, "EMPTY_SOURCE", "backup", "--item", "projects", "--confirm", "0" * 64)
        self.assertFalse(self.store.exists())
        (folder / "zero.bytes").touch()
        self.plan(marker="files=1 bytes=0")
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.home / "alternate")):
            self.run_checked(2, "CUSTOM_CONFIG_NOT_SUPPORTED", "plan", "--item", "projects")

    def test_arguments_approval_and_pre_copy_mutation(self):
        src = self.fixture()
        self.run_checked(2, "INVALID_ARGUMENTS", "backup", "--item", "projects")
        self.run_checked(2, "INVALID_ARGUMENTS", "plan", "--item", "cookies")
        self.run_checked(2, "INVALID_ARGUMENTS", "restore", "--backup", NAME)
        self.run_checked(2, "INVALID_ARGUMENTS", "archive", "--backup", NAME)
        self.run_checked(2, "INVALID_BATCH_ID", "verify", "--backup", "../outside")
        token = self.plan()
        self.fixture("desktop-code")
        self.run_checked(2, "PLAN_MISMATCH", "backup", "--item", "desktop-code", "--confirm", token)
        src.write_bytes(b"new size after approval")
        self.run_checked(2, "PLAN_MISMATCH", "backup", "--item", "projects", "--confirm", token)
        self.assertFalse(self.store.exists())

    def test_change_during_copy_retains_source_and_marks_batch_incomplete(self):
        src = self.fixture()
        src.write_bytes(DATA * CODE["CHUNK"])
        token = self.plan()
        inode = src.stat().st_ino
        os_read = os.read
        mutated = False

        def change_after_first_read(fd, size):
            nonlocal mutated
            block = os_read(fd, size)
            if not mutated and block and os.fstat(fd).st_ino == inode:
                mutated = True
                with src.open("r+b") as stream:
                    stream.seek(-len(DATA), os.SEEK_END)
                    stream.write(b"Z" * len(DATA))
            return block

        with patch.object(os, "read", change_after_first_read):
            result = self.run_checked(2, "TREE_CHANGED", "backup", "--item", "projects", "--confirm", token)
        self.assertTrue(mutated)
        self.assertIn("BATCH=", result)
        self.assertNotIn("BACKED_UP", result)
        self.assertTrue(src.is_file())
        batch = next(self.store.iterdir())
        self.assertFalse((batch / "complete").exists())
        self.run_checked(2, "INCOMPLETE_BATCH", "verify", "--backup", batch.name)

    def test_symlinks_special_files_and_exclusions(self):
        src = self.fixture()
        entry = src.with_name("other")
        entry.symlink_to(self.home / "not-present")
        self.run_checked(2, "SYMLINK", "plan", "--item", "projects")
        entry.unlink()
        os.link(src, entry)
        self.run_checked(2, "HARDLINK_OR_SPECIAL_FILE", "plan", "--item", "projects")
        entry.unlink()
        os.mkfifo(entry)
        self.run_checked(2, "HARDLINK_OR_SPECIAL_FILE", "plan", "--item", "projects")
        entry.unlink()
        for name in ("credentials.json", ".env.local", "setup.log", "Cookies"):
            with self.subTest(name=name):
                entry = src.with_name(name)
                entry.write_bytes(DATA)
                result = self.run_checked(2, "OUT_OF_SCOPE_ENTRY", "plan", "--item", "projects")
                self.assertNotIn(name, result)
                entry.unlink()
        ancestor = self.home / ".claude"
        alternate = self.home / "fixture-rename"
        ancestor.rename(alternate)
        ancestor.symlink_to(alternate, target_is_directory=True)
        self.run_checked(2, "SYMLINK", "plan", "--item", "projects")
        self.assertFalse(self.store.exists())

    def test_permissions_and_acl(self):
        src = self.fixture()
        token = self.plan()
        src.chmod(0)
        self.run_checked(2, "PERMISSION_DENIED", "plan", "--item", "projects")
        src.chmod(0o600)
        token = self.plan()
        self.store.mkdir(mode=0o755)
        self.store.chmod(0o755)
        self.run_checked(2, "UNSAFE_BACKUP_PERMISSIONS_OR_TYPE", "backup", "--item", "projects", "--confirm", token)
        self.assertEqual(list(self.store.iterdir()), [])
        self.store.chmod(0o700)
        subprocess.run(["/bin/chmod", "+a", "everyone allow list,search", str(self.store)],
                       capture_output=True, check=True)
        try:
            self.run_checked(2, "UNSAFE_BACKUP_ACL", "backup", "--item", "projects", "--confirm", token)
            self.assertEqual(list(self.store.iterdir()), [])
        finally:
            subprocess.run(["/bin/chmod", "-N", str(self.store)], capture_output=True, check=True)
        batch = self.backup(token)
        copy = self.store / batch / "copy" / "fixture-subdir" / NAME
        copy.chmod(0o644)
        self.run_checked(2, "UNSAFE_BACKUP_PERMISSIONS_OR_TYPE", "verify", "--backup", batch)

    def test_store_symlink_and_bad_copy_layout(self):
        src = self.fixture()
        token = self.plan()
        target = self.home / "unapproved-target"
        target.mkdir()
        self.store.symlink_to(target)
        self.run_checked(2, "SYMLINK", "backup", "--item", "projects", "--confirm", token)
        self.assertEqual(list(target.iterdir()), [])
        self.store.unlink()
        batch = self.backup(token)
        folder = self.store / batch
        copy = folder / "copy" / "fixture-subdir" / NAME
        copy.unlink()
        copy.symlink_to(src)
        self.run_checked(2, "SYMLINK", "verify", "--backup", batch)
        copy.unlink()
        self.run_checked(2, "BAD_COPY_OR_MANIFEST", "verify", "--backup", batch)
        extra = copy.with_name("unlisted.bin")
        extra.touch(mode=0o600)
        with patch.dict(CODE, {"stream_file": lambda *args: self.fail("verify read an unlisted file")}):
            self.run_checked(2, "BAD_COPY_OR_MANIFEST", "verify", "--backup", batch)
        (folder / "manifest.json").write_bytes(b"malformed synthetic data")
        self.run_checked(2, "BAD_COPY_OR_MANIFEST", "verify", "--backup", batch)

    def test_macos_sandbox_denies_source_read_and_store_write(self):
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            self.skipTest("sandbox-exec not installed; this is not a Codex test")
        src = self.fixture()
        token = self.plan()
        program = ("from pathlib import Path; import runpy, sys; "
                   "sys.exit(runpy.run_path(sys.argv[1])['main'](sys.argv[3:], home=Path(sys.argv[2])))")
        for operation, deny_action, deny_path in (
            (["plan", "--item", "projects"], "file-read*", src.parents[1]),
            (["backup", "--item", "projects", "--confirm", token], "file-write*", self.home),
        ):
            with self.subTest(action=deny_action):
                profile = f'(version 1) (allow default) (deny network*) (deny {deny_action} (subpath (param "FIXTURE")))'
                result = subprocess.run([str(sandbox), "-D", "FIXTURE=" + str(deny_path), "-p", profile,
                                         sys.executable, "-I", "-c", program, str(SCRIPT), str(self.home), *operation],
                                        capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("STOP reason=PERMISSION_DENIED", result.stdout)
                self.assertNotIn(str(self.home), result.stdout)
                self.assertNotIn("BATCH=", result.stdout)
                self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
