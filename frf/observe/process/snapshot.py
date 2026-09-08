"""Stable filesystem observations, including file contents and link targets."""
import hashlib
import os
import stat


def tree_lines(root, exclude=()):
    lines = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in exclude)
        for name in sorted(dirs + files):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, root)
            if any(part in exclude for part in relative.split(os.sep)):
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                value = "link " + os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                value = "directory"
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                # Do not follow a link swapped in after lstat, or block on a special file.
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode):
                        raise ValueError("file type changed during observation")
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                value = "file %s %d %s" % (
                    "x" if opened.st_mode & 0o111 else "-", opened.st_size, digest.hexdigest())
            else:
                value = "special %o" % stat.S_IFMT(info.st_mode)
            # JSON quoting makes filenames containing newlines unambiguous to line masking.
            import json
            lines.append(json.dumps([relative, value], ensure_ascii=True, separators=(",", ":")))
    return sorted(lines)


def standalone_source():
    from pathlib import Path
    return Path(__file__).read_text(encoding="utf-8")
