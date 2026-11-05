"""Translate local, UNC and connected SMB locations into native filesystem paths.

Authentication and mounting belong to the operating system. No share credentials
are accepted, stored, or sent by Bookroom.
"""
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import stat
import subprocess
import urllib.parse


@dataclass(frozen=True)
class Share:
    server: str
    share: str
    parts: tuple[str, ...] = ()

    @property
    def uri(self):
        return 'smb://'+self.server+'/'+ '/'.join(urllib.parse.quote(p,safe='') for p in (self.share,*self.parts))

    @property
    def unc(self): return str(PureWindowsPath('\\\\'+self.server+'\\'+self.share,*self.parts))


@dataclass(frozen=True)
class Mount:
    path: Path
    remote: Share | None = None


@dataclass(frozen=True)
class Location:
    path: Path
    uri: str = ''
    boundary: Path | None = None

    @property
    def value(self): return self.uri or str(self.path)

    @property
    def parent(self):
        if self.uri:
            if self.boundary==self.path: return self.uri
            return self.uri.rsplit('/',1)[0]
        return str(self.path.parent)

    def child(self,name):
        return self.uri.rstrip('/')+'/'+urllib.parse.quote(name,safe='') if self.uri else str(self.path/name)


def _share(server, parts):
    if not server or server in {'.','..','?'} or any(c in server for c in '@\\/\x00'):
        raise ValueError('Use a server and share name without embedded credentials.')
    if not parts or not parts[0]: raise ValueError('Include a share name, for example smb://server/books.')
    if any(p in {'.','..'} or '\x00' in p for p in parts):
        raise ValueError('Network locations cannot contain parent-directory traversal or null characters.')
    if ':' in server: raise ValueError('For IPv6 shares, use the mounted folder path.')
    return Share(server.casefold(),parts[0],tuple(parts[1:]))


def parse_location(value,system=None):
    """Return a local path string or a validated share; no filesystem access."""
    system=system or platform.system()
    text=str(value).strip()
    if not text: raise ValueError('Choose a folder path or network location.')
    if '\x00' in text: raise ValueError('Folder paths cannot contain null characters.')
    if text.casefold().startswith('\\\\?\\unc\\'): text='\\\\'+text[8:]
    if text.startswith(('\\\\','//')):
        return _share(*_unc_parts(text))
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:',text) and not re.match(r'^[a-zA-Z]:',text):
        parsed=urllib.parse.urlsplit(text)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError('Do not put credentials in a network URL. Connect the share in your operating system first.')
        if parsed.query or parsed.fragment: raise ValueError('Folder URLs cannot contain a query or fragment. Encode special filename characters such as # as %23.')
        if parsed.scheme.casefold() in {'smb','cifs'}:
            if parsed.port is not None: raise ValueError('For custom network ports, mount the share and use its local folder path.')
            return _share(parsed.hostname,tuple(p for p in urllib.parse.unquote(parsed.path).replace('\\','/').split('/') if p))
        if parsed.scheme.casefold()=='file':
            if parsed.port is not None: raise ValueError('File URLs cannot specify a network port; use the mounted folder path.')
            path=urllib.parse.unquote(parsed.path)
            if parsed.netloc and parsed.netloc.casefold()!='localhost':
                return _share(parsed.hostname,tuple(p for p in path.replace('\\','/').split('/') if p))
            if path.startswith('//'): return _share(*_unc_parts(path))
            if not path.startswith('/'): raise ValueError('Use an absolute file URL.')
            if system=='Windows' and re.match(r'^/[a-zA-Z]:',path): path=path[1:]
            return path
        raise ValueError('Use a folder path, UNC path, smb:// URL, or file:// URL. Mount other protocols such as NFS or SFTP first and use their folder path.')
    return text


def _unc_parts(text):
    parts=tuple(p for p in text.replace('\\','/').split('/') if p)
    return (parts[0] if parts else '',parts[1:])


def _unescape_mount(value):
    return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),value)


def _gvfs_roots():
    roots=[]
    if os.environ.get('XDG_RUNTIME_DIR'): roots.append(Path(os.environ['XDG_RUNTIME_DIR'])/'gvfs')
    if hasattr(os,'getuid'): roots.append(Path('/run/user')/str(os.getuid())/'gvfs')
    roots.append(Path.home()/'.gvfs')
    return list(dict.fromkeys(roots))


def parse_mountinfo(text):
    mounts=[]
    for line in text.splitlines():
        try:
            left,right=line.split(' - ',1);info=right.split();fields=left.split()
            if info[0] not in {'cifs','smb3','nfs','nfs4','fuse.sshfs','davfs','fuse.davfs'}: continue
            remote=parse_location(_unescape_mount(info[1])) if info[0] in {'cifs','smb3'} else None
            if info[0] in {'cifs','smb3'} and not isinstance(remote,Share): continue
            mounts.append(Mount(Path(_unescape_mount(fields[4])),remote if isinstance(remote,Share) else None))
        except (ValueError,IndexError): continue
    return mounts


def mounted_shares():
    """Read mount metadata, without probing remote server contents."""
    mounts=[];system=platform.system()
    if system=='Linux':
        try: mounts.extend(parse_mountinfo(Path('/proc/self/mountinfo').read_text()))
        except OSError: pass
        for root in _gvfs_roots():
            try:
                for path in root.iterdir():
                    if not path.name.startswith('smb-share:'): continue
                    fields=dict(pair.split('=',1) for pair in path.name.split(':',1)[1].split(',') if '=' in pair)
                    try: remote=_share(urllib.parse.unquote(fields.get('server','')),(urllib.parse.unquote(fields.get('share','')),))
                    except ValueError: continue
                    mounts.append(Mount(path,remote))
            except OSError: pass
    elif system=='Darwin':
        try:
            result=subprocess.run(['mount'],capture_output=True,text=True,timeout=5,check=True)
            for line in result.stdout.splitlines():
                match=re.match(r'(.+) on (.+) \(smbfs[,)]',line)
                if not match: continue
                source=match[1].removeprefix('//').rsplit('@',1)[-1]
                remote=parse_location('//'+urllib.parse.unquote(source))
                mounts.append(Mount(Path(_unescape_mount(match[2])),remote))
        except (OSError,ValueError,subprocess.SubprocessError): pass
    return list(dict.fromkeys(mounts))


def resolve_location(value):
    parsed=parse_location(value)
    if isinstance(parsed,Share):
        if platform.system()=='Windows':
            path=Path(parsed.unc)
            return Location(path,parsed.uri,Path(Share(parsed.server,parsed.share).unc))
        matches=[m for m in mounted_shares() if m.remote and m.remote.server==parsed.server and m.remote.share.casefold()==parsed.share.casefold() and parsed.parts[:len(m.remote.parts)]==m.remote.parts]
        if not matches:
            raise ValueError('This network share is not connected. Open it in your file manager (Finder: Go → Connect to Server; Windows: Explorer), then retry. On Linux, the share must be mounted or exposed through GVfs/FUSE. You can also enter its mounted folder path.')
        mount=max(matches,key=lambda m:len(m.remote.parts))
        return Location(mount.path.joinpath(*parsed.parts[len(mount.remote.parts):]),parsed.uri,mount.path)
    path=Path(os.path.abspath(Path(parsed).expanduser()))
    # Preserve the logical SMB source for rescans, even if the mount directory
    # changes after reconnecting. NFS/SFTP remain native mounted folder paths.
    for mount in sorted(mounted_shares(),key=lambda m:len(m.path.parts),reverse=True):
        if mount.remote and path.is_relative_to(mount.path):
            relative=path.relative_to(mount.path).parts
            remote=Share(mount.remote.server,mount.remote.share,mount.remote.parts+relative)
            return Location(path,remote.uri,mount.path)
    return Location(path)


def directory(location):
    try:
        mode=location.path.stat().st_mode
        if not stat.S_ISDIR(mode): raise NotADirectoryError(str(location.path))
        return location.path
    except OSError as error:
        detail='Check that the network share is connected and your account has read permission.' if location.uri else 'Check the path, mount connection, and your account’s read permission.'
        raise ValueError(f'Cannot open folder: {location.value}. {detail} ({error.strerror or type(error).__name__})') from error


def list_folders(value):
    location=resolve_location(value or str(Path.home()))
    folder=directory(location)
    children=[]
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith('.') or entry.is_symlink(): continue
                try:
                    if entry.is_dir(): children.append({'name':entry.name,'path':location.child(entry.name)})
                except OSError: continue
    except OSError as error:
        raise ValueError(f'Cannot list folder: {location.value}. Reconnect the share and check read permissions. ({error.strerror})') from error
    return dict(path=location.value,parent=location.parent,children=sorted(children,key=lambda p:p['name'].casefold())[:2000])


def folder_locations():
    result=[{'name':'Home folder','path':str(Path.home())}]
    system=platform.system()
    if system=='Windows':
        import ctypes
        mask=ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask&(1<<i): result.append({'name':chr(65+i)+': drive','path':chr(65+i)+':\\'})
    else:
        for path in ([Path('/Volumes')] if system=='Darwin' else [Path('/mnt'),Path('/media'),Path('/run/media')]):
            if path.is_dir(): result.append({'name':path.name+' (mounted folders)','path':str(path)})
    for mount in mounted_shares():
        result.append({'name':mount.remote.uri if mount.remote else mount.path.name+' (network mount)','path':mount.remote.uri if mount.remote else str(mount.path)})
    return list({row['path']:row for row in result}.values())
