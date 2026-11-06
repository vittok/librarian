import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

import pytest

from bookroom.core import Library,extract,signature
from bookroom.metadata import inspect_book,title_page,valid_isbn,isbn13,choose_match,author_score
from bookroom.server import make_server


@pytest.fixture
def library(tmp_path):
    lib=Library(tmp_path/'catalogue');lib.converter=None
    yield lib
    lib.close()


def book(lib,tmp_path,name='file_123.txt',body='Title: Pride and Prejudice\nAuthor: Jane Austen\n\nChapter 1\nA familiar story.'):
    path=tmp_path/name;path.write_text(body)
    return lib.index_file(path)


def wait(lib,jid):
    for _ in range(1000):
        job=next(j for j in lib.jobs() if j['id']==jid)
        if job['status'] not in {'queued','running','cancelling'}:return job
        time.sleep(.01)
    raise AssertionError('Job timed out')


def candidate(title='Pride and Prejudice',author='Jane Austen',key='OL66554W',isbns=None):
    return dict(title=title,author=author,year='1813',source='https://openlibrary.org/works/'+key,isbns=isbns or [])


def identified(title='Pride and Prejudice',author='Jane Austen'):
    c=candidate(title,author)
    return dict(evidence=dict(title=title,author=author,isbn='',strength=.98,source='labelled title page'),candidates=[c],match=c,reason='Title and author agree with the book')


@pytest.mark.parametrize('value,valid',[('9780141439518',True),('0-14-143951-3',True),('9780141439519',False),('0000000000',True),('12345',False)])
def test_isbn_checksums(value,valid):
    assert bool(valid_isbn(value)) is valid
    assert isbn13('0-14-143951-3')=='9780141439518'


def test_identifies_title_and_author_from_contents(library,tmp_path):
    bid=book(library,tmp_path)
    queries=[]
    library.lookup=lambda title,author='',isbn='': queries.append((title,author,isbn)) or [candidate()]
    info=library.identify_book(bid)
    assert info['evidence']['title']=='Pride and Prejudice'
    assert info['evidence']['author']=='Jane Austen'
    assert info['match']['title']=='Pride and Prejudice'
    assert queries==[('Pride and Prejudice','Jane Austen','')]


def test_html_and_docx_extraction_keep_title_page_boundaries(tmp_path):
    import zipfile
    path=tmp_path/'random.html';path.write_text('<html><body><h1>Pride and Prejudice</h1><p>by Jane Austen</p><p>Chapter 1</p></body></html>')
    evidence=inspect_book(path,extract(path))
    assert evidence['title']=='Pride and Prejudice' and evidence['author']=='Jane Austen'
    docx=tmp_path/'random.docx'
    with zipfile.ZipFile(docx,'w') as z:z.writestr('word/document.xml','<document><p><r><t>Title: Pride and Prejudice</t></r></p><p><r><t>Author: Jane Austen</t></r></p></document>')
    evidence=inspect_book(docx,extract(docx))
    assert evidence['title']=='Pride and Prejudice' and evidence['author']=='Jane Austen'


def test_opening_text_alone_is_not_enough_to_auto_apply():
    evidence=title_page('Pride and Prejudice\nAn interesting sentence from the story.')
    evidence['isbn']=''
    assert choose_match(evidence,[candidate()])['match'] is None


def test_conflicting_authors_ambiguous_titles_and_volumes_require_review():
    evidence=dict(title='Pride and Prejudice',author='Jane Austen',strength=.98,isbn='')
    assert choose_match(evidence,[candidate(author='John Austen')])['match'] is None
    assert author_score('Jane Austen','John Austen')==0
    evidence['author']=''
    assert choose_match(evidence,[candidate(),candidate(author='Another Author',key='OL2W')])['match'] is None
    evidence.update(title='Collected Tales Volume II',author='Jane Austen')
    assert choose_match(evidence,[candidate(title='Collected Tales Volume III')])['match'] is None


def test_exact_verified_isbn_can_identify_book_without_clear_title():
    evidence=dict(title='file_123',author='',strength=.35,isbn='0141439513')
    assert choose_match(evidence,[candidate(isbns=['9780141439518'])])['match']
    assert choose_match(evidence,[candidate(isbns=['9780141439519'])])['match'] is None


def test_automatic_update_is_logged_undoable_and_does_not_change_source(library,tmp_path):
    bid=book(library,tmp_path);source=Path(library.get(bid)['path']);before=source.read_bytes()
    library.lookup=lambda *a,**kw:[candidate()]
    jid=library.automatic_metadata({'ids':[bid]})['job_id']
    job=wait(library,jid)
    assert job['status']=='completed' and job['undoable']==1
    assert library.get(bid)['title']=='Pride and Prejudice' and library.get(bid)['metadata_origin']=='automatic'
    assert source.read_bytes()==before
    undone=wait(library,library.undo_metadata(jid)['job_id'])
    assert undone['errors']==0
    restored=library.get(bid)
    assert restored['title']=='file 123' and restored['metadata_locked']==0
    assert next(j for j in library.jobs() if j['id']==jid)['undoable']==0


def test_manual_edits_are_protected_and_undo_never_overwrites_newer_edits(library,tmp_path):
    bid=book(library,tmp_path)
    library.update_metadata(bid,dict(title='My chosen title',author='My chosen author'))
    def fail(*args):raise AssertionError('Protected book must not be inspected')
    library.identify_book=fail
    job=wait(library,library.automatic_metadata({'ids':[bid]})['job_id'])
    assert job['errors']==0 and library.get(bid)['title']=='My chosen title'
    library.identify_book=lambda *args:identified()
    jid=library.automatic_metadata({'ids':[bid],'protect_edits':False})['job_id'];wait(library,jid)
    library.update_metadata(bid,dict(title='A newer manual edit',author='Jane Austen'))
    undone=wait(library,library.undo_metadata(jid)['job_id'])
    assert undone['errors']==1 and library.get(bid)['title']=='A newer manual edit'


def test_uncertain_results_enter_review_queue_then_manual_apply_clears_them(library,tmp_path):
    bid=book(library,tmp_path,body='An opening sentence\nAnother ordinary sentence.')
    library.lookup=lambda *a,**kw:[candidate()]
    job=wait(library,library.automatic_metadata({'ids':[bid]})['job_id'])
    assert job['errors']==0 and library.get(bid)['title']=='file 123'
    assert library.stats()['review_count']==1
    assert library.catalogue(review=True)['total']==1
    library.update_metadata(bid,candidate())
    assert library.stats()['review_count']==0 and library.catalogue(review=True)['total']==0


def test_metadata_concurrent_edit_is_preserved(library,tmp_path):
    bid=book(library,tmp_path)
    def inspect(*a):
        library.update_metadata(bid,dict(title='Edited during lookup',author='Me'))
        return identified()
    library.identify_book=inspect
    job=wait(library,library.automatic_metadata({'ids':[bid]})['job_id'])
    assert job['errors']==1 and library.get(bid)['title']=='Edited during lookup'


def test_lookup_failure_does_not_stop_next_book(library,tmp_path):
    a=book(library,tmp_path,'first.txt');b=book(library,tmp_path,'second.txt')
    def inspect(bid):
        if bid==a:raise ValueError('Open Library is offline')
        return identified()
    library.identify_book=inspect
    job=wait(library,library.automatic_metadata({'ids':[a,b]})['job_id'])
    assert job['done']==2 and job['errors']==1
    assert library.get(a)['title']=='first' and library.get(b)['title']=='Pride and Prejudice'


def test_cancel_stops_after_current_lookup_without_applying_it(library,tmp_path):
    a=book(library,tmp_path,'first.txt');b=book(library,tmp_path,'second.txt')
    entered=threading.Event();release=threading.Event()
    def inspect(bid):entered.set();release.wait(5);return identified()
    library.identify_book=inspect
    jid=library.automatic_metadata({'ids':[a,b]})['job_id']
    assert entered.wait(3)
    library.cancel_job(jid);release.set()
    job=wait(library,jid)
    assert job['status']=='cancelled' and job['done']==0
    assert library.get(a)['title']=='first' and library.get(b)['title']=='second'


def test_lookup_cache_and_isbn_query_use_only_metadata(library,monkeypatch):
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return json.dumps({'docs':[{'key':'/works/OL66554W','title':'Pride and Prejudice','author_name':['Jane Austen'],'isbn':['9780141439518']}]}).encode()
    def request(req,timeout):calls.append(req.full_url);return Response()
    monkeypatch.setattr(urllib.request,'urlopen',request)
    library.lookup('Pride and Prejudice','Jane Austen',isbn='9780141439518')
    library.lookup('Pride and Prejudice','Jane Austen',isbn='9780141439518')
    assert len(calls)==1 and 'isbn%3A9780141439518' in calls[0]
    assert 'file_123' not in calls[0]


def test_library_snapshot_spans_pages_and_excludes_new_imports(library,tmp_path):
    with library.db() as db:
        db.executemany('INSERT INTO books(path,title,author,added) VALUES(?,?,?,?)',[(str(tmp_path/f'{i}.txt'),f'Book {i}','Author',i) for i in range(125)])
    selection=library.create_selection()
    assert selection['count']==125
    page=library.catalogue(page=2,selection_id=selection['selection_id'])
    assert len(page['books'])==60 and all(b['in_selection'] for b in page['books'])
    assert library.catalogue(q='!!!',selection_id=selection['selection_id'])['selection_count']==125
    new=book(library,tmp_path,'new.txt')
    first=page['books'][0]['id']
    chosen=library.selected_ids({'selection_id':selection['selection_id'],'excluded_ids':[first]})
    assert len(chosen)==124 and new not in chosen and first not in chosen
    assert len(library.selected_ids({'selection_id':selection['selection_id'],'excluded_ids':[first],'ids':[new]}))==125


def test_mass_plan_above_1000_executes_all_entries_not_just_preview(library,tmp_path,monkeypatch):
    folder=tmp_path/'sources';folder.mkdir();original=folder/'source.txt';original.write_text('book')
    rows=[];sha=hashlib.sha256(b'book').hexdigest();size,mtime=signature(original)
    for i in range(1005):
        path=folder/f'{i}.txt';os.link(original,path)
        rows.append((str(path),f'Book {i}','txt',size,mtime,sha))
    with library.db() as db:db.executemany('INSERT INTO books(path,title,format,size,mtime,sha256) VALUES(?,?,?,?,?,?)',rows)
    selection=library.create_selection()
    plan=library.plan({'selection_id':selection['selection_id'],'kind':'copy','destination':str(tmp_path/'output')})
    assert plan['total_entries']==1005 and len(plan['entries'])==200 and plan['omitted_entries']==805
    processed=[]
    monkeypatch.setattr(library,'perform',lambda data,entry:processed.append(entry['id']))
    job=wait(library,library.execute(plan['plan_id'])['job_id'])
    assert job['status']=='completed' and job['done']==1005 and len(set(processed))==1005


def test_snapshot_expiry_and_empty_selection_have_clear_errors(library,tmp_path):
    bid=book(library,tmp_path);snapshot=library.create_selection()
    with library.db() as db:db.execute('UPDATE selections SET created=0')
    with pytest.raises(ValueError,match='expired'):library.selected_ids(snapshot)
    with pytest.raises(ValueError,match='at least one'):library.selected_ids({'ids':[]})
    with pytest.raises(ValueError,match='Invalid book selection'):library.selected_ids({'ids':[True]})


def test_http_mass_selection_and_auto_metadata(library,tmp_path):
    bid=book(library,tmp_path);library.lookup=lambda *a,**kw:[candidate()]
    server=make_server(library,0);threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:token=response.read().decode().split('name="bookroom-token" content="')[1].split('"')[0]
        def api(route,data):
            request=urllib.request.Request(url+'/api/'+route,data=json.dumps(data).encode(),headers={'X-Bookroom-Token':token,'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:return json.load(response)
        selection=api('selection',{})
        assert api('selected-books',selection)[0]['id']==bid
        result=api('metadata-auto',selection)
        assert wait(library,result['job_id'])['status']=='completed'
        assert library.get(bid)['author']=='Jane Austen'
        undone=api('metadata-undo',result)
        assert wait(library,undone['job_id'])['errors']==0
    finally:server.shutdown();server.server_close()


def test_import_can_queue_automatic_identification_after_scan(library,tmp_path):
    folder=tmp_path/'import';folder.mkdir()
    (folder/'file123.txt').write_text('Title: Pride and Prejudice\nAuthor: Jane Austen\n\nChapter One')
    library.lookup=lambda *a,**kw:[candidate()]
    scan=library.scan(str(folder),auto_metadata=True)
    assert wait(library,scan['job_id'])['status']=='completed'
    metadata=next(j for j in library.jobs() if j['kind']=='Auto metadata')
    assert wait(library,metadata['id'])['errors']==0
    assert library.catalogue()['books'][0]['title']=='Pride and Prejudice'
