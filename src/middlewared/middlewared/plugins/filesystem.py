import binascii
try:
    from bsd import acl
except ImportError:
    acl = None
import errno
import functools
import grp
import os
import pwd
import select
import shutil

import psutil
try:
    import pyinotify
except ImportError:
    pyinotify = None

from middlewared.event import EventSource
from middlewared.schema import accepts, Bool, Dict, Float, Int, List, Ref, returns, Path, Str
from middlewared.service import private, CallError, filterable_returns, Service, job
from middlewared.utils import filter_list, osc
from middlewared.utils.path import is_child
from middlewared.plugins.pwenc import PWENC_FILE_SECRET
from middlewared.plugins.cluster_linux.utils import CTDBConfig, FuseConfig


class FilesystemService(Service):

    class Config:
        cli_namespace = 'storage.filesystem'

    @private
    def resolve_cluster_path(self, path):
        """
        Convert a "CLUSTER:"-prefixed path to an absolute path
        on the server.
        """
        if not path.startswith(FuseConfig.FUSE_PATH_SUBST.value):
            return path

        gluster_volume = path[8:].split("/")[0]
        if gluster_volume == CTDBConfig.CTDB_VOL_NAME.value:
            raise CallError('access to ctdb volume is not permitted.', errno.EPERM)

        is_mounted = self.middleware.call_sync('gluster.fuse.is_mounted', {'name': gluster_volume})
        if not is_mounted:
            raise CallError(f'{gluster_volume}: cluster volume is not mounted.', errno.ENXIO)

        cluster_path = path.replace(FuseConfig.FUSE_PATH_SUBST.value, f'{FuseConfig.FUSE_PATH_BASE.value}/')
        return cluster_path

    @accepts(Str('path', required=True), Ref('query-filters'), Ref('query-options'))
    @filterable_returns(Dict(
        'path_entry',
        Str('name', required=True),
        Path('path', required=True),
        Path('realpath', required=True),
        Str('type', required=True, enum=['DIRECTORY', 'FILESYSTEM', 'SYMLINK', 'OTHER']),
        Int('size', required=True, null=True),
        Int('mode', required=True, null=True),
        Bool('acl', required=True, null=True),
        Int('uid', required=True, null=True),
        Int('gid', required=True, null=True),
    ))
    def listdir(self, path, filters, options):
        """
        Get the contents of a directory.

        Paths on clustered volumes may be specifed with the path prefix
        `CLUSTER:<volume name>`. For example, to list directories
        in the directory 'data' in the clustered volume `smb01`, the
        path should be specified as `CLUSTER:smb01/data`.

        Each entry of the list consists of:
          name(str): name of the file
          path(str): absolute path of the entry
          realpath(str): absolute real path of the entry (if SYMLINK)
          type(str): DIRECTORY | FILESYSTEM | SYMLINK | OTHER
          size(int): size of the entry
          mode(int): file mode/permission
          uid(int): user id of entry owner
          gid(int): group id of entry onwer
          acl(bool): extended ACL is present on file
        """
        path = self.resolve_cluster_path(path)
        if not os.path.exists(path):
            raise CallError(f'Directory {path} does not exist', errno.ENOENT)

        if not os.path.isdir(path):
            raise CallError(f'Path {path} is not a directory', errno.ENOTDIR)

        rv = []
        for entry in os.scandir(path):
            if entry.is_symlink():
                etype = 'SYMLINK'
            elif entry.is_dir():
                etype = 'DIRECTORY'
            elif entry.is_file():
                etype = 'FILE'
            else:
                etype = 'OTHER'

            data = {
                'name': entry.name,
                'path': entry.path.replace(f'{FuseConfig.FUSE_PATH_BASE.value}/', FuseConfig.FUSE_PATH_SUBST.value),
                'realpath': os.path.realpath(entry.path) if etype == 'SYMLINK' else entry.path,
                'type': etype,
            }
            try:
                stat = entry.stat()
                data.update({
                    'size': stat.st_size,
                    'mode': stat.st_mode,
                    'acl': False if self.acl_is_trivial(data["path"]) else True,
                    'uid': stat.st_uid,
                    'gid': stat.st_gid,
                })
            except FileNotFoundError:
                data.update({'size': None, 'mode': None, 'acl': None, 'uid': None, 'gid': None})
            rv.append(data)
        return filter_list(rv, filters=filters or [], options=options or {})

    @accepts(Str('path'))
    @returns(Dict(
        'path_stats',
        Int('size', required=True),
        Int('mode', required=True),
        Int('uid', required=True),
        Int('gid', required=True),
        Float('atime', required=True),
        Float('mtime', required=True),
        Float('ctime', required=True),
        Int('dev', required=True),
        Int('inode', required=True),
        Int('nlink', required=True),
        Str('user', null=True, required=True),
        Str('group', null=True, required=True),
        Bool('acl', required=True),
    ))
    def stat(self, path):
        """
        Return the filesystem stat(2) for a given `path`.

        Paths on clustered volumes may be specifed with the path prefix
        `CLUSTER:<volume name>`. For example, to list directories
        in the directory 'data' in the clustered volume `smb01`, the
        path should be specified as `CLUSTER:smb01/data`.
        """
        path = self.resolve_cluster_path(path)
        try:
            stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            raise CallError(f'Path {path} not found', errno.ENOENT)

        stat = {
            'size': stat.st_size,
            'mode': stat.st_mode,
            'uid': stat.st_uid,
            'gid': stat.st_gid,
            'atime': stat.st_atime,
            'mtime': stat.st_mtime,
            'ctime': stat.st_ctime,
            'dev': stat.st_dev,
            'inode': stat.st_ino,
            'nlink': stat.st_nlink,
        }

        try:
            stat['user'] = pwd.getpwuid(stat['uid']).pw_name
        except KeyError:
            stat['user'] = None

        try:
            stat['group'] = grp.getgrgid(stat['gid']).gr_name
        except KeyError:
            stat['group'] = None

        stat['acl'] = False if self.acl_is_trivial(path) else True

        return stat

    @private
    @accepts(
        Str('path'),
        Str('content', max_length=2048000),
        Dict(
            'options',
            Bool('append', default=False),
            Int('mode'),
        ),
    )
    def file_receive(self, path, content, options):
        """
        Simplified file receiving method for small files.

        `content` must be a base 64 encoded file content.
        """
        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        if options.get('append'):
            openmode = 'ab'
        else:
            openmode = 'wb+'
        with open(path, openmode) as f:
            f.write(binascii.a2b_base64(content))
        mode = options.get('mode')
        if mode:
            os.chmod(path, mode)
        if path == PWENC_FILE_SECRET:
            self.middleware.call_sync('pwenc.reset_secret_cache')
        return True

    @private
    @accepts(
        Str('path'),
        Dict(
            'options',
            Int('offset'),
            Int('maxlen'),
        ),
    )
    def file_get_contents(self, path, options):
        """
        Get contents of a file `path` in base64 encode.

        DISCLAIMER: DO NOT USE THIS FOR BIG FILES (> 500KB).
        """
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as f:
            if options.get('offset'):
                f.seek(options['offset'])
            data = binascii.b2a_base64(f.read(options.get('maxlen'))).decode().strip()
        return data

    @accepts(Str('path'))
    @returns()
    @job(pipes=["output"])
    async def get(self, job, path):
        """
        Job to get contents of `path`.
        """

        if not os.path.isfile(path):
            raise CallError(f'{path} is not a file')

        with open(path, 'rb') as f:
            await self.middleware.run_in_thread(shutil.copyfileobj, f, job.pipes.output.w)

    @accepts(
        Str('path'),
        Dict(
            'options',
            Bool('append', default=False),
            Int('mode'),
        ),
    )
    @returns(Bool('successful_put'))
    @job(pipes=["input"])
    async def put(self, job, path, options):
        """
        Job to put contents to `path`.
        """
        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        if options.get('append'):
            openmode = 'ab'
        else:
            openmode = 'wb+'

        with open(path, openmode) as f:
            await self.middleware.run_in_thread(shutil.copyfileobj, job.pipes.input.r, f)

        mode = options.get('mode')
        if mode:
            os.chmod(path, mode)
        return True

    @accepts(Str('path'))
    @returns(Dict(
        'path_statfs',
        List('flags', required=True),
        List('fsid', required=True),
        Str('fstype', required=True),
        Str('source', required=True),
        Str('dest', required=True),
        Int('blocksize', required=True),
        Int('total_blocks', required=True),
        Int('free_blocks', required=True),
        Int('avail_blocks', required=True),
        Int('files', required=True),
        Int('free_files', required=True),
        Int('name_max', required=True),
        Int('total_bytes', required=True),
        Int('free_bytes', required=True),
        Int('avail_bytes', required=True),
    ))
    def statfs(self, path):
        """
        Return stats from the filesystem of a given path.

        Raises:
            CallError(ENOENT) - Path not found
        """
        try:
            st = os.statvfs(path)
        except FileNotFoundError:
            raise CallError('Path not found.', errno.ENOENT)

        for partition in sorted(psutil.disk_partitions(), key=lambda p: len(p.mountpoint), reverse=True):
            if is_child(os.path.realpath(path), partition.mountpoint):
                break
        else:
            raise CallError('Unable to find mountpoint.')

        return {
            'flags': [],
            'fstype': partition.fstype,
            'source': partition.device,
            'dest': partition.mountpoint,
            'blocksize': st.f_frsize,
            'total_blocks': st.f_blocks,
            'free_blocks': st.f_bfree,
            'avail_blocks': st.f_bavail,
            'files': st.f_files,
            'free_files': st.f_ffree,
            'name_max': st.f_namemax,
            'fsid': [],
            'total_bytes': st.f_blocks * st.f_frsize,
            'free_bytes': st.f_bfree * st.f_frsize,
            'avail_bytes': st.f_bavail * st.f_frsize,
        }

    @accepts(Str('path'))
    @returns(Bool('paths_acl_is_trivial'))
    def acl_is_trivial(self, path):
        """
        Returns True if the ACL can be fully expressed as a file mode without losing
        any access rules.

        Paths on clustered volumes may be specifed with the path prefix
        `CLUSTER:<volume name>`. For example, to list directories
        in the directory 'data' in the clustered volume `smb01`, the
        path should be specified as `CLUSTER:smb01/data`.
        """
        path = self.resolve_cluster_path(path)
        if not os.path.exists(path):
            raise CallError(f'Path not found [{path}].', errno.ENOENT)

        acl = self.middleware.call_sync('filesystem.getacl', path, True)
        return acl['trivial']


class FileFollowTailEventSource(EventSource):

    """
    Retrieve last `no_of_lines` specified as an integer argument for a specific `path` and then
    any new lines as they are added. Specified argument has the format `path:no_of_lines` ( `/var/log/messages:3` ).

    `no_of_lines` is optional and if it is not specified it defaults to `3`.

    However `path` is required for this.
    """

    def run_sync(self):
        if ':' in self.arg:
            path, lines = self.arg.rsplit(':', 1)
            lines = int(lines)
        else:
            path = self.arg
            lines = 3
        if not os.path.exists(path):
            # FIXME: Error?
            return

        bufsize = 8192
        fsize = os.stat(path).st_size
        if fsize < bufsize:
            bufsize = fsize
        i = 0
        with open(path, encoding='utf-8', errors='ignore') as f:
            data = []
            while True:
                i += 1
                if bufsize * i > fsize:
                    break
                f.seek(fsize - bufsize * i)
                data.extend(f.readlines())
                if len(data) >= lines or f.tell() == 0:
                    break

            self.send_event('ADDED', fields={'data': ''.join(data[-lines:])})
            f.seek(fsize)

            if osc.IS_FREEBSD:
                gen = self._follow_freebsd(f)
            else:
                gen = self._follow_linux(path, f)

            for data in gen:
                self.send_event('ADDED', fields={'data': data})

    def _follow_freebsd(self, f):
        kqueue = select.kqueue()

        ev = [select.kevent(
            f.fileno(),
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_DELETE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB,
        )]
        kqueue.control(ev, 0, 0)

        while not self._cancel_sync.is_set():
            events = kqueue.control([], 1, 1)
            if not events:
                continue
            # TODO: handle other file operations other than just extend/write
            yield f.read()

    def _follow_linux(self, path, f):
        queue = []
        watch_manager = pyinotify.WatchManager()
        notifier = pyinotify.Notifier(watch_manager)
        watch_manager.add_watch(path, pyinotify.IN_MODIFY, functools.partial(self._follow_linux_callback, queue, f))

        data = f.read()
        if data:
            yield data

        while not self._cancel_sync.is_set():
            notifier.process_events()

            data = "".join(queue)
            if data:
                yield data
            queue[:] = []

            if notifier.check_events():
                notifier.read_events()

        notifier.stop()

    def _follow_linux_callback(self, queue, f, event):
        data = f.read()
        if data:
            queue.append(data)


def setup(middleware):
    middleware.register_event_source('filesystem.file_tail_follow', FileFollowTailEventSource)
