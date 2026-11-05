"""Find Calibre or install an app-managed copy from the official distribution."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

LINUX_INSTALLER = 'https://download.calibre-ebook.com/linux-installer.sh'
DOWNLOADS = {'Windows':'https://calibre-ebook.com/dist/portable', 'Darwin':'https://calibre-ebook.com/dist/osx'}
INSTALL_TIMEOUT = 1200


@dataclass
class Converter:
    path: str | None = None
    error: str = ''


def _environment():
    env = {**os.environ, 'QT_QPA_PLATFORM':os.environ.get('QT_QPA_PLATFORM','offscreen'), 'PYTHONUNBUFFERED':'1'}
    # The official Linux installer supports this developer override; do not let
    # it redirect an automatic install to an unrelated inherited URL.
    env.pop('CALIBRE_INSTALLER_LOCAL_URL', None)
    return env


def working_converter(candidate):
    if not candidate: return None
    path = shutil.which(str(Path(candidate).expanduser()))
    if not path: return None
    try:
        result = subprocess.run([path,'--version'],capture_output=True,timeout=20,env=_environment())
        if result.returncode==0 and b'ebook-convert' in result.stdout.lower(): return str(Path(path).resolve())
    except (OSError, subprocess.SubprocessError): pass
    return None


def _settings(data_dir):
    try:
        settings = json.loads((data_dir/'settings.json').read_text())
        return settings if isinstance(settings,dict) else {}
    except (OSError,ValueError): return {}


def _remember(data_dir, converter):
    settings = _settings(data_dir); settings['converter'] = converter
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=data_dir,prefix='.settings-',delete=False) as f:
            temporary=Path(f.name)
            json.dump(settings,f); f.flush()
        os.replace(temporary,data_dir/'settings.json')
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)



def _candidates(data_dir, system):
    saved = _settings(data_dir).get('converter')
    if isinstance(saved,str): yield saved
    yield 'ebook-convert'
    if system=='Darwin':
        yield '/Applications/calibre.app/Contents/MacOS/ebook-convert'
        yield Path.home()/'Applications/calibre.app/Contents/MacOS/ebook-convert'
    elif system=='Windows':
        for key in ('ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
            if os.environ.get(key): yield Path(os.environ[key])/'Calibre2/ebook-convert.exe'
    else: yield '/opt/calibre/ebook-convert'
    root = data_dir/'runtime/calibre'
    yield root/'ebook-convert'
    yield root/'Calibre/ebook-convert.exe'
    yield root/'calibre.app/Contents/MacOS/ebook-convert'


def find_converter(data_dir, system):
    seen = set()
    for candidate in _candidates(data_dir,system):
        if str(candidate) in seen: continue
        seen.add(str(candidate))
        found = working_converter(candidate)
        if found: return found
    return None


@contextmanager
def _install_lock(data_dir):
    """OS-owned file lock: concurrent starts wait; a crashed process releases it."""
    with (data_dir/'.calibre-install.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        deadline = time.monotonic()+INSTALL_TIMEOUT
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except (BlockingIOError,OSError):
                if time.monotonic()>=deadline: raise TimeoutError('Another Calibre installation is still running. Try starting Bookroom again later.')
                time.sleep(.25)
        try: yield
        finally:
            if os.name=='nt':
                import msvcrt
                lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def _download(url, destination, report, max_bytes=600_000_000):
    request = urllib.request.Request(url,headers={'User-Agent':'Bookroom/1.0 Calibre setup'})
    report('Downloading from '+urllib.parse.urlsplit(url).netloc+'…')
    with urllib.request.urlopen(request,timeout=45) as response, destination.open('wb') as output:
        if urllib.parse.urlsplit(response.geturl()).scheme!='https': raise ValueError('Calibre download did not use HTTPS')
        total = int(response.headers.get('Content-Length',0))
        if total>max_bytes: raise ValueError('Calibre download exceeds the size limit')
        filename = response.headers.get_filename() or Path(urllib.parse.urlsplit(response.geturl()).path).name
        received = 0; next_report = 25_000_000; deadline = time.monotonic()+INSTALL_TIMEOUT
        while True:
            if time.monotonic()>deadline: raise TimeoutError('Calibre download timed out')
            chunk = response.read(1024*1024)
            if not chunk: break
            received += len(chunk)
            if received>max_bytes: raise ValueError('Calibre download exceeds the size limit')
            output.write(chunk)
            if received>=next_report:
                report(f'Downloaded {received//1_000_000} MB'+(f' of {total//1_000_000} MB' if total else ''))
                next_report=received+25_000_000
        if not received or (total and received!=total): raise ValueError('Incomplete Calibre download; restart Bookroom to retry')
    return filename


def _checksum_context():
    # Calibre's signature server uses its own CA. This public certificate is
    # supplied by the official Linux installer; trust it only for the checksum
    # request, while preserving certificate validation and hostname checks.
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(Path(__file__).with_name('calibre-ca.pem')))
    return context


def _verify_download(path, filename):
    # Use only official filenames to construct the checksum URL.
    if not re.fullmatch(r'calibre(?:-portable-installer)?-[0-9.]+\.(?:exe|dmg)',filename):
        raise ValueError('Unexpected Calibre installer filename')
    name = filename+'.sha512'
    for location in ('signatures/','signatures/old/'):
        try:
            with urllib.request.urlopen('https://code.calibre-ebook.com/'+location+name,timeout=30,context=_checksum_context()) as response:
                raw=response.read(4096).decode('ascii').strip()
            break
        except urllib.error.HTTPError as e:
            if e.code!=404 or location.endswith('old/'): raise
    expected=raw.split()[0] if raw.split() else ''
    if not re.fullmatch('[a-fA-F0-9]{128}',expected): raise ValueError('Invalid Calibre checksum response')
    with path.open('rb') as source:
        actual=hashlib.file_digest(source,'sha512').hexdigest()
    if actual.lower()!=expected.lower(): raise ValueError('Calibre download checksum failed; installer was not run')


def _run(command, log):
    """Capture diagnostics and stop the installer process group on timeout."""
    with log.open('ab') as output:
        process=subprocess.Popen([str(v) for v in command],stdout=output,stderr=subprocess.STDOUT,env=_environment(),start_new_session=os.name!='nt')
        try:
            code=process.wait(timeout=INSTALL_TIMEOUT)
        except BaseException:
            if process.poll() is None:
                if os.name!='nt': os.killpg(process.pid,signal.SIGKILL)
                else: process.kill()
            process.wait(); raise
    if code:
        with log.open('rb') as f:
            f.seek(max(0,log.stat().st_size-2000)); detail=f.read().decode('utf-8','replace').strip()
        raise RuntimeError(f'Calibre setup exited with code {code}. {detail}')


def _install_payload(stage, system, log, report):
    payload=stage/'payload'; payload.mkdir()
    if system=='Linux':
        installer=stage/'linux-installer.sh'
        _download(LINUX_INSTALLER,installer,report,max_bytes=2_000_000)
        report('Installing a private Calibre runtime (the official installer verifies the archive)…')
        _run(['sh',installer,f'install_dir={payload}','isolated=y'],log)
        return payload/'calibre', Path('ebook-convert')
    if system=='Windows':
        installer=stage/'calibre-portable-installer.exe'
        filename=_download(DOWNLOADS[system],installer,report)
        _verify_download(installer,filename)
        report('Installing Calibre Portable…')
        # Official portable installer accepts the complete destination directory
        # as its sole argument, without a UAC/admin installation.
        target=payload/'calibre'
        _run([installer,target],log)
        return target,Path('Calibre/ebook-convert.exe')
    if system=='Darwin':
        installer=stage/'calibre.dmg'
        filename=_download(DOWNLOADS[system],installer,report)
        _verify_download(installer,filename)
        mount=stage/'mounted'; mount.mkdir()
        report('Copying the Calibre application into the private runtime…')
        _run(['hdiutil','attach','-readonly','-nobrowse','-mountpoint',mount,installer],log)
        try:
            _run(['ditto',mount/'calibre.app',payload/'calibre.app'],log)
        finally:
            _run(['hdiutil','detach',mount],log)
        return payload,Path('calibre.app/Contents/MacOS/ebook-convert')
    raise RuntimeError(f'Automatic Calibre installation is not supported on {system}. Install Calibre manually and pass --converter.')


def prepare_converter(data_dir, explicit=None, auto_install=True, report=print):
    """Resolve a working engine, installing once if needed; keep failures nonfatal."""
    data_dir=Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True,exist_ok=True)
    system=platform.system()
    if explicit:
        found=working_converter(explicit)
        if not found: raise ValueError(f'Configured converter could not run: {explicit}. Check --converter or BOOKROOM_CONVERTER.')
        _remember(data_dir,found)
        return Converter(found)
    found=find_converter(data_dir,system)
    if found:
        _remember(data_dir,found)
        report('Using Calibre: '+found)
        return Converter(found)
    if not auto_install: return Converter(error='Calibre was not found. Automatic installation was disabled; restart without --no-install-calibre to enable conversion.')
    log=data_dir/'calibre-install.log'
    try:
        report('Calibre was not found. Setting up conversion automatically; the first launch may take several minutes.')
        with _install_lock(data_dir):
            # Another startup may have installed it while we waited.
            found=find_converter(data_dir,system)
            if found: return Converter(found)
            runtime=data_dir/'runtime'; runtime.mkdir(exist_ok=True)
            log.write_text('Bookroom automatic Calibre setup\n')
            with tempfile.TemporaryDirectory(prefix='.calibre-setup-',dir=runtime) as scratch:
                stage=Path(scratch)
                payload,relative=_install_payload(stage,system,log,report)
                if not working_converter(payload/relative): raise RuntimeError('Installed ebook-convert could not run. See the installation log for missing system libraries or OS compatibility errors.')
                target=runtime/'calibre'; backup=stage/'previous-runtime'
                if target.exists(): target.rename(backup)
                try: payload.rename(target)
                except BaseException:
                    if backup.exists(): backup.rename(target)
                    raise
                found=str(target/relative)
                _remember(data_dir,found)
            report('Calibre is ready. Future launches will reuse this installation.')
            return Converter(found)
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
        message=f'Automatic Calibre setup failed: {e}. Restart Bookroom to retry, or install Calibre manually and use --converter. Log: {log}'
        report(message)
        return Converter(error=message)
