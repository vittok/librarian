"""Installer coverage without network access or system installation."""
from contextlib import contextmanager
from email.message import Message
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time

import pytest

from bookroom import calibre


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a,**kw): raise AssertionError('Test must not access the network')
    monkeypatch.setattr(calibre.urllib.request,'urlopen',deny)


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(calibre.platform,'system',lambda:'Linux')
    monkeypatch.setattr(calibre,'working_converter',lambda p: str(Path(p).resolve()) if p and Path(p).is_file() else None)
    monkeypatch.setattr(calibre,'_candidates',lambda data_dir,system:[data_dir/'runtime/calibre/ebook-convert'])
    return tmp_path/'catalogue'


def payload(stage,system,log,report):
    folder=stage/'payload'; folder.mkdir()
    (folder/'ebook-convert').write_text('working engine')
    return folder,Path('ebook-convert')


def test_installs_then_reuses_without_network(isolated,monkeypatch):
    calls=[]
    def install(*args): calls.append(True); return payload(*args)
    monkeypatch.setattr(calibre,'_install_payload',install)
    reports=[]
    result=calibre.prepare_converter(isolated,report=reports.append)
    assert result.path==str(isolated/'runtime/calibre/ebook-convert')
    assert Path(result.path).read_text()=='working engine' and not result.error
    assert json.loads((isolated/'settings.json').read_text())['converter']==result.path
    again=calibre.prepare_converter(isolated,report=reports.append)
    assert again.path==result.path and len(calls)==1
    assert not list((isolated/'runtime').glob('.calibre-setup-*'))


def test_explicit_converter_remembered_and_settings_preserved(isolated,tmp_path):
    isolated.mkdir(); (isolated/'settings.json').write_text('{"theme":"green"}')
    existing=tmp_path/'custom engine';existing.write_text('works')
    result=calibre.prepare_converter(isolated,explicit=existing)
    assert result.path==str(existing)
    assert json.loads((isolated/'settings.json').read_text())=={'theme':'green','converter':str(existing)}


def test_invalid_explicit_override_does_not_install(isolated):
    with pytest.raises(ValueError,match='Configured converter could not run'):
        calibre.prepare_converter(isolated,explicit=isolated/'missing')


def test_disabled_install_leaves_catalogue_available(isolated):
    result=calibre.prepare_converter(isolated,auto_install=False)
    assert result.path is None and 'disabled' in result.error
    assert not (isolated/'runtime').exists()


def test_failure_is_nonfatal_and_next_start_retries(isolated,monkeypatch):
    calls=[]
    def install(*args):
        calls.append(True)
        if len(calls)==1: raise OSError('network unreachable')
        return payload(*args)
    monkeypatch.setattr(calibre,'_install_payload',install)
    result=calibre.prepare_converter(isolated,report=lambda _:None)
    assert result.path is None and 'network unreachable' in result.error
    assert 'calibre-install.log' in result.error
    assert not list((isolated/'runtime').iterdir())
    assert calibre.prepare_converter(isolated,report=lambda _:None).path


def test_failed_validation_preserves_old_runtime(isolated,monkeypatch):
    previous=isolated/'runtime/calibre';previous.mkdir(parents=True)
    (previous/'keep.txt').write_text('old runtime')
    def broken(stage,*args):
        folder=stage/'payload';folder.mkdir();return folder,Path('ebook-convert')
    monkeypatch.setattr(calibre,'_install_payload',broken)
    result=calibre.prepare_converter(isolated,report=lambda _:None)
    assert result.path is None and 'could not run' in result.error
    assert (previous/'keep.txt').read_text()=='old runtime'


def test_concurrent_starts_only_install_once(isolated,monkeypatch):
    calls=[]; results=[]
    def install(*args):
        calls.append(True);time.sleep(.1);return payload(*args)
    monkeypatch.setattr(calibre,'_install_payload',install)
    def start(): results.append(calibre.prepare_converter(isolated,report=lambda _:None))
    threads=[threading.Thread(target=start) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join(timeout=5)
    assert len(results)==2 and len(calls)==1
    assert results[0].path==results[1].path


def test_detects_saved_path_and_standard_locations(monkeypatch,tmp_path):
    (tmp_path/'settings.json').write_text('{"converter":"/custom/ebook-convert"}')
    monkeypatch.setattr(calibre,'working_converter',lambda p: str(p) if str(p)=='/custom/ebook-convert' else None)
    assert calibre.find_converter(tmp_path,'Linux')=='/custom/ebook-convert'
    monkeypatch.setattr(calibre,'working_converter',lambda p: str(p) if str(p)=='/Applications/calibre.app/Contents/MacOS/ebook-convert' else None)
    assert calibre.find_converter(tmp_path,'Darwin')=='/Applications/calibre.app/Contents/MacOS/ebook-convert'
    monkeypatch.setenv('ProgramFiles',str(tmp_path/'Program Files'))
    windows=tmp_path/'Program Files/Calibre2/ebook-convert.exe'
    monkeypatch.setattr(calibre,'working_converter',lambda p: str(p) if str(p)==str(windows) else None)
    assert calibre.find_converter(tmp_path,'Windows')==str(windows)


def test_linux_uses_official_isolated_installer(monkeypatch,tmp_path):
    downloaded=[];commands=[]
    monkeypatch.setattr(calibre,'_download',lambda url,path,report,**kw:downloaded.append(url))
    monkeypatch.setattr(calibre,'_run',lambda cmd,log:commands.append(cmd))
    target,relative=calibre._install_payload(tmp_path,'Linux',tmp_path/'log',lambda _:None)
    assert downloaded==[calibre.LINUX_INSTALLER]
    assert commands==[['sh',tmp_path/'linux-installer.sh',f'install_dir={tmp_path / "payload"}','isolated=y']]
    assert target==tmp_path/'payload/calibre' and str(relative)=='ebook-convert'


@pytest.mark.parametrize('system', ['Windows','Darwin'])
def test_platform_install_commands_verify_before_run(monkeypatch,tmp_path,system):
    events=[]
    monkeypatch.setattr(calibre,'_download',lambda url,path,report: events.append(('download',url)) or 'installer')
    monkeypatch.setattr(calibre,'_verify_download',lambda path,name:events.append(('verify',name)))
    monkeypatch.setattr(calibre,'_run',lambda cmd,log:events.append(('run',cmd)))
    target,relative=calibre._install_payload(tmp_path,system,tmp_path/'log',lambda _:None)
    assert events[0]==('download',calibre.DOWNLOADS[system])
    assert events[1][0]=='verify' and events[2][0]=='run'
    if system=='Windows':
        assert events[2][1]==[tmp_path/'calibre-portable-installer.exe',tmp_path/'payload/calibre']
        assert str(relative)=='Calibre/ebook-convert.exe'
    else:
        assert events[2][1][:4]==['hdiutil','attach','-readonly','-nobrowse']
        assert events[-1][1][:2]==['hdiutil','detach']
        assert str(relative)=='calibre.app/Contents/MacOS/ebook-convert'


def test_macos_detaches_on_copy_failure(monkeypatch,tmp_path):
    commands=[]
    monkeypatch.setattr(calibre,'_download',lambda *args:'installer')
    monkeypatch.setattr(calibre,'_verify_download',lambda *args:None)
    def run(cmd,log):
        commands.append(cmd)
        if cmd[0]=='ditto':raise RuntimeError('disk full')
    monkeypatch.setattr(calibre,'_run',run)
    with pytest.raises(RuntimeError,match='disk full'):
        calibre._install_payload(tmp_path,'Darwin',tmp_path/'log',lambda _:None)
    assert commands[-1][:2]==['hdiutil','detach']


class Response:
    def __init__(self,data,filename='calibre-9.17.0.dmg',length=None):
        self.data=data;self.offset=0;self.headers=Message()
        self.headers['Content-Length']=str(len(data) if length is None else length)
        self.headers['Content-Disposition']=f'attachment; filename="{filename}"'
    def geturl(self):return 'https://download.calibre-ebook.com/file'
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):
        data=self.data[self.offset:self.offset+n];self.offset+=len(data);return data


def test_checksums_reject_tampered_download(monkeypatch,tmp_path):
    path=tmp_path/'calibre.dmg';path.write_bytes(b'original')
    checksum=hashlib.sha512(b'original').hexdigest().encode()
    monkeypatch.setattr(calibre.urllib.request,'urlopen',lambda *a,**kw:Response(checksum))
    calibre._verify_download(path,'calibre-9.17.0.dmg')
    path.write_bytes(b'tampered')
    with pytest.raises(ValueError,match='checksum failed'): calibre._verify_download(path,'calibre-9.17.0.dmg')
    with pytest.raises(ValueError,match='filename'): calibre._verify_download(path,'../anything.exe')


def test_download_detects_truncation_and_cleans_up_via_staging(monkeypatch,tmp_path):
    monkeypatch.setattr(calibre.urllib.request,'urlopen',lambda *a,**kw:Response(b'short',length=100))
    with pytest.raises(ValueError,match='Incomplete'):
        calibre._download('https://calibre-ebook.com/dist/osx',tmp_path/'file',lambda _:None)


def test_download_returns_filename_and_limits_size(monkeypatch,tmp_path):
    monkeypatch.setattr(calibre.urllib.request,'urlopen',lambda *a,**kw:Response(b'content'))
    assert calibre._download('https://calibre-ebook.com/dist/osx',tmp_path/'file',lambda _:None)=='calibre-9.17.0.dmg'
    assert (tmp_path/'file').read_bytes()==b'content'
    with pytest.raises(ValueError,match='size limit'):
        calibre._download('https://calibre-ebook.com/dist/osx',tmp_path/'file',lambda _:None,max_bytes=3)


def test_probe_handles_nonworking_binary(monkeypatch,tmp_path):
    path=tmp_path/'ebook-convert';path.write_text('test');path.chmod(0o755)
    monkeypatch.setattr(calibre.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,stdout=b'ebook-convert (calibre 9.17.0)',stderr=b''))
    assert calibre.working_converter(path)==str(path)
    monkeypatch.setattr(calibre.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,1,stdout=b'',stderr=b'error'))
    assert calibre.working_converter(path) is None


def test_scrubs_linux_source_override(monkeypatch):
    monkeypatch.setenv('CALIBRE_INSTALLER_LOCAL_URL','https://unrelated.invalid/file')
    assert 'CALIBRE_INSTALLER_LOCAL_URL' not in calibre._environment()


def test_checksum_tls_context_preserves_validation():
    import ssl
    context=calibre._checksum_context()
    assert context.verify_mode==ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()['x509_ca']>=1


@pytest.mark.parametrize('system',['Windows','Darwin'])
def test_bad_checksum_never_runs_installer(monkeypatch,tmp_path,system):
    commands=[]
    monkeypatch.setattr(calibre,'_download',lambda *args:'installer')
    def reject(*args):raise ValueError('checksum failed')
    monkeypatch.setattr(calibre,'_verify_download',reject)
    monkeypatch.setattr(calibre,'_run',lambda *args:commands.append(args))
    with pytest.raises(ValueError,match='checksum failed'):
        calibre._install_payload(tmp_path,system,tmp_path/'log',lambda _:None)
    assert commands==[]


@pytest.mark.parametrize('disabled',[False,True])
def test_server_startup_uses_bootstrap_and_preserves_error(monkeypatch,tmp_path,disabled):
    from bookroom import server
    calls=[];libraries=[]
    def prepare(data_dir,**kw):calls.append(kw);return calibre.Converter(error='offline setup failed')
    class FakeLibrary:
        db_path=tmp_path/'library.sqlite3'
        def __init__(self,*args):libraries.append(self)
        def close(self):self.closed=True
    class FakeServer:
        server_port=9876
        def serve_forever(self):pass
        def server_close(self):pass
    monkeypatch.setattr(server,'prepare_converter',prepare)
    monkeypatch.setattr(server,'Library',FakeLibrary)
    monkeypatch.setattr(server,'make_server',lambda *args:FakeServer())
    monkeypatch.delenv('BOOKROOM_CONVERTER',raising=False)
    monkeypatch.setattr('sys.argv',['bookroom','--no-browser','--data-dir',str(tmp_path)]+(['--no-install-calibre'] if disabled else []))
    server.main()
    assert calls[0]['auto_install'] is (not disabled)
    assert libraries[0].converter is None and libraries[0].converter_error=='offline setup failed'
    assert libraries[0].closed


def test_empty_checksum_is_a_handled_failure(monkeypatch,tmp_path):
    path=tmp_path/'calibre.dmg';path.write_bytes(b'data')
    monkeypatch.setattr(calibre.urllib.request,'urlopen',lambda *a,**kw:Response(b''))
    with pytest.raises(ValueError,match='checksum response'):
        calibre._verify_download(path,'calibre-9.17.0.dmg')
