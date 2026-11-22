#!/usr/bin/env python3
"""Read-only inventory, private byte copies, and offline verification (macOS)."""

import argparse
from contextlib import contextmanager, nullcontext
import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import sys


ITEMS = {
    "projects": (".claude", "projects"),
    "desktop-code": ("Library", "Application Support", "Claude-3p", "claude-code-sessions"),
    "desktop-scratch": ("Library", "Application Support", "Claude", "scratch-workspaces"),
    "desktop-agent": ("Library", "Application Support", "Claude", "local-agent-mode-sessions"),
}
STORE = ".claude-code-local-backup"
CHUNK = 1024 * 1024
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
DIR = READ | os.O_DIRECTORY
CREATE = os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL
DATALESS = 0x40000000  # Darwin SF_DATALESS (do not open placeholders).
EXCLUDED = {
    ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".ssh", ".git-credentials",
    "auth", "auth.json", "cookies", "cookies.binarycookies", "cookies.sqlite",
    "credentials", "credentials.json", ".credentials.json", "debug", "login data",
    "keychains", "log", "logs", "session storage", "local storage",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
}


class Stop(Exception):
    """Fixed status code only; never embed private names or exception messages."""


class Absent(Stop):
    pass


class StatFS(ctypes.Structure):
    # Darwin 64-bit struct statfs, from sys/mount.h. Only f_flags is read.
    _fields_ = [
        ("block_size", ctypes.c_uint32), ("io_size", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5), ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32), ("fs_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
        ("type_name", ctypes.c_char * 16), ("mount_on", ctypes.c_char * 1024),
        ("mount_from", ctypes.c_char * 1024), ("reserved", ctypes.c_uint32 * 8),
    ]


class LocalPolicy:
    """Use Darwin FD APIs, without subprocesses, for local-volume and ACL checks."""

    def __init__(self):
        lib = ctypes.CDLL(None, use_errno=True)
        # Intel macOS uses the INODE64 symbol; arm64 exposes plain fstatfs.
        self.statfs = getattr(lib, "fstatfs$INODE64", lib.fstatfs)
        self.statfs.argtypes = (ctypes.c_int, ctypes.POINTER(StatFS))
        self.statfs.restype = ctypes.c_int
        self.get_acl = lib.acl_get_fd_np
        self.get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
        self.get_acl.restype = ctypes.c_void_p
        self.free_acl = lib.acl_free
        self.free_acl.argtypes = (ctypes.c_void_p,)
        self.free_acl.restype = ctypes.c_int

    def check_volume(self, fd):
        data = StatFS()
        if self.statfs(fd, ctypes.byref(data)) != 0:
            raise OSError(ctypes.get_errno(), "fstatfs")
        if not data.flags & 0x1000 or data.flags & 0x200000:  # MNT_LOCAL / MNT_IGNORE_OWNERSHIP
            raise Stop("UNSAFE_VOLUME")

    def check_private(self, fd, device, directory):
        info = os.fstat(fd)
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (not kind(info.st_mode) or info.st_dev != device or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
                or (not directory and info.st_nlink != 1) or info.st_flags & DATALESS):
            raise Stop("UNSAFE_BACKUP_PERMISSIONS_OR_TYPE")
        acl = self.get_acl(fd, 0x100)  # ACL_TYPE_EXTENDED
        if acl:
            self.free_acl(acl)
            raise Stop("UNSAFE_BACKUP_ACL")
        code = ctypes.get_errno()
        if code != errno.ENOENT:  # Darwin reports no extended ACL as ENOENT.
            raise OSError(code, "acl_get_fd_np")


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_gid, info.st_flags, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def inspect_entry(parent, name):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise Stop("SYMLINK")
    if info.st_flags & DATALESS:
        raise Stop("DATALESS_PLACEHOLDER")
    return info


def check_entry(info, directory, device):
    if device is not None and info.st_dev != device:
        raise Stop("CROSSES_VOLUME")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise Stop("UNEXPECTED_TYPE")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Stop("HARDLINK_OR_SPECIAL_FILE")


@contextmanager
def existing(parent, name, *, directory, device=None, before=None):
    info = inspect_entry(parent, name) if before is None else before
    check_entry(info, directory, device)
    fd = os.open(name, DIR if directory else READ, dir_fd=parent)
    try:
        if fingerprint(os.fstat(fd)) != fingerprint(info):
            raise Stop("TREE_CHANGED")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def descend(fd, parts, device=None):
    # Reopen each component relative to an already-open ancestor, never by an
    # unchecked absolute path. Hold only one FD for each descent step.
    current = os.dup(fd)
    try:
        for name in parts:
            with existing(current, name, directory=True, device=device) as child:
                next_fd = os.dup(child)
            os.close(current)
            current = next_fd
        yield current
    finally:
        os.close(current)


@contextmanager
def open_home(path):
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or path.anchor != "/":
        raise Stop("UNSUPPORTED_HOME")
    root = os.open("/", DIR)
    try:
        with descend(root, path.parts[1:]) as fd:
            if os.fstat(fd).st_uid != os.getuid():
                raise Stop("UNSUPPORTED_HOME")
            yield fd
    finally:
        os.close(root)


@contextmanager
def private_directory(parent, name, policy, device, *, create=False, reuse=False):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            if not reuse:
                raise Stop("DESTINATION_ALREADY_EXISTS") from None
    with existing(parent, name, directory=True, device=device) as fd:
        policy.check_private(fd, device, directory=True)
        yield fd


@contextmanager
def new_file(parent, name, policy, device):
    fd = os.open(name, CREATE, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as stream:
        policy.check_private(stream.fileno(), device, directory=False)
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def excluded(name):
    lower = name.casefold()
    return (lower in EXCLUDED or lower.startswith((".env.", "cookies.sqlite-", "login data-"))
            or lower == ".env" or lower.endswith((".log", ".pem", ".key", ".p12", ".pfx")))


@dataclass
class Tree:
    metadata: dict = field(default_factory=dict)
    entries: dict = field(default_factory=dict)

    def summary(self):
        sizes = [entry[1] for entry in self.entries.values() if entry[0] == "file"]
        return len(sizes), sum(sizes), len(self.entries) - len(sizes) - 1

    def approval(self, item):
        # No content or paths in the token. Directory timestamps also detect
        # entry additions, removals and renames between plan and backup.
        data = json.dumps(["plan-v1", item, sorted(self.metadata.values())], separators=(",", ":"))
        return hashlib.sha256(data.encode("ascii")).hexdigest()


def require_unchanged(parent, name, fd, before):
    if (fingerprint(os.fstat(fd)) != before
            or fingerprint(inspect_entry(parent, name)) != before):
        raise Stop("TREE_CHANGED")


def stream_file(fd, target, size):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = os.read(fd, min(CHUNK, remaining))
        if not block:
            raise Stop("TREE_CHANGED")
        remaining -= len(block)
        digest.update(block)
        if target is not None:
            target.write(block)
    if os.read(fd, 1):
        raise Stop("TREE_CHANGED")
    return digest.hexdigest()


def walk(parent, name, device, policy, *, mode, root_info, target=None, approved=None, expected=None):
    tree = Tree()

    def visit(parent_fd, child_name, rel, target_fd, info):
        stamp = fingerprint(info)
        if approved is not None and approved.get(rel) != stamp:
            raise Stop("TREE_CHANGED")
        directory = stat.S_ISDIR(info.st_mode)
        if expected is not None:
            entry = expected.get(rel)
            if (directory and entry != ["dir"]) or (not directory and (
                    not isinstance(entry, list) or len(entry) != 3
                    or entry[:2] != ["file", info.st_size])):
                raise Stop("BAD_COPY_OR_MANIFEST")
        with existing(parent_fd, child_name, directory=directory, device=device, before=info) as fd:
            if mode == "verify":
                policy.check_private(fd, device, directory)
            tree.metadata[rel] = stamp
            if directory:
                tree.entries[rel] = ["dir"]
                output_name = "copy" if rel == "." else child_name
                output = (private_directory(target_fd, output_name, policy, device, create=True)
                          if target_fd is not None else nullcontext())
                with output as destination:
                    for child in sorted(os.listdir(fd)):
                        if excluded(child):
                            raise Stop("OUT_OF_SCOPE_ENTRY" if mode != "verify" else "BAD_COPY_OR_MANIFEST")
                        subpath = child if rel == "." else rel + "/" + child
                        visit(fd, child, subpath, destination, inspect_entry(fd, child))
            else:
                output = (new_file(target_fd, child_name, policy, device)
                          if target_fd is not None else nullcontext())
                with output as destination:
                    digest = stream_file(fd, destination, info.st_size) if mode != "plan" else None
                tree.entries[rel] = ["file", info.st_size, digest]
            require_unchanged(parent_fd, child_name, fd, stamp)

    try:
        visit(parent, name, ".", target, root_info)
    except FileNotFoundError:
        raise Stop("TREE_CHANGED") from None
    return tree


@contextmanager
def source_entry(home, item, device):
    if item == "projects" and os.environ.get("CLAUDE_CONFIG_DIR"):
        raise Stop("CUSTOM_CONFIG_NOT_SUPPORTED")
    *parts, name = ITEMS[item]
    with descend(home, parts, device) as parent:
        info = inspect_entry(parent, name)
        check_entry(info, True, device)
        yield parent, name, info


def source_tree(home, item, device, policy, *, mode, target=None, approved=None):
    try:
        with source_entry(home, item, device) as (parent, name, info):
            return walk(parent, name, device, policy, mode=mode, root_info=info,
                        target=target, approved=approved)
    except FileNotFoundError:
        if approved is not None:
            raise Stop("TREE_CHANGED") from None
        raise Absent("MISSING_SOURCE") from None


def manifest(batch_id, item, entries):
    return {"format": 1, "batch": batch_id, "item": item, "entries": entries}


def copy_tree(batch, device, policy, expected):
    info = inspect_entry(batch, "copy")
    check_entry(info, True, device)
    return walk(batch, "copy", device, policy, mode="verify", root_info=info, expected=expected)


def read_private(batch, name, device, policy, reader):
    with existing(batch, name, directory=False, device=device) as fd:
        policy.check_private(fd, device, directory=False)
        stamp = fingerprint(os.fstat(fd))
        with os.fdopen(os.dup(fd), "rb") as stream:
            result = reader(stream)
        require_unchanged(batch, name, fd, stamp)
        return result


def backup(home, item, snapshot, device, policy):
    with private_directory(home, STORE, policy, device, create=True, reuse=True) as store:
        batch_id = secrets.token_hex(8)
        with private_directory(store, batch_id, policy, device, create=True) as batch:
            print("BATCH=" + batch_id, flush=True)
            copied = source_tree(home, item, device, policy, mode="hash", target=batch,
                                 approved=snapshot.metadata)
            if copied.metadata != snapshot.metadata:
                raise Stop("TREE_CHANGED")
            if copy_tree(batch, device, policy, copied.entries).entries != copied.entries:
                raise Stop("BAD_COPY_OR_MANIFEST")
            after = source_tree(home, item, device, policy, mode="hash", approved=snapshot.metadata)
            if copied.metadata != after.metadata or copied.entries != after.entries:
                raise Stop("TREE_CHANGED")
            data = manifest(batch_id, item, copied.entries)
            with new_file(batch, "manifest.json", policy, device) as out:
                out.write(json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
            # A missing marker means even a physically present copy is incomplete.
            # linkat is exclusive and makes a checked marker visible in one step.
            with new_file(batch, ".ready", policy, device):
                pass
            os.link(".ready", "complete", src_dir_fd=batch, dst_dir_fd=batch, follow_symlinks=False)
            os.unlink(".ready", dir_fd=batch)
            print("BACKED_UP item={} {}".format(item, format_summary(copied)))


def verify(home, batch_id, device, policy):
    with private_directory(home, STORE, policy, device) as store:
        with private_directory(store, batch_id, policy, device) as batch:
            try:
                marker = read_private(batch, "complete", device, policy, lambda stream: stream.read(1))
            except FileNotFoundError:
                raise Stop("INCOMPLETE_BATCH") from None
            if marker or set(os.listdir(batch)) != {"complete", "manifest.json", "copy"}:
                raise Stop("BAD_BATCH_LAYOUT")
            try:
                record = read_private(batch, "manifest.json", device, policy, json.load)
                if not isinstance(record, dict):
                    raise Stop("BAD_MANIFEST")
                item = record.get("item")
                entries = record.get("entries")
                if (not isinstance(item, str) or item not in ITEMS or not isinstance(entries, dict)
                        or record != manifest(batch_id, item, entries)):
                    raise Stop("BAD_MANIFEST")
                actual = copy_tree(batch, device, policy, entries)
            except (ValueError, FileNotFoundError, RecursionError):
                raise Stop("BAD_COPY_OR_MANIFEST") from None
            if actual.summary()[0] == 0 or record != manifest(batch_id, item, actual.entries):
                raise Stop("BAD_COPY_OR_MANIFEST")
            print("VERIFIED item={} {}".format(item, format_summary(actual)))


def format_summary(tree):
    return "files={} bytes={} subdirs={}".format(*tree.summary())


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "STOP reason=INVALID_ARGUMENTS (see --help)\n")


def main(argv=None, *, home=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("plan").add_argument("--item", required=True, choices=ITEMS)
    writer = commands.add_parser("backup")
    writer.add_argument("--item", required=True, choices=ITEMS)
    writer.add_argument("--confirm", required=True, metavar="PLAN_TOKEN")
    commands.add_parser("verify").add_argument("--backup", required=True, metavar="BATCH_ID")
    args = parser.parse_args(argv)
    mask = os.umask(0o077)
    try:
        if sys.platform != "darwin" or sys.version_info < (3, 9):
            raise Stop("REQUIRES_MACOS_PYTHON_3_9")
        if os.getuid() == 0 or os.getuid() != os.geteuid():
            raise Stop("DO_NOT_RUN_AS_ROOT")
        policy = LocalPolicy()
        # No CLI/env home override. The keyword is used only by synthetic tests.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir) if home is None else home
        with open_home(home) as fd:
            policy.check_volume(fd)
            device = os.fstat(fd).st_dev
            if args.action == "verify":
                if not re.fullmatch(r"[0-9a-f]{16}", args.backup):
                    raise Stop("INVALID_BATCH_ID")
                try:
                    verify(fd, args.backup, device, policy)
                except FileNotFoundError:
                    raise Absent("MISSING_BATCH") from None
                return 0
            planned = source_tree(fd, args.item, device, policy, mode="plan")
            if planned.summary()[0] == 0:
                raise Absent("EMPTY_SOURCE")
            token = planned.approval(args.item)
            if args.action == "plan":
                print("PLAN item={} {} CONFIRM={}".format(args.item, format_summary(planned), token))
            else:
                if not re.fullmatch(r"[0-9a-f]{64}", args.confirm) or args.confirm != token:
                    raise Stop("PLAN_MISMATCH")
                backup(fd, args.item, planned, device, policy)
            return 0
    except Absent as err:
        print("SKIP reason=" + str(err))
        return 3
    except Stop as err:
        print("STOP reason=" + str(err))
    except OSError as err:
        reason = "PERMISSION_DENIED" if err.errno in (errno.EACCES, errno.EPERM) else "SYSTEM_ERROR"
        print("STOP reason={} errno={}".format(reason, err.errno))
    except (AttributeError, KeyError, RecursionError, ValueError):
        print("STOP reason=UNSUPPORTED_OR_INVALID_DATA")
    except KeyboardInterrupt:
        print("STOP reason=INTERRUPTED")
    finally:
        os.umask(mask)
    return 2


if __name__ == "__main__":
    sys.exit(main())
