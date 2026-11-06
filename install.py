#!/usr/bin/env python3
"""Install an isolated Bookroom release from GitHub using only the standard library."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPOSITORY = 'vittok/librarian'
MARKER = 'Bookroom installer managed launcher'
MANIFEST = '.bookroom-install.json'
MAX_ARCHIVE = 30_000_000
MAX_EXTRACTED = 100_000_000


class InstallError(Exception):
    pass


def defaults():
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData'/'Local'))/'Bookroom'
        return base/'app', base/'bin'
    return Path.home()/'.local/share/bookroom-app', Path.home()/'.local/bin'


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent':'Bookroom-installer/1.0', 'Accept':'application/vnd.github+json'}), timeout=60)


def resolve_ref(ref):
    if re.fullmatch('[0-9a-fA-F]{40}', ref):
        return ref.lower()
    if not ref or len(ref)>250 or '\x00' in ref:
        raise InstallError('Invalid GitHub ref. Use a branch, tag, or full commit SHA.')
    with request(f'https://api.github.com/repos/{REPOSITORY}/commits/'+urllib.parse.quote(ref,safe='')) as response:
        commit = json.loads(response.read(1_000_000)).get('sha', '')
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise InstallError('GitHub did not return a valid commit for this ref.')
    return commit


def download_archive(commit, destination):
    url = f'https://codeload.github.com/{REPOSITORY}/tar.gz/{commit}'
    with request(url) as response, destination.open('wb') as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
            raise InstallError('GitHub archive download did not use HTTPS.')
        expected = int(response.headers.get('Content-Length', 0))
        if expected > MAX_ARCHIVE:
            raise InstallError('Repository archive exceeds the download limit.')
        received = 0
        while True:
            chunk = response.read(1024*1024)
            if not chunk: break
            received += len(chunk)
            if received > MAX_ARCHIVE:
                raise InstallError('Repository archive exceeds the download limit.')
            output.write(chunk)
        if not received or (expected and received != expected):
            raise InstallError('Incomplete repository download. Run the installer again.')


def extract_source(archive, destination):
    """Copy regular files only; never follow archive links or write outside source."""
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        if len(members)>20000 or any(m.size<0 for m in members) or sum(m.size for m in members)>MAX_EXTRACTED:
            raise InstallError('Repository archive exceeds the extraction limit.')
        roots = set(); seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or any(':' in p for p in name.parts):
                raise InstallError('Repository archive contains an unsafe path.')
            if not name.parts: continue
            roots.add(name.parts[0])
            if not (member.isdir() or member.isfile()):
                raise InstallError('Repository archive contains a link or special file.')
            if len(name.parts)==1:
                if member.isfile(): raise InstallError('Repository archive has an unexpected layout.')
                continue
            relative = Path(*name.parts[1:])
            key = str(relative).casefold() if os.name=='nt' else str(relative)
            if member.isfile() and key in seen: raise InstallError('Repository archive contains duplicate file paths.')
            if member.isfile(): seen.add(key)
            target = destination/relative
            if member.isdir(): target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with source.extractfile(member) as data, target.open('xb') as output:
                    shutil.copyfileobj(data,output)
        if len(roots)!=1 or not (destination/'pyproject.toml').is_file() or not (destination/'bookroom/__init__.py').is_file():
            raise InstallError('Downloaded archive is not a Bookroom source tree.')


def atomic_write(path, text, executable=False):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',newline='\n',dir=path.parent,prefix='.bookroom-',delete=False) as output:
            temporary = Path(output.name); output.write(text)
        if executable: temporary.chmod(0o755)
        os.replace(temporary,path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def check_prefix(prefix):
    marker = prefix/MANIFEST
    if marker.exists():
        try: metadata=json.loads(marker.read_text())
        except (OSError,ValueError): raise InstallError(f'Cannot read install ownership file: {marker}')
        if not isinstance(metadata,dict) or metadata.get('repository') != REPOSITORY:
            raise InstallError('Install directory belongs to another application. Choose a different --prefix.')
    elif prefix.exists() and any(prefix.iterdir()):
        raise InstallError('Install directory is not empty and is not managed by Bookroom. Choose a different --prefix.')


def check_launcher(launcher):
    if launcher.is_symlink():
        raise InstallError(f'Refusing to replace an existing launcher symlink: {launcher}. Choose another --bin-dir.')
    if launcher.exists():
        if not launcher.is_file() or MARKER not in launcher.read_text(encoding='utf-8',errors='replace')[:500]:
            raise InstallError(f'An unrelated command already exists: {launcher}. Choose another --bin-dir.')


@contextmanager
def install_lock(prefix):
    with (prefix/'.install.lock').open('a+b') as lock:
        lock.write(b'0'); lock.flush(); lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise InstallError('Another installation is using this directory. Wait for it to finish and retry.')
        try: yield
        finally:
            if os.name=='nt':
                import msvcrt
                lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def launcher_text(python, windows=None):
    windows = os.name=='nt' if windows is None else windows
    if windows:
        path=str(python).replace('%','%%')
        if '"' in path: raise InstallError('Windows install paths cannot contain quotes.')
        return f'@echo off\nrem {MARKER}\nsetlocal\n"{path}" -I -m bookroom %*\n'
    return '#!/bin/sh\n# '+MARKER+'\nexec '+shlex.quote(str(python))+' -I -m bookroom "$@"\n'


def run(command, **kwargs):
    environment = dict(os.environ)
    environment.pop('PYTHONPATH',None); environment.pop('PYTHONHOME',None)
    subprocess.run([str(v) for v in command],check=True,env=environment,**kwargs)


def ensure_pip(python):
    try:
        run([python,'-I','-m','ensurepip','--upgrade'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        return
    except subprocess.CalledProcessError:
        print('This Python omits ensurepip; bootstrapping pip inside the private environment from PyPA…',flush=True)
    with tempfile.TemporaryDirectory(prefix='bookroom-pip-') as folder:
        script=Path(folder)/'get-pip.py'
        with request('https://bootstrap.pypa.io/get-pip.py') as response:
            if urllib.parse.urlsplit(response.geturl()).scheme!='https':
                raise InstallError('pip bootstrap download did not use HTTPS.')
            data=response.read(5_000_001)
        if not data or len(data)>5_000_000:raise InstallError('Invalid pip bootstrap download.')
        script.write_bytes(data)
        run([python,'-I',script,'--disable-pip-version-check','--no-input'])


def install(prefix, bin_dir, ref='main', pdf=True):
    prefix=Path(prefix).expanduser().resolve();bin_dir=Path(bin_dir).expanduser().resolve()
    if any(c in str(prefix)+str(bin_dir) for c in '\r\n\x00'):
        raise InstallError('Install paths cannot contain newline or null characters.')
    launcher=bin_dir/('bookroom.cmd' if os.name=='nt' else 'bookroom')
    check_prefix(prefix);check_launcher(launcher)
    prefix.mkdir(parents=True,exist_ok=True)
    if not (prefix/MANIFEST).exists():
        atomic_write(prefix/MANIFEST,json.dumps({'repository':REPOSITORY},indent=2)+'\n')
    with install_lock(prefix):
        check_prefix(prefix)
        # Claim only a previously validated empty/owned install root. A failed
        # first install can then be retried without touching unrelated files.
        marker=prefix/MANIFEST
        if not marker.exists(): atomic_write(marker,json.dumps({'repository':REPOSITORY},indent=2)+'\n')
        commit=resolve_ref(ref)
        print(f'Installing {REPOSITORY} at commit {commit}',flush=True)
        releases=prefix/'releases';releases.mkdir(exist_ok=True)
        release=releases/(commit[:12]+'-'+uuid.uuid4().hex[:8]);release.mkdir()
        try:
            with tempfile.TemporaryDirectory(prefix='bookroom-download-') as folder:
                archive=Path(folder)/'source.tar.gz'
                print('Downloading source from GitHub…',flush=True)
                download_archive(commit,archive)
                source=release/'source';source.mkdir();extract_source(archive,source)
            environment=release/'venv'
            print('Creating an isolated Python environment…',flush=True)
            try: run([sys.executable,'-I','-m','venv','--without-pip',environment])
            except subprocess.CalledProcessError as error:
                raise InstallError('Cannot create a Python virtual environment. Install Python with the venv module (on Debian/Ubuntu, install python3-venv) and retry.') from error
            python=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            ensure_pip(python)
            print('Installing Bookroom'+(' and the PDF reader' if pdf else '')+'…',flush=True)
            run([python,'-I','-m','pip','--disable-pip-version-check','install','--no-input',str(source)+('[pdf]' if pdf else '')])
            run([python,'-I','-c',"from bookroom.server import main; import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE test USING fts5(text)'); print('Bookroom import and SQLite FTS5 checks passed.')"],cwd=release)
            # Publish only after a complete working release exists. Earlier
            # releases are retained because an older process may still use one.
            bin_dir.mkdir(parents=True,exist_ok=True)
            check_launcher(launcher)
            atomic_write(launcher,launcher_text(python),executable=os.name!='nt')
        except BaseException:
            shutil.rmtree(release,ignore_errors=True)
            raise
        atomic_write(marker,json.dumps({'repository':REPOSITORY,'commit':commit,'release':str(release),'launcher':str(launcher)},indent=2)+'\n')
    command = "& '"+str(launcher).replace("'","''")+"'" if os.name=='nt' else shlex.quote(str(launcher))
    print(f'\nInstalled successfully. Start Bookroom with:\n  {command}\n',flush=True)
    if str(bin_dir) not in os.environ.get('PATH','').split(os.pathsep):
        print(f'The launcher directory is not on PATH: {bin_dir}\nUse the full command above, or add that directory to your PATH.',flush=True)
    print('On first launch, Bookroom reuses Calibre or installs its own private copy. Catalogue data is stored separately from this app installation.',flush=True)
    return python,launcher


def main(argv=None):
    prefix,bin_dir=defaults()
    parser=argparse.ArgumentParser(description='Install Bookroom from GitHub into a user-owned Python virtual environment.')
    parser.add_argument('--prefix',type=Path,default=prefix,help='Application install root (default: %(default)s)')
    parser.add_argument('--bin-dir',type=Path,default=bin_dir,help='Launcher directory (default: %(default)s)')
    parser.add_argument('--ref',default='main',help='GitHub branch, tag or full commit SHA (default: main)')
    parser.add_argument('--no-pdf',action='store_true',help='Skip the optional pypdf reader')
    parser.add_argument('--run',action='store_true',help='Launch Bookroom immediately after installation')
    parser.add_argument('app_args',nargs=argparse.REMAINDER,help='App options after --, for example -- --port 8766')
    args=parser.parse_args(argv)
    if sys.version_info<(3,11): parser.exit(1,'Python 3.11 or newer is required. Install a supported Python and retry.\n')
    app_args=args.app_args[1:] if args.app_args[:1]==['--'] else args.app_args
    if app_args and not args.run:parser.error('App options require --run; place them after --.')
    try:
        python,launcher=install(args.prefix,args.bin_dir,args.ref,pdf=not args.no_pdf)
    except (InstallError,OSError,ValueError,tarfile.TarError,subprocess.CalledProcessError) as error:
        parser.exit(1,f'Installation failed: {error}\nAn existing working installation is kept. Correct the problem and retry.\n')
    except KeyboardInterrupt:parser.exit(130,'\nInstallation cancelled. An existing working installation is kept.\n')
    if args.run: os.execv(str(python),[str(python),'-I','-m','bookroom',*app_args])


if __name__=='__main__':
    main()
