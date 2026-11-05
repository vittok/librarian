"""Network location tests use simulated mounts and disposable local books."""
import json
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from bookroom import locations
from bookroom.core import Library
from bookroom.server import make_server


@pytest.mark.parametrize('value',[
    r'\\NAS\Books\Science Fiction', '//NAS/Books/Science Fiction',
    'smb://NAS/Books/Science%20Fiction', 'cifs://NAS/Books/Science%20Fiction',
    'file://NAS/Books/Science%20Fiction',r'\\?\UNC\NAS\Books\Science Fiction'])
def test_network_spellings_are_equivalent(value):
    remote=locations.parse_location(value)
    assert remote==locations.Share('nas','Books',('Science Fiction',))
    assert remote.uri=='smb://nas/Books/Science%20Fiction'
    assert remote.unc==r'\\nas\Books\Science Fiction'


def test_local_file_urls_and_windows_drives(tmp_path):
    path=tmp_path/'Book shelf'
    assert locations.parse_location(path.as_uri())==str(path)
    assert locations.parse_location('file://localhost'+urllib.parse.quote(str(path)))==str(path)
    assert locations.parse_location('file:///C:/Book%20shelf',system='Windows')=='C:/Book shelf'
    assert locations.parse_location(r'Z:\Book shelf',system='Windows')==r'Z:\Book shelf'
    assert locations.parse_location(r'\\server\books\100%20literal').parts==('100%20literal',)


@pytest.mark.parametrize('value',[
    'smb://user:password@server/books','file://user:password@server/books',
    'smb://server','smb://server/books/../private','smb://server/books/%2e%2e/private',
    'smb://server:1445/books','file://server:1445/books','smb://[::1]/books','smb://server/books?password=secret',
    'smb://server/books#fragment','https://server/books','file:relative','',r'\\.\pipe',
    'smb://server/books/%00bad'])
def test_invalid_or_credential_bearing_locations_are_rejected(value):
    with pytest.raises(ValueError) as error: locations.parse_location(value)
    assert 'password@' not in str(error.value) and 'password=secret' not in str(error.value)


@pytest.fixture
def share(tmp_path,monkeypatch):
    path=tmp_path/'mounted';path.mkdir()
    monkeypatch.setattr(locations.platform,'system',lambda:'Linux')
    mounts=[locations.Mount(path,locations.Share('nas','Books'))]
    monkeypatch.setattr(locations,'mounted_shares',lambda:mounts)
    return path,mounts


def test_smb_resolves_to_connected_mount_and_native_path_keeps_uri(share):
    path,_=share
    location=locations.resolve_location('smb://NAS/books/Science%20Fiction')
    assert location.path==path/'Science Fiction'
    assert location.boundary==path
    native=locations.resolve_location(str(path/'Science Fiction'))
    assert native.uri=='smb://nas/Books/Science%20Fiction'
    assert native.child('A # book')=='smb://nas/Books/Science%20Fiction/A%20%23%20book'
    assert native.parent=='smb://nas/Books'
    root=locations.resolve_location('smb://nas/Books')
    assert root.parent==root.value


def test_windows_unc_resolution_does_not_require_mapped_drive(monkeypatch):
    monkeypatch.setattr(locations.platform,'system',lambda:'Windows')
    location=locations.resolve_location('smb://NAS/Books/Fiction')
    assert str(location.path)==r'\\nas\Books\Fiction'
    assert location.value=='smb://nas/Books/Fiction'


def test_unconnected_share_has_actionable_error(share):
    _,mounts=share;mounts.clear()
    with pytest.raises(ValueError,match='not connected'):
        locations.resolve_location('smb://nas/Books')


def test_folder_browser_encodes_children_and_stays_within_share(share):
    path,_=share;(path/'Science #1').mkdir();(path/'.hidden').mkdir();(path/'book.txt').write_text('book')
    listing=locations.list_folders('smb://nas/Books')
    assert listing['path']=='smb://nas/Books' and listing['parent']==listing['path']
    assert listing['children']==[{'name':'Science #1','path':'smb://nas/Books/Science%20%231'}]
    assert locations.list_folders(listing['children'][0]['path'])['children']==[]
    assert any(row['path']=='smb://nas/Books' for row in locations.folder_locations())


def test_linux_mountinfo_preserves_spaces_and_skips_bad_entries():
    text='''bad input
1 2 0:1 / /mnt/bad rw - cifs bad-source rw
3 2 0:2 / /mnt/Book\\040shelf rw - cifs //nas/Books rw
4 2 0:3 / /mnt/archive rw - nfs4 archive:/data rw
'''
    mounts=locations.parse_mountinfo(text)
    assert mounts==[locations.Mount(Path('/mnt/Book shelf'),locations.Share('nas','Books')),locations.Mount(Path('/mnt/archive'))]


def test_mounting_subdirectory_uses_longest_matching_prefix(tmp_path,monkeypatch):
    monkeypatch.setattr(locations.platform,'system',lambda:'Linux')
    monkeypatch.setattr(locations,'mounted_shares',lambda:[locations.Mount(tmp_path/'all',locations.Share('nas','Books')),locations.Mount(tmp_path/'subset',locations.Share('nas','Books',('Fiction',)))])
    loc=locations.resolve_location('smb://nas/Books/Fiction/Classic')
    assert loc.path==tmp_path/'subset/Classic'
    assert loc.parent=='smb://nas/Books/Fiction'


def test_gvfs_discovery_does_not_store_login_names(tmp_path,monkeypatch):
    mount=tmp_path/'smb-share:domain=OFFICE,server=NAS,share=Book%20shelf,user=reader';mount.mkdir()
    monkeypatch.setattr(locations.platform,'system',lambda:'Linux')
    monkeypatch.setattr(locations,'_gvfs_roots',lambda:[tmp_path])
    mounts=locations.mounted_shares()
    match=next(m for m in mounts if m.path==mount)
    assert match.remote==locations.Share('nas','Book shelf')
    assert 'reader' not in match.remote.uri and 'OFFICE' not in match.remote.uri


def test_macos_mount_source_and_escaped_mountpoint(monkeypatch):
    monkeypatch.setattr(locations.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(locations.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,stdout='//reader@NAS/Book%20shelf on /Volumes/Book\\040shelf (smbfs, nodev, mounted by reader)\n'))
    assert locations.mounted_shares()==[locations.Mount(Path('/Volumes/Book shelf'),locations.Share('nas','Book shelf'))]


def wait_job(lib,jid):
    for _ in range(500):
        job=next(j for j in lib.jobs() if j['id']==jid)
        if job['status'] not in {'queued','running'}:return job
        time.sleep(.01)
    raise AssertionError('Job timed out')


def test_network_import_search_rescan_and_disconnect_preserve_catalogue(share,tmp_path):
    path,mounts=share;(path/'Book.txt').write_text('A network cartography collection')
    lib=Library(tmp_path/'catalogue')
    try:
        assert wait_job(lib,lib.scan('smb://NAS/Books')['job_id'])['status']=='completed'
        assert lib.catalogue(q='cartography')['total']==1
        root=lib.stats()['roots'][0]
        assert root['location']=='smb://nas/Books'
        (path/'Second.txt').write_text('Another network book')
        assert wait_job(lib,lib.scan(root['location'])['job_id'])['errors']==0
        assert lib.catalogue()['total']==2
        mounts.clear()
        with pytest.raises(ValueError,match='not connected'):lib.scan(root['location'])
        assert lib.catalogue()['total']==2
    finally:lib.close()


def test_failed_network_walk_is_logged_and_does_not_clear_books(share,tmp_path,monkeypatch):
    path,_=share;(path/'Book.txt').write_text('available')
    lib=Library(tmp_path/'catalogue')
    try:
        lib.index_file(path/'Book.txt')
        def failed_walk(root,**kwargs):
            kwargs['onerror'](PermissionError('network is offline'));return iter(())
        monkeypatch.setattr('bookroom.core.os.walk',failed_walk)
        job=wait_job(lib,lib.scan('smb://nas/Books')['job_id'])
        assert job['status']=='failed' and job['errors']==1
        assert 'Reconnect' in job['log'][-1]['message']
        assert lib.catalogue()['total']==1
    finally:lib.close()


def test_network_destination_dropped_after_preview_is_not_written_locally(share,tmp_path):
    path,mounts=share
    source=tmp_path/'local.txt';source.write_text('keep original')
    lib=Library(tmp_path/'catalogue')
    try:
        bid=lib.index_file(source)
        plan=lib.plan({'kind':'copy','ids':[bid],'destination':'smb://nas/Books/Organized'})
        mounts.clear()
        job=wait_job(lib,lib.execute(plan['plan_id'])['job_id'])
        assert job['errors']==1 and source.is_file()
        assert not (path/'Organized').exists()
    finally:lib.close()


def test_existing_catalogue_root_schema_migrates(tmp_path):
    with sqlite3.connect(tmp_path/'library.sqlite3') as db:
        db.execute('CREATE TABLE roots(path TEXT PRIMARY KEY,scanned REAL)')
        db.execute('INSERT INTO roots VALUES(?,?)',('/old/books',123))
    lib=Library(tmp_path)
    try:assert lib.stats()['roots']==[{'path':'/old/books','scanned':123.0,'location':''}]
    finally:lib.close()


def test_http_network_browse_import_and_missing_share(share,tmp_path):
    path,mounts=share;(path/'Book.txt').write_text('book')
    lib=Library(tmp_path/'catalogue');server=make_server(lib,0)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:token=response.read().decode().split('name="bookroom-token" content="')[1].split('"')[0]
        def api(route,data=None):
            request=urllib.request.Request(url+'/api/'+route,headers={'X-Bookroom-Token':token,'Content-Type':'application/json'},data=json.dumps(data).encode() if data else None)
            with urllib.request.urlopen(request) as response:return json.load(response)
        assert any(row['path']=='smb://nas/Books' for row in api('folder-locations'))
        listing=api('folders?'+urllib.parse.urlencode({'path':r'\\NAS\Books'}))
        assert listing['path']=='smb://nas/Books'
        assert wait_job(lib,api('scan',{'folder':r'\\NAS\Books'})['job_id'])['errors']==0
        mounts.clear()
        with pytest.raises(urllib.error.HTTPError) as error:api('folders?'+urllib.parse.urlencode({'path':'smb://nas/Books'}))
        assert error.value.code==400
    finally:server.shutdown();server.server_close();lib.close()
