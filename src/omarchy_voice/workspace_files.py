"""Descriptor-relative workspace I/O; never follow attacker-swapped symlinks."""
from contextlib import contextmanager
import errno
import os
from pathlib import PurePosixPath
import stat
import uuid


@contextmanager
def parent_fd(root, relative, create=False):
    if not isinstance(relative, str) or '\0' in relative:
        raise ValueError('invalid workspace path')
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or '..' in path.parts:
        raise ValueError('path must stay inside the workspace')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError('workspace path contains a symlink or non-directory') from None
                raise
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


@contextmanager
def open_file(root, relative, mode='rb'):
    if mode not in ('rb', 'xb'):
        raise ValueError('unsupported workspace file mode')
    with parent_fd(root, relative, create=mode=='xb') as (parent, name):
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_RDONLY if mode=='rb' else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('not a regular workspace file')
        stream = os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


def write_text(root, relative, content):
    with parent_fd(root, relative, create=True) as (parent, name):
        temporary = '.oma-write-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(content)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
