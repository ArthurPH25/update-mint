
'Safe file removal.\n\nRules that apply to EVERY removal performed by the program (user or root):\n  * the path must be absolute and must NOT be a protected root (/, /home, ~, /usr...);\n  * symbolic links are never followed (the link itself is treated as a file), and the parent directories of the\n    cleanup root are checked too: a symbolic link in the middle of the path (not owned by root) is refused;\n  * directories are opened with O_NOFOLLOW and handled through file descriptors, so a directory replaced by a\n    symbolic link while the cleanup runs cannot redirect the removal elsewhere;\n  * removal never crosses into another file system (same ``st_dev`` as the root);\n  * when an age limit is given, only "unused" files (atime AND mtime older than N days) are removed;\n  * files opened by a process are skipped (when the list is provided);\n  * the function returns what it deleted (files/bytes) and what it skipped, for the report.\n'
from __future__ import annotations

import fnmatch
import functools
import grp
import os
import pwd
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set


PROTECTED_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/media", "/mnt",
    "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var", "/usr/bin", "/usr/lib",
    "/usr/local", "/usr/sbin", "/usr/share", "/var/lib", "/var/log", "/var/cache", "/var/tmp", "/tmp",
}


class ForbiddenPath(Exception):
    pass


@dataclass
class Summary:
    files: int = 0
    bytes: int = 0
    skipped_in_use: int = 0
    errors: List[str] = field(default_factory=list)

    def sum(self, other: "Summary") -> None:
        self.files += other.files
        self.bytes += other.bytes
        self.skipped_in_use += other.skipped_in_use
        self.errors.extend(other.errors)


@functools.lru_cache(maxsize=None)
def _group_exclusive_to(gid: int, uid: int) -> bool:
    try:
        owner = pwd.getpwuid(uid)
        if owner.pw_gid != gid:
            return False
        if any(p.pw_gid == gid and p.pw_uid != uid for p in pwd.getpwall()):
            return False
        try:
            members = grp.getgrgid(gid).gr_mem
        except KeyError:
            members = []
        return all(pwd.getpwnam(name).pw_uid == uid for name in members)
    except KeyError:
        return False 


def _others_can_modify(st: os.stat_result) -> bool:
    euid = os.geteuid()
    if st.st_uid not in (0, euid):
        return True
    if st.st_mode & stat.S_IWOTH and not st.st_mode & stat.S_ISVTX:
        return True
    if st.st_mode & stat.S_IWGRP:
        return not _group_exclusive_to(st.st_gid, st.st_uid)
    return False


def _check_physical_chain(real: str) -> None:
    current = "/"
    for part in [""] + [p for p in real.split("/") if p][:-1]:
        current = os.path.join(current, part) if part else "/"
        try:
            st = os.lstat(current)
        except OSError:
            return  
        if _others_can_modify(st):
            raise ForbiddenPath(f"directory in the resolved path can be modified by other users: {current}")


def _check_ancestors(norm: str) -> str:
    parts = [p for p in norm.split("/") if p][:-1]
    current = "/"
    trusted_link = False
    for part in parts:
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except OSError:
            break
        if stat.S_ISLNK(st.st_mode):
            if st.st_uid != 0:
                raise ForbiddenPath(f"symbolic link in the path: {current}")
            trusted_link = True
    if not trusted_link:
        return norm
    real = os.path.realpath(norm)
    _check_physical_chain(real)
    return real


def _opened_at(fd: int, expected: str) -> bool:
    try:
        return os.readlink(f"/proc/self/fd/{fd}") == expected
    except OSError:
        return True 


def validate_root(root_path: str, home: Optional[str] = None, allow_top_level: bool = False) -> str:
    'Raises :class:`ForbiddenPath` if ``root_path`` cannot be targeted for bulk removal.'
    if not root_path or not os.path.isabs(root_path):
        raise ForbiddenPath(f"path is not absolute: {root_path!r}")
    norm = os.path.normpath(root_path)
    if norm in PROTECTED_ROOTS and not allow_top_level:
        raise ForbiddenPath(f"protected root: {norm}")
    if home and norm == os.path.normpath(home):
        raise ForbiddenPath('the entire home directory is never a cleanup target')
    if os.path.islink(norm):
        raise ForbiddenPath(f"is a symbolic link: {norm}")
    _check_ancestors(norm)
    return norm


def tree_size(root_path: str) -> int:
    'Sums file sizes (st_size) without following links or leaving the file system.'
    total = 0
    try:
        dev = os.lstat(root_path).st_dev
    except OSError:
        return 0
    stack = [root_path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_dev != dev:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    else:
                        total += st.st_size
        except OSError:
            continue
    return total


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_dir_nofollow(name: str, dir_fd: Optional[int] = None) -> int:
    if dir_fd is None:
        return os.open(name, _DIR_FLAGS)
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _remove_empty_root(root_path: str, root_st: os.stat_result) -> None:
    parent_fd = None
    try:
        parent_fd = os.open(os.path.dirname(root_path) or "/", _DIR_FLAGS & ~os.O_NOFOLLOW)
        name = os.path.basename(root_path)
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (root_st.st_dev, root_st.st_ino):
            os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _is_unused(st: os.stat_result, limit: float) -> bool:
    return st.st_atime < limit and st.st_mtime < limit


def clean_tree(
    root_path: str,
    *,
    age_days: Optional[float] = None,
    exclude: Optional[Callable[[str, bool], bool]] = None,
    preserved_names: Iterable[str] = (),
    open_files: Optional[Set[str]] = None,
    delete_root_contents: bool = False,
    regular_files_only: bool = False,
    home: Optional[str] = None,
    simulate: bool = False,
    now: Optional[float] = None,
    allow_top_level: bool = False,
) -> Summary:
    "Deletes the contents of ``root_path`` according to this module's rules.\n\n    ``age_days=None`` deletes everything (100% regenerable cache); with an age limit, only unused items.\n    ``exclude(path, is_dir)`` returns True to preserve an item (and everything inside it, if it is a directory).\n    ``preserved_names`` are fnmatch patterns for file NAMES that are never deleted.\n    "
    root_path = validate_root(root_path, home, allow_top_level=allow_top_level)
    expected_location = _check_ancestors(root_path)
    res = Summary()
    try:
        root_fd = _open_dir_nofollow(root_path)
    except FileNotFoundError:
        return res
    except OSError as e:
        res.errors.append(f"{root_path}: {e.strerror}")
        return res
    if not _opened_at(root_fd, expected_location):
        os.close(root_fd)
        res.errors.append(f"{root_path}: the directory is not where it was validated (path changed while opening) — left untouched")
        return res
    limit = (now if now is not None else time.time()) - age_days * 86400 if age_days is not None else None
    preserve = tuple(preserved_names)

    def preserved(path: str, is_dir: bool) -> bool:
        if exclude and exclude(path, is_dir):
            return True
        name = os.path.basename(path)
        return any(fnmatch.fnmatch(name, part) for part in preserve)

    def descend(parent_fd: int, name: str, path: str, st: os.stat_result) -> bool:
        'Opens subdirectory ``name`` of ``parent_fd`` without following links and visits it.'
        try:
            fd = _open_dir_nofollow(name, parent_fd)
        except OSError as e:
            res.errors.append(f"{path}: {e.strerror}")
            return False
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or opened.st_dev != dev:
                res.errors.append(f"{path}: changed while it was being cleaned (left untouched)")
                return False
            return visit(fd, path)
        finally:
            os.close(fd)

    def visit(dir_fd: int, d: str) -> bool:
        'Returns True if directory ``d`` (open as ``dir_fd``) is empty.'
        empty = True
        try:
            with os.scandir(dir_fd) as it:
                names = [e.name for e in it]
        except OSError as e:
            res.errors.append(f"{d}: {e.strerror}")
            return False
        for name in names:
            path = os.path.join(d, name)
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                empty = False
                continue
            if st.st_dev != dev:
                empty = False
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if preserved(path, is_dir):
                empty = False
                continue
            if is_dir:
                if descend(dir_fd, name, path, st):
                    if limit is None or st.st_mtime < limit:
                        try:
                            if not simulate:
                                os.rmdir(name, dir_fd=dir_fd)
                        except OSError:
                            empty = False
                    else:
                        empty = False
                else:
                    empty = False
                continue

            if regular_files_only and not stat.S_ISREG(st.st_mode):
                empty = False
                continue
            if limit is not None and not _is_unused(st, limit):
                empty = False
                continue
            if open_files and path in open_files:
                res.skipped_in_use += 1
                empty = False
                continue
            try:
                if not simulate:
                    os.unlink(name, dir_fd=dir_fd)
                res.files += 1
                res.bytes += st.st_size if stat.S_ISREG(st.st_mode) else 0
            except OSError as ex:
                res.errors.append(f"{path}: {ex.strerror}")
                empty = False
        return empty

    try:
        root_st = os.fstat(root_fd)
        dev = root_st.st_dev
        try:
            became_empty = visit(root_fd, root_path)
        except RecursionError:
            res.errors.append(f"{root_path}: directory tree too deep (cleanup stopped)")
            became_empty = False
    finally:
        os.close(root_fd)
    if delete_root_contents and became_empty and not simulate:
        _remove_empty_root(root_path, root_st)
    return res


def delete_directory_contents(path: str, *, home: Optional[str] = None, simulate: bool = False) -> Summary:
    'Deletes an entire cache directory (including the directory itself). For 100% regenerable caches.\n\n    Uses :func:`clean_tree` (does not follow links or cross file systems): this is the\n    safe equivalent of ``rm -rf --one-file-system``.\n    '
    path = validate_root(path, home)
    if not os.path.lexists(path):
        return Summary()
    if not os.path.isdir(path) or os.path.islink(path):  
        res = Summary()
        try:
            res.bytes += os.lstat(path).st_size
            if not simulate:
                os.unlink(path)
            res.files += 1
        except OSError as e:
            res.errors.append(f"{path}: {e.strerror}")
        return res
    return clean_tree(path, delete_root_contents=True, home=home, simulate=simulate)
