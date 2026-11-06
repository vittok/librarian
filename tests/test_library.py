import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import zipfile

import pytest

from bookroom.core import Library, extract
from bookroom.server import make_server


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path/'data')
    yield lib
    lib.close()


def make_book(tmp_path, name='Book.txt', body='the secret word cartography'):
    path = tmp_path/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(body)
    return path


def wait_job(lib, jid):
    for _ in range(500):
        job = next(j for j in lib.jobs() if j['id']==jid)
        if job['status'] not in ('queued','running'): return job
        time.sleep(.01)
    raise AssertionError('Job timed out')


def test_scan_search_rescan_metadata_and_duplicates(library,tmp_path):
    folder = tmp_path/'books'; make_book(folder,'book.txt'); make_book(folder,'copy.txt')
    (folder/'ignored.jpg').write_bytes(b'x')
    job = wait_job(library,library.scan(str(folder))['job_id'])
    assert job['done']==2 and job['errors']==0
    assert library.catalogue(q='cartogra')['total']==2
    assert library.catalogue(duplicates=True)['total']==2
    bid = library.catalogue()['books'][0]['id']
    library.update_metadata(bid,dict(title='Atlas of the World',author='A. Writer'))
    assert library.catalogue(q='atlas writer')['total']==1
    assert library.catalogue(q='" OR ** - :')['total']==0
    book = library.get(bid); Path(book['path']).write_text('changed text')
    wait_job(library,library.scan(str(folder))['job_id'])
    assert library.get(bid)['title']=='Atlas of the World'
    assert library.catalogue()['total']==2
    assert library.catalogue(duplicates=True)['total']==0


def test_epub_metadata_and_spine_text(tmp_path):
    path = tmp_path/'test.epub'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr('OEBPS/content.opf','<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>A Better Title</dc:title><dc:creator>The Author</dc:creator></metadata><manifest><item id="one" href="chapter.xhtml"/></manifest><spine><itemref idref="one"/></spine></package>')
        z.writestr('OEBPS/chapter.xhtml','<html><body><h1>Unusual constellation</h1></body></html>')
    title,author,text,status = extract(path)
    assert (title,author)==('A Better Title','The Author')
    assert 'constellation' in text and status=='Text indexed'


def test_copy_preview_collisions_and_single_use(library,tmp_path):
    source=make_book(tmp_path/'in'); bid=library.index_file(source)
    out=tmp_path/'out'; out.mkdir(); (out/'Book.txt').write_text('keep')
    plan=library.plan(dict(kind='copy',ids=[bid],destination=str(out)))
    assert plan['entries'][0]['target'].endswith('Book (2).txt')
    job=wait_job(library,library.execute(plan['plan_id'])['job_id'])
    assert job['status']=='completed'
    assert source.exists() and (out/'Book.txt').read_text()=='keep'
    assert (out/'Book (2).txt').read_text()==source.read_text()
    assert library.stats()['count']==2
    with pytest.raises(ValueError,match='already used'): library.execute(plan['plan_id'])


def test_destination_created_after_preview_is_not_overwritten(library,tmp_path):
    source=make_book(tmp_path/'in'); bid=library.index_file(source)
    plan=library.plan(dict(kind='move',ids=[bid],destination=str(tmp_path/'out')))
    target=Path(plan['entries'][0]['target']); target.parent.mkdir(); target.write_text('external file')
    job=wait_job(library,library.execute(plan['plan_id'])['job_id'])
    assert job['errors']==1 and source.exists()
    assert target.read_text()=='external file'


def test_move_indexes_target_and_preserves_metadata(library,tmp_path):
    source=make_book(tmp_path/'in'); bid=library.index_file(source)
    library.update_metadata(bid,dict(title='Title / with slash',author='../Author',year='1900'))
    plan=library.plan(dict(kind='organize',organize_mode='move',ids=[bid],destination=str(tmp_path/'out')))
    target=Path(plan['entries'][0]['target'])
    assert target.is_relative_to(tmp_path/'out')
    job=wait_job(library,library.execute(plan['plan_id'])['job_id'])
    assert job['errors']==0 and not source.exists() and target.exists()
    books=library.catalogue()['books']; assert len(books)==1
    assert books[0]['title']=='Title / with slash' and books[0]['year']=='1900'


def test_changed_source_rejected_without_removal(library,tmp_path):
    source=make_book(tmp_path/'in'); bid=library.index_file(source)
    plan=library.plan(dict(kind='move',ids=[bid],destination=str(tmp_path/'out')))
    source.write_text('changed')
    job=wait_job(library,library.execute(plan['plan_id'])['job_id'])
    assert job['errors']==1 and source.exists()
    assert not Path(plan['entries'][0]['target']).exists()


def test_conversion_without_engine_gives_actionable_error(library,tmp_path):
    bid=library.index_file(make_book(tmp_path/'in')); library.converter=None
    with pytest.raises(ValueError,match='Install Calibre'):
        library.plan(dict(kind='convert',ids=[bid],formats=['epub'],destination=str(tmp_path/'out')))


def test_conversion_multi_format_and_failure(library,tmp_path):
    import sys
    converter=tmp_path/'converter'
    converter.write_text(f'#!{sys.executable}\nimport pathlib,sys\np=pathlib.Path(sys.argv[2])\nif p.suffix==".pdf": sys.stderr.write("PDF failed");sys.exit(1)\np.write_text("converted book")\n')
    converter.chmod(0o755); library.converter=str(converter)
    source=make_book(tmp_path/'in'); bid=library.index_file(source)
    plan=library.plan(dict(kind='convert',ids=[bid],formats=['txt','epub','pdf'],destination=str(tmp_path/'out')))
    assert len(plan['entries'])==2
    job=wait_job(library,library.execute(plan['plan_id'])['job_id'])
    assert job['done']==2 and job['errors']==1
    assert source.exists() and (tmp_path/'out'/'Book.epub').exists()
    assert not (tmp_path/'out'/'Book.pdf').exists()
    assert not list((tmp_path/'out').glob('.bookroom-*'))


def test_metadata_lookup_mocked_response(library,monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n): return json.dumps({'docs':[dict(key='/works/OL123W',title='Verified title',author_name=['Author'],first_publish_year=1920),dict(key='javascript:bad',title='Bad')]}).encode()
    monkeypatch.setattr(urllib.request,'urlopen',lambda req,timeout:Response())
    matches=library.lookup('Wrong Title')
    assert len(matches)==1 and matches[0]['source']=='https://openlibrary.org/works/OL123W'


def test_http_auth_search_and_csrf(library,tmp_path):
    server=make_server(library,0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:
            html=response.read().decode(); token=html.split('name="bookroom-token" content="')[1].split('"')[0]
            assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(url+'/api/books')
        assert error.value.code==403
        req=urllib.request.Request(url+'/api/stats',headers={'X-Bookroom-Token':token})
        with urllib.request.urlopen(req) as response: assert json.load(response)['count']==0
        req=urllib.request.Request(url+'/api/scan',data=json.dumps({'folder':str(tmp_path)}).encode(),headers={'X-Bookroom-Token':token,'Origin':'https://example.com','Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
        assert error.value.code==403
        req=urllib.request.Request(url+'/',headers={'Host':'evil.example'})
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
        assert error.value.code==403
    finally: server.shutdown(); server.server_close()


def test_missing_sources_are_visible_and_batch_rejected(library,tmp_path):
    path=make_book(tmp_path/'in'); bid=library.index_file(path); path.unlink()
    assert library.catalogue()['total']==1
    with pytest.raises(ValueError,match='missing'): library.plan(dict(kind='copy',ids=[bid],destination=str(tmp_path/'out')))


def test_optional_real_calibre_smoke(library,tmp_path):
    """Run with BOOKROOM_CONVERTER set to an actual Calibre executable."""
    import os
    converter=os.environ.get('BOOKROOM_CONVERTER')
    if not converter:
        pytest.skip('Set BOOKROOM_CONVERTER to run real conversion smoke tests')
    library.converter=converter
    source=make_book(tmp_path/'in',body='A small library contains a cartography collection.')
    bid=library.index_file(source)
    library.update_metadata(bid,dict(title='A Little Atlas',author='Example Author'))
    plan=library.plan(dict(kind='convert',ids=[bid],formats=['epub','pdf','mobi','azw3','docx'],destination=str(tmp_path/'out')))
    jid=library.execute(plan['plan_id'])['job_id']
    for _ in range(1200):
        job=next(j for j in library.jobs() if j['id']==jid)
        if job['status'] not in ('queued','running'): break
        time.sleep(.1)
    assert job['status']=='completed',job
    assert job['done']==5
    assert source.is_file()
    output=tmp_path/'out'
    assert (output/'A Little Atlas.pdf').read_bytes().startswith(b'%PDF')
    title,author,body,status=extract(output/'A Little Atlas.epub')
    assert title=='A Little Atlas' and author=='Example Author'
    assert 'cartography' in body
    assert library.stats()['count']==6
    from bookroom.metadata import inspect_book
    identified=inspect_book(output/'A Little Atlas.mobi',extract(output/'A Little Atlas.mobi'),converter)
    assert identified['title']=='A Little Atlas' and identified['author']=='Example Author'
