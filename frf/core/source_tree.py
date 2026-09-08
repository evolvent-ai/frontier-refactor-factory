"""Stage source trees without following links into the factory host filesystem."""
from pathlib import Path
import os
import shutil


def validate_links(root, *, ignore=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('source tree is not a directory')
    for directory, dirs, files in os.walk(root, followlinks=False):
        ignored = set(ignore(directory, dirs + files)) if ignore else set()
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in dirs + files:
            if name in ignored:
                continue
            path = Path(directory) / name
            if path.is_symlink():
                try:
                    contained = path.resolve().is_relative_to(root)
                except RuntimeError:
                    contained = False
                if os.path.isabs(os.readlink(path)) or not contained:
                    raise ValueError('source symlink leaves its portable tree: %s' % path.relative_to(root))


def copy_tree(source, destination, *, ignore=None, dirs_exist_ok=True):
    source, destination = Path(source), Path(destination)
    validate_links(source, ignore=ignore)
    if destination.is_symlink():
        raise ValueError('destination tree must not be a symlink')
    if destination.resolve().is_relative_to(source.resolve()):
        relative = destination.resolve().relative_to(source.resolve())
        if (not relative.parts or ignore is None
                or relative.parts[0] not in ignore(str(source), [relative.parts[0]])):
            raise ValueError('source tree cannot be copied into itself without excluding the destination')
    if destination.exists():
        validate_links(destination)
        # copytree can merge regular files, but symlink entries must be replaced explicitly.
        for directory, dirs, files in os.walk(source, followlinks=False):
            ignored = set(ignore(directory, dirs + files)) if ignore else set()
            dirs[:] = [name for name in dirs if name not in ignored]
            for name in dirs + files:
                if name in ignored:
                    continue
                path = Path(directory) / name
                target = destination / path.relative_to(source)
                if target.is_symlink():
                    target.unlink()
    return shutil.copytree(source, destination, dirs_exist_ok=dirs_exist_ok,
                           symlinks=True, ignore=ignore)


def replace_control_link(path):
    """Generated control files replace source links instead of modifying their targets."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
