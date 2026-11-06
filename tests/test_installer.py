"""Standalone installer tests never write to the user's actual install location."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

spec=importlib.util.spec_from_file_location('bookroom_install',Path(__file__).resolve().parents[1]/'install.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
SHA='a'*40


def make_archive(path,files=None,special=None):
    files=files or {'repository/pyproject.toml':b'[project]\nname="bookroom-local"','repository/bookroom/__init__.py':b'__version__="test"'}
    with tarfile.open(path,'w:gz') as archive:
        for name,data in files.items():
            member=tarfile.TarInfo(name);member.size=len(data);archive.addfile(member,io.BytesIO(data))
        if special:archive.addfile(special)


@pytest.fixture
def fake_install(monkeypatch,tmp_path):
    archive=tmp_path/'source.tar.gz';make_archive(archive)
    monkeypatch.setattr(installer,'resolve_ref',lambda ref:SHA)
    monkeypatch.setattr(installer,'download_archive',lambda commit,path:path.write_bytes(archive.read_bytes()))
    commands=[]
    def run(command,**kwargs):
        commands.append([str(v) for v in command])
        if 'venv' in command:
            root=Path(command[-1]);binary=root/('Scripts/python.exe' if installer.os.name=='nt' else 'bin/python')
            binary.parent.mkdir(parents=True);binary.write_text('test executable')
    monkeypatch.setattr(installer,'run',run)
    return tmp_path/'app',tmp_path/'bin',commands


def test_success_creates_launcher_and_manifest(fake_install):
    prefix,bin_dir,commands=fake_install
    python,launcher=installer.install(prefix,bin_dir)
    assert launcher.is_file() and installer.MARKER in launcher.read_text()
    assert '-I -m bookroom' in launcher.read_text()
    metadata=json.loads((prefix/installer.MANIFEST).read_text())
    assert metadata['commit']==SHA and metadata['repository']==installer.REPOSITORY
    assert python.is_relative_to(Path(metadata['release']))
    assert any(command[-1].endswith('[pdf]') for command in commands)
    assert any('fts5' in command[-1] for command in commands)


def test_update_keeps_previous_release_and_uses_new_environment(fake_install):
    prefix,bin_dir,commands=fake_install
    first,launcher=installer.install(prefix,bin_dir)
    second,_=installer.install(prefix,bin_dir,pdf=False)
    assert first!=second and first.is_file() and second.is_file()
    assert str(second) in launcher.read_text()
    assert str(first) not in launcher.read_text()
    assert len(list((prefix/'releases').iterdir()))==2


def test_failed_update_preserves_working_launcher(fake_install,monkeypatch):
    prefix,bin_dir,commands=fake_install
    python,launcher=installer.install(prefix,bin_dir)
    old_launcher=launcher.read_bytes();old_manifest=(prefix/installer.MANIFEST).read_bytes()
    def fail(command,**kwargs):raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(installer,'run',fail)
    with pytest.raises(installer.InstallError,match='virtual environment'):installer.install(prefix,bin_dir)
    assert launcher.read_bytes()==old_launcher and (prefix/installer.MANIFEST).read_bytes()==old_manifest
    assert python.is_file() and len(list((prefix/'releases').iterdir()))==1


def test_refuses_unrelated_directory_or_launcher(fake_install):
    prefix,bin_dir,commands=fake_install
    prefix.mkdir();(prefix/'keep.txt').write_text('do not replace')
    with pytest.raises(installer.InstallError,match='not empty'):installer.install(prefix,bin_dir)
    assert (prefix/'keep.txt').read_text()=='do not replace'
    (prefix/'keep.txt').unlink();bin_dir.mkdir();launcher=bin_dir/('bookroom.cmd' if installer.os.name=='nt' else 'bookroom');launcher.write_text('another command')
    with pytest.raises(installer.InstallError,match='unrelated command'):installer.install(prefix,bin_dir)
    assert launcher.read_text()=='another command'


def test_refuses_launcher_symlink(fake_install,tmp_path):
    prefix,bin_dir,commands=fake_install
    bin_dir.mkdir();target=tmp_path/'keep';target.write_text('keep')
    (bin_dir/'bookroom').symlink_to(target)
    with pytest.raises(installer.InstallError,match='symlink'):installer.install(prefix,bin_dir)
    assert target.read_text()=='keep'


def test_extract_valid_archive(tmp_path):
    archive=tmp_path/'source.tar.gz';make_archive(archive)
    output=tmp_path/'out';output.mkdir();installer.extract_source(archive,output)
    assert (output/'pyproject.toml').is_file() and (output/'bookroom/__init__.py').is_file()


@pytest.mark.parametrize('bad_name',['repository/../../outside','/absolute/escape','repository/a\\..\\escape','repository/C:/escape'])
def test_extract_rejects_unsafe_paths(tmp_path,bad_name):
    archive=tmp_path/'source.tar.gz';make_archive(archive,{bad_name:b'data'})
    output=tmp_path/'out';output.mkdir()
    with pytest.raises(installer.InstallError,match='unsafe path'):installer.extract_source(archive,output)


@pytest.mark.parametrize('kind',[tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.FIFOTYPE])
def test_extract_rejects_links_and_special_files(tmp_path,kind):
    member=tarfile.TarInfo('repository/link');member.type=kind;member.linkname='/outside'
    archive=tmp_path/'source.tar.gz';make_archive(archive,special=member)
    output=tmp_path/'out';output.mkdir()
    with pytest.raises(installer.InstallError,match='special file'):installer.extract_source(archive,output)


def test_pinned_commit_does_not_need_github_ref_api(monkeypatch):
    def fail(*args):raise AssertionError('Pinned commit should not require a ref lookup')
    monkeypatch.setattr(installer,'request',fail)
    assert installer.resolve_ref(SHA.upper())==SHA


def test_resolves_branch_to_immutable_commit(monkeypatch):
    requested=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps({'sha':SHA}).encode()
    def request(url):requested.append(url);return Response()
    monkeypatch.setattr(installer,'request',request)
    assert installer.resolve_ref('feature/test')==SHA
    assert requested[0].endswith('/commits/feature%2Ftest')


def test_launcher_quotes_paths_with_spaces_and_metacharacters(tmp_path):
    python=tmp_path/"some ' quoted $path"/'python'
    assert installer.launcher_text(python).endswith(' -I -m bookroom "$@"\n')
    assert installer.MARKER in installer.launcher_text(python)
    assert '%%username%%' in installer.launcher_text(Path('C:/%username%/python.exe'),windows=True)


def test_shell_wrapper_passes_arguments_and_cleans_tempfiles(tmp_path):
    root=Path(__file__).resolve().parents[1]
    fake_bin=tmp_path/'fake-bin';fake_bin.mkdir()
    payload=tmp_path/'payload.py';payload.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    curl=fake_bin/'curl';curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n if [ "$1" = "--output" ]; then shift; cp "$BOOKROOM_TEST_PAYLOAD" "$1"; exit 0; fi\n shift\ndone\nexit 1\n');curl.chmod(0o755)
    import os
    environment={**os.environ,'PATH':str(fake_bin)+os.pathsep+os.environ['PATH'],'BOOKROOM_PYTHON':sys.executable,'BOOKROOM_TEST_PAYLOAD':str(payload),'TMPDIR':str(tmp_path)}
    result=subprocess.run(['sh',str(root/'install.sh'),'--prefix',str(tmp_path/'A spaced path'),'--run','--','--port','9876'],capture_output=True,text=True,env=environment,check=True)
    assert json.loads(result.stdout)==['--prefix',str(tmp_path/'A spaced path'),'--run','--','--port','9876']
    assert not list(tmp_path.glob('bookroom-bootstrap.*'))


def test_shell_wrapper_rejects_invalid_python_override(tmp_path):
    root=Path(__file__).resolve().parents[1]
    import os
    result=subprocess.run(['sh',str(root/'install.sh')],capture_output=True,text=True,env={**os.environ,'BOOKROOM_PYTHON':'/not/an/interpreter'})
    assert result.returncode==1 and 'Python 3.11+' in result.stderr


def test_cli_forwards_app_options_only_when_run_requested(fake_install,monkeypatch):
    prefix,bin_dir,commands=fake_install
    calls=[]
    monkeypatch.setattr(installer.os,'execv',lambda python,args:calls.append((python,args)))
    installer.main(['--prefix',str(prefix),'--bin-dir',str(bin_dir),'--run','--','--port','8766','--no-browser'])
    assert calls[0][1][-3:]==['--port','8766','--no-browser']
    with pytest.raises(SystemExit) as error:installer.main(['--','--port','8766'])
    assert error.value.code==2


def test_missing_ensurepip_uses_official_bootstrap_only_in_private_env(monkeypatch,tmp_path):
    python=tmp_path/'venv/bin/python';commands=[];urls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def geturl(self):return 'https://bootstrap.pypa.io/get-pip.py'
        def read(self,n):return b'# test pip bootstrap\n'
    def request(url):urls.append(url);return Response()
    def run(command,**kwargs):
        commands.append(command)
        if 'ensurepip' in command:raise subprocess.CalledProcessError(1,command)
        assert Path(command[2]).read_text()=='# test pip bootstrap\n'
    monkeypatch.setattr(installer,'request',request)
    monkeypatch.setattr(installer,'run',run)
    installer.ensure_pip(python)
    assert urls==['https://bootstrap.pypa.io/get-pip.py']
    assert all(command[0]==python for command in commands)
    assert not Path(commands[-1][2]).exists()


def test_builtin_ensurepip_does_not_download_bootstrap(monkeypatch,tmp_path):
    monkeypatch.setattr(installer,'run',lambda *a,**kw:None)
    def fail(*args):raise AssertionError('No network bootstrap is needed')
    monkeypatch.setattr(installer,'request',fail)
    installer.ensure_pip(tmp_path/'venv/bin/python')
