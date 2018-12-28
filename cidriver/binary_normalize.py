import copy
from gzip import GzipFile
import os
import shutil
import tarfile
import sys

if sys.version_info < (3,5,2):
    class TarInfoWithoutGreedyNameSplitting(tarfile.TarInfo):
        """Variant of tarfile.TarInfo that helps to ensure reproducible builds."""

        def _posix_split_name(self, name, *_):
            """Split a path into a prefix and a name part.

            This is a non-greedy variant of this function for Python versions older than 3.5.2.

            This ensures that archives before and after that version are equal at the byte level.
            This change is necessary due to the fix for https://bugs.python.org/issue24838
            """
            prefix = name[:-tarfile.LENGTH_NAME]
            while prefix and prefix[-1] != "/" and len(prefix) < len(name):
                prefix = name[:len(prefix)+1]

            name = name[len(prefix):]
            prefix = prefix[:-1]

            if len(name) > tarfile.LENGTH_NAME:
                raise ValueError("path is too long")
            return prefix, name

    class TarFile(tarfile.TarFile):
        tarinfo = TarInfoWithoutGreedyNameSplitting
else:
    TarFile = tarfile.TarFile

class ArInfo(object):
    """Represents a single member in an ar archive."""
    HEADER_SIZE = 60

    mtime = 0
    uid = 0
    gid = 0
    perm = 0

    def __init__(self, fileobj, offset, size, name=None):
        self.fileobj = fileobj
        self.offset = offset
        self.size = size
        self.name = name
        self.pos = 0
        self.mode = 'rb'
        self.padded_size = size + (size % 2)

    def tell(self):
        return self.pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = self.pos + offset
        elif whence == os.SEEK_END:
            new_pos = self.size + offset

        self.pos = max(0, min(new_pos, self.size))

    def read(self, size=None):
        max_size = self.size - self.pos
        if size is None:
            size = max_size
        else:
            size = min(size, max_size)

        self.fileobj.seek(self.offset + self.pos)
        self.pos += size

        buf = self.fileobj.read(size)
        if len(buf) != size:
            raise IOError("unexpected end of data")
        return buf

    def write(self, buf):
        if self.mode != 'ab':
            raise IOError("bad operation for mode {self.mode!r}".format(self=self))

        self.fileobj.seek(self.offset + self.pos)
        self.fileobj.write(buf)
        self.size += max(0, len(buf) - (self.size - self.pos))
        self.pos += len(buf)
        return len(buf)

    def close(self):
        if self.mode == 'rb':
            return

        remainder = self.size % 2
        self.padded_size = self.size + remainder
        if remainder > 0:
            self.fileobj.seek(self.offset + self.size)
            self.fileobj.write(b'\n' * remainder)

        self.fileobj.seek(self.offset - self.HEADER_SIZE)
        self.fileobj.write(self.tobuf())
        self.arfile.offset = self.offset + self.padded_size
        self.mode == 'rb'

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        if type is None:
            self.close()
        else:
            # An exception occurred. Don't call close() because we cannot afford to try writing the header
            self.mode = 'rb'

    @classmethod
    def frombuf(cls, fileobj, buf, data_offset):
        if len(buf) != cls.HEADER_SIZE:
            raise IOError("Too short a header for ar file: {} instead of {}".format(len(buf), HEADER_SIZE))
        member_name, mtime, uid, gid, perm, size, ending = (
                buf[ 0:16],
                buf[16:28],
                buf[28:34],
                buf[34:40],
                buf[40:48],
                buf[48:58],
                buf[58:60],
            )
        member_name = member_name.rstrip(b' ').rstrip(b'/').decode('ASCII')

        try:
            size = int(size)
        except ValueError:
            raise IOError("Non-numeric file size in ar header")

        arinfo = cls(fileobj, data_offset, size, member_name or None)

        arinfo.mtime = int(mtime)
        arinfo.uid   = int(uid  )
        arinfo.gid   = int(gid)
        arinfo.perm  = int(perm, 8)

        return arinfo

    def tobuf(self):
        buf = u'{self.name:<16.16}{self.mtime:<12d}{self.uid:<6d}{self.gid:<6d}{self.perm:<8o}{self.size:<10d}`\n'.format(self=self).encode('ASCII')
        if len(buf) != self.HEADER_SIZE:
            raise IOError("Exceeding maximum header size: {buf}".format(buf=buf))
        return buf

class ArFile(object):
    """Provides an interface to ar archives."""
    arinfo = ArInfo

    def __init__(self, name=None, mode='r', fileobj=None):
        modes = {'r': 'rb', 'w': 'wb'}
        if mode not in modes:
            raise ValueError("mode must be 'r' or 'w'")
        self.mode = mode

        if not fileobj:
            fileobj = open(name, modes[mode])
            self._extfileobj = False
        else:
            if name is None:
                name = getattr(fileobj, 'name', None)
            self._extfileobj = True

        try:
            self.name = os.path.abspath(name) if name else None
            self.fileobj = fileobj
            self.closed = False

            if mode == 'r':
                self.read_signature = False
            if mode == 'w':
                self.fileobj.write(b'!<arch>\n')
            self.offset = self.fileobj.tell()
        except:
            if not self._extfileobj:
                fileobj.close()
            self.closed = True
            raise

    def close(self):
        if self.closed:
            return

        self.closed = True
        if not self._extfileobj:
            self.fileobj.close()

    def next(self):
        if self.closed:
            raise IOError("ArFile is closed")
        if self.mode != 'r':
            raise IOError("bad operation for mode {self.mode!r}".format(self=self))

        self.fileobj.seek(self.offset)
        if not self.read_signature:
            signature = self.fileobj.read(8)
            self.offset += len(signature)
            expected_signature = b'!<arch>\n'
            if len(signature) < len(expected_signature):
                raise StopIteration
            if signature != expected_signature:
                raise IOError('Invalid ar file signature')
            self.read_signature = True

        file_header = self.fileobj.read(self.arinfo.HEADER_SIZE)
        self.offset += len(file_header)
        if len(file_header) < self.arinfo.HEADER_SIZE:
            raise StopIteration
        arinfo = self.arinfo.frombuf(self.fileobj, file_header, self.offset)
        self.offset += arinfo.padded_size
        return arinfo
    def __next__(self):
        return self.next()

    def __iter__(self):
        if self.closed:
            raise IOError("ArFile is closed")
        if self.mode != 'r':
            raise IOError("bad operation for mode {self.mode!r}".format(self=self))

        self.offset = 0
        self.read_signature = False
        return self


    def appendfile(self, arinfo):
        if self.closed:
            raise IOError("ArFile is closed")
        if self.mode != 'w':
            raise IOError("bad operation for mode {self.mode!r}".format(self=self))

        arinfo = copy.copy(arinfo)

        self.fileobj.seek(self.offset)
        buf = arinfo.tobuf()
        self.fileobj.write(buf)
        arinfo.offset = self.offset + len(buf)

        arinfo.fileobj = self.fileobj
        arinfo.arfile = self
        arinfo.mode = 'ab'
        arinfo.size = 0
        arinfo.pos = 0

        return arinfo

    def addfile(self, arinfo, fileobj):
        with self.appendfile(arinfo) as outfile:
            shutil.copyfileobj(fileobj, outfile)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

def normalize(filename, fileobj=None, outname='', outfileobj=None, source_date_epoch=0):
    """Make the given file as close to reproducible as possible. Mostly be clamping timestamps to source_date_epoch."""
    if (fileobj is None or outfileobj is None) and not os.path.isfile(filename):
        return

    if filename.endswith('.tar') or filename.endswith('.tar.gz'):
        if outfileobj is None:
            archivefile = outfile = open(filename + '.tmp', 'wb')
        else:
            archivefile = outfile = outfileobj
        with TarFile.open(filename, fileobj=fileobj) as in_archive:
            try:
                compress = False
                if filename.endswith('.gz'):
                    compress = True
                    archivefile = GzipFile(filename=outname, mode='wb', compresslevel=9, fileobj=outfile, mtime=source_date_epoch)

                with TarFile.open(outname, fileobj=archivefile, format=tarfile.USTAR_FORMAT, mode='w', encoding='UTF-8') as out_archive:
                    # Sorting the file list ensures that we don't depend on the order that files appear on disk
                    for member in sorted(in_archive, key=lambda x: x.name):
                        # Clamping mtime to source_date_epoch ensures that source files are the only sources of timestamps, not build time
                        member.mtime = min(member.mtime, source_date_epoch)

                        # Prevent including the account details of the account used to execute the build
                        if member.uid == os.getuid() or member.gid == os.getgid():
                            member.uid = 0
                            member.gid = 0
                        member.uname = ''
                        member.gname = ''

                        fileobj = (in_archive.extractfile(member) if member.isfile() else None)
                        out_archive.addfile(member, fileobj)
                if compress:
                    archivefile.close()
            finally:
                if outfileobj is not None:
                    outfile.close()
        if outfileobj is None:
            os.utime(filename + '.tmp', (source_date_epoch, source_date_epoch))
            os.rename(filename + '.tmp', filename)

    elif filename.endswith('.deb'):
        with ArFile(filename) as in_pkg, ArFile(filename + '.tmp', 'w') as out_pkg:
            # A valid Debian package contains these files in this order
            expected_files = [
                    (u'debian-binary',),
                    (u'control.tar', u'control.tar.gz', u'control.tar.xz'),
                    (u'data.tar', u'data.tar.gz', u'data.tar.bz2', u'data.tar.xz'),
                ]
            for pkg_member in in_pkg:
                if expected_files:
                    expected = expected_files.pop(0)
                    if pkg_member.name not in expected:
                        break

                # Clamping mtime to source_date_epoch ensures that source files are the only sources of timestamps, not build time
                pkg_member.mtime = min(pkg_member.mtime, source_date_epoch)

                # Prevent including permission information
                pkg_member.uid = 0
                pkg_member.gid = 0
                pkg_member.perm = 0o100644

                with out_pkg.appendfile(pkg_member) as outfile:
                    normalize(pkg_member.name, fileobj=pkg_member, outname=pkg_member.name, outfileobj=outfile, source_date_epoch=source_date_epoch)
            else:
                in_pkg.close()
                out_pkg.close()
                os.utime(out_pkg.name, (source_date_epoch, source_date_epoch))
                os.rename(out_pkg.name, in_pkg.name)

    elif fileobj is not None and outfileobj is not None:
        shutil.copyfileobj(fileobj, outfileobj)
