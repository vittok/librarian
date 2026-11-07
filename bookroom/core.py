"""Bookroom's local catalogue, safe file operations and conversion jobs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from html.parser import HTMLParser

from .locations import directory, resolve_location
from .metadata import inspect_book, choose_match, normalized, valid_isbn, isbn13
from .preview import Previews

INPUTS = set('azw azw3 azw4 cbz cbr cb7 cbc chm djvu docx epub fb2 fbz html htm htmlz kepub lit lrf mobi odt pdf prc pdb pml rb rtf snb tcr txt txtz'.split())
OUTPUTS = 'epub pdf azw3 mobi docx txt fb2 rtf htmlz kepub lit lrf pdb pmlz rb snb tcr txtz zip'.split()
TEXT_LIMIT = 2_000_000


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('p','div','h1','h2','h3','br','section','title'): self.parts.append('\n')
        if tag in ('script', 'style'): self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
        if tag in ('p','div','h1','h2','h3','section','title'): self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def plain_html(raw):
    p = TextHTML(); p.feed(raw); return ''.join(p.parts)


def xml_text(raw):
    root = ET.fromstring(raw)
    paragraphs=[e for e in root.iter() if e.tag.rsplit('}',1)[-1] in {'p','h'}]
    return '\n'.join(''.join(e.itertext()) for e in paragraphs) if paragraphs else ' '.join(root.itertext())


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:110]
    if value.upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}: value = '_' + value
    return value or 'Unknown'


def is_placeholder(path):
    """Author-index sidecars (and copies converted from them) are not books."""
    return Path(path).name.startswith('!')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()


def signature(path):
    s = path.stat(); return [s.st_size, s.st_mtime_ns]


def extract(path, text_limit=TEXT_LIMIT, pdf_pages=1000):
    """Bounded extraction. Unsupported/image-only files remain metadata searchable."""
    title = path.stem.replace('_', ' '); author = ''; body = ''; status = 'Metadata only'
    ext = path.suffix.lower()[1:]
    try:
        if ext in {'txt', 'html', 'htm', 'pml'}:
            raw = path.open('rb')
            with raw: body = raw.read(text_limit * 2).decode('utf-8', errors='replace')
            if ext in {'html', 'htm'}: body = plain_html(body)
        elif ext == 'fb2':
            with path.open('rb') as f: root = ET.fromstring(f.read(16_000_000))
            def values(name): return [' '.join(n.itertext()).strip() for n in root.iter() if n.tag.rsplit('}', 1)[-1] == name]
            title = next(iter(values('book-title')), title)
            author = ' '.join(values('first-name')[:1] + values('last-name')[:1])
            body = ' '.join(values('body'))
        elif ext in {'epub', 'kepub', 'docx', 'odt', 'htmlz', 'txtz', 'fbz'}:
            with zipfile.ZipFile(path) as z:
                members = z.infolist()
                if sum(i.file_size for i in members) > 100_000_000 or any(i.file_size > 16_000_000 for i in members):
                    return title, author, '', 'Metadata only: archive exceeds extraction limit'
                names = z.namelist()
                if ext in {'epub', 'kepub'}:
                    container = ET.fromstring(z.read('META-INF/container.xml'))
                    opf = next(e.attrib['full-path'] for e in container.iter() if e.tag.endswith('rootfile'))
                    root = ET.fromstring(z.read(opf))
                    title = next((e.text for e in root.iter() if e.tag.endswith('}title') and e.text), title)
                    author = '; '.join(e.text for e in root.iter() if e.tag.endswith('}creator') and e.text)
                    base = PurePosixPath(opf).parent
                    manifest = {e.get('id'): e.get('href') for e in root.iter() if e.tag.endswith('}item')}
                    ordered = [str(base / urllib.parse.unquote(manifest.get(e.get('idref'), ''))) for e in root.iter() if e.tag.endswith('}itemref')]
                    selected = [n for n in ordered if n in names and n.lower().endswith(('.html', '.xhtml', '.htm'))]
                elif ext == 'docx':
                    selected = ['word/document.xml']
                    if 'docProps/core.xml' in names:
                        root = ET.fromstring(z.read('docProps/core.xml'))
                        title = next((e.text for e in root if e.tag.endswith('}title') and e.text), title)
                        author = next((e.text for e in root if e.tag.endswith('}creator') and e.text), '')
                elif ext == 'odt': selected = ['content.xml']
                else: selected = [n for n in names if n.lower().endswith(('.txt', '.html', '.htm', '.xhtml', '.fb2'))]
                parts = []; length = 0
                for name in selected:
                    raw = z.read(name).decode('utf-8', errors='replace')
                    text = raw if name.endswith('.txt') else (xml_text(raw) if name.endswith(('.xml', '.fb2')) else plain_html(raw))
                    parts.append(text[:text_limit-length]); length += len(parts[-1])
                    if length >= text_limit: break
                body = '\n'.join(parts)
        elif ext == 'pdf':
            try:
                import pypdf
                reader = pypdf.PdfReader(path)
                meta = reader.metadata
                if meta: title = meta.title or title; author = meta.author or ''
                parts = []; length = 0
                for page in reader.pages[:pdf_pages]:
                    text = (page.extract_text() or '')[:text_limit-length]
                    parts.append(text); length += len(text)
                    if length >= text_limit: break
                body = '\n'.join(parts)
            except ImportError:
                if shutil.which('pdftotext'):
                    with tempfile.TemporaryDirectory() as folder:
                        out = Path(folder) / 'content.txt'
                        subprocess.run(['pdftotext', '-f', '1', '-l', str(pdf_pages), str(path), str(out)], check=True, capture_output=True, timeout=90)
                        with out.open('rb') as f: body = f.read(text_limit*2).decode('utf-8', 'replace')
                else: status = 'Metadata only: install pypdf or pdftotext for PDF text'
        if body.strip(): status = 'Text indexed' + (' (truncated)' if len(body) >= text_limit else '')
        elif ext == 'pdf': status = 'Metadata only: no text extracted; scanned PDFs need OCR'
    except Exception as e:
        status = f'Metadata only: extraction failed ({type(e).__name__})'
    return title[:1000], author[:1000], body[:text_limit], status


class Library:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve(); self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'library.sqlite3'
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bookroom')
        self.lock = threading.Lock(); self.metadata_lock = threading.Lock(); self.last_lookup = 0
        self.previews = Previews(extract)
        self.converter_error = ''
        self.converter = os.environ.get('BOOKROOM_CONVERTER') or shutil.which('ebook-convert')
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS books(
                    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                    author TEXT DEFAULT '', format TEXT, size INTEGER, mtime INTEGER, sha256 TEXT,
                    body TEXT DEFAULT '', index_status TEXT, year TEXT DEFAULT '', source TEXT DEFAULT '',
                    metadata_locked INTEGER DEFAULT 0, added REAL);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(title,author,body,content=books,content_rowid=id,tokenize='unicode61 remove_diacritics 2');
                CREATE TRIGGER IF NOT EXISTS books_ai AFTER INSERT ON books BEGIN
                    INSERT INTO search(rowid,title,author,body) VALUES(new.id,new.title,new.author,new.body); END;
                CREATE TRIGGER IF NOT EXISTS books_ad AFTER DELETE ON books BEGIN
                    INSERT INTO search(search,rowid,title,author,body) VALUES('delete',old.id,old.title,old.author,old.body); END;
                CREATE TRIGGER IF NOT EXISTS books_au AFTER UPDATE ON books BEGIN
                    INSERT INTO search(search,rowid,title,author,body) VALUES('delete',old.id,old.title,old.author,old.body);
                    INSERT INTO search(rowid,title,author,body) VALUES(new.id,new.title,new.author,new.body); END;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, status TEXT, total INTEGER,
                    done INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, log TEXT DEFAULT '[]', created REAL);
                CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, data TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY, scanned REAL);
                CREATE TABLE IF NOT EXISTS selections(id TEXT PRIMARY KEY, created REAL);
                CREATE TABLE IF NOT EXISTS selection_books(selection_id TEXT,book_id INTEGER,PRIMARY KEY(selection_id,book_id));
                CREATE TABLE IF NOT EXISTS metadata_cache(key TEXT PRIMARY KEY,data TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS metadata_reviews(book_id INTEGER PRIMARY KEY,evidence TEXT,candidates TEXT,reason TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS metadata_history(id INTEGER PRIMARY KEY,job_id TEXT,book_id INTEGER,previous TEXT,applied TEXT,undone INTEGER DEFAULT 0,created REAL);

            ''')
            if 'location' not in {row[1] for row in db.execute('PRAGMA table_info(roots)')}:
                db.execute("ALTER TABLE roots ADD COLUMN location TEXT DEFAULT ''")
            if 'metadata_origin' not in {row[1] for row in db.execute('PRAGMA table_info(books)')}:
                db.execute("ALTER TABLE books ADD COLUMN metadata_origin TEXT DEFAULT ''")
            if 'cancel_requested' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0')
            db.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running','cancelling')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self): self.pool.shutdown(wait=True)

    def get(self, book_id):
        with self.db() as db: row = db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not row: raise ValueError('Book no longer exists in catalogue')
        return dict(row)

    def book_preview(self, book_id):
        book=self.get(book_id)
        preview=self.previews.opening(Path(book['path']),self.converter)
        return {**preview,'book':{k:book[k] for k in ('id','title','author','format')},'filename':Path(book['path']).name}

    def preview_page(self, book_id, page):
        book=self.get(book_id)
        return self.previews.page(Path(book['path']),page,self.converter)

    def catalogue(self, q='', fmt='', duplicates=False, page=1, selection_id='', review=False):
        page = max(1, int(page)); clauses = []; params = []
        if q.strip():
            words = re.findall(r'\w+', q, flags=re.UNICODE)[:30]
            if not words: clauses.append('0')
            else:
                clauses.append('b.id IN (SELECT rowid FROM search WHERE search MATCH ?)')
                params.append(' AND '.join('"'+w+'"*' for w in words))
        if review: clauses.append('b.id IN (SELECT book_id FROM metadata_reviews)')
        if fmt: clauses.append('b.format=?'); params.append(fmt)
        if duplicates: clauses.append('b.sha256 IN (SELECT sha256 FROM books GROUP BY sha256 HAVING COUNT(*)>1)')
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        selected=set();selection_count=0
        with self.db() as db:
            if selection_id:
                self._check_selection(db,selection_id)
                selected={r[0] for r in db.execute('SELECT s.book_id FROM selection_books s JOIN books b ON b.id=s.book_id WHERE selection_id=?',(selection_id,))}
                selection_count=len(selected)
            total = db.execute('SELECT COUNT(*) FROM books b'+where, params).fetchone()[0]
            rows = db.execute('SELECT b.id,b.path,b.title,b.author,b.format,b.size,b.index_status,b.year,b.source,b.added FROM books b'+where+' ORDER BY b.title COLLATE NOCASE,b.id LIMIT 60 OFFSET ?', (*params,(page-1)*60)).fetchall()
        return {'books':[{**dict(r),'in_selection':r['id'] in selected} for r in rows], 'total':total, 'page':page, 'pages':max(1,(total+59)//60),'selection_count':selection_count}

    def stats(self):
        with self.db() as db:
            count, size, indexed = db.execute("SELECT COUNT(*),COALESCE(SUM(size),0),COALESCE(SUM(index_status LIKE 'Text indexed%'),0) FROM books").fetchone()
            formats = [dict(r) for r in db.execute('SELECT format,COUNT(*) AS count FROM books GROUP BY format ORDER BY count DESC')]
            duplicates = db.execute('SELECT COALESCE(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM books GROUP BY sha256 HAVING c>1)').fetchone()[0]
            review_count=db.execute('SELECT COUNT(*) FROM metadata_reviews r JOIN books b ON b.id=r.book_id').fetchone()[0]
            roots = [dict(r) for r in db.execute('SELECT * FROM roots ORDER BY scanned DESC')]
        return dict(count=count,size=size,indexed=indexed,duplicates=duplicates,review_count=review_count,formats=formats,roots=roots,converter=bool(self.converter),converter_error=self.converter_error,outputs=OUTPUTS,data_dir=str(self.data_dir))

    def index_file(self, path):
        if is_placeholder(path):raise ValueError('Files with names beginning with ! are excluded from the book catalogue')
        path = Path(path).resolve()
        with self.db() as db: old = db.execute('SELECT * FROM books WHERE path=?',(str(path),)).fetchone()
        size, mtime = signature(path)
        if old and old['size']==size and old['mtime']==mtime: return old['id']
        sha = digest(path)
        title, author, body, status = extract(path)
        if signature(path) != [size, mtime]: raise ValueError('File changed during indexing; scan again')
        if old and old['metadata_locked']: title, author = old['title'], old['author']
        with self.db() as db:
            db.execute('''INSERT INTO books(path,title,author,format,size,mtime,sha256,body,index_status,added)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET title=excluded.title,
                author=excluded.author,format=excluded.format,size=excluded.size,mtime=excluded.mtime,
                sha256=excluded.sha256,body=excluded.body,index_status=excluded.index_status''',
                (str(path),title,author,path.suffix.lower()[1:],size,mtime,sha,body,status,time.time()))
            return db.execute('SELECT id FROM books WHERE path=?',(str(path),)).fetchone()[0]

    def update_metadata(self, book_id, values):
        self.get(book_id)
        title = str(values.get('title','')).strip()[:1000]
        if not title: raise ValueError('Title is required')
        author = str(values.get('author','')).strip()[:1000]
        year = str(values.get('year','')).strip()[:20]
        source = str(values.get('source',''))[:300]
        if source and not re.fullmatch(r'https://openlibrary\.org/works/OL\d+W', source): source = ''
        with self.db() as db:
            db.execute("UPDATE books SET title=?,author=?,year=?,source=?,metadata_locked=1,metadata_origin='manual' WHERE id=?",(title,author,year,source,book_id))
            db.execute('DELETE FROM metadata_reviews WHERE book_id=?',(book_id,))
        return {'ok':True}

    def lookup(self, title='', author='', isbn=''):
        isbn=valid_isbn(isbn)
        key=json.dumps([normalized(title),normalized(author),isbn],ensure_ascii=False)
        with self.metadata_lock:
            with self.db() as db:
                cached=db.execute('SELECT data FROM metadata_cache WHERE key=? AND created>?',(key,time.time()-7*86400)).fetchone()
            if cached:return json.loads(cached[0])
            time.sleep(max(0, 1.1 - (time.monotonic() - self.last_lookup)))
            self.last_lookup = time.monotonic()
            query = {'limit':8,'fields':'key,title,author_name,first_publish_year'+(',isbn' if isbn else '')}
            if isbn:query['q']='isbn:'+isbn
            else:
                query['title']=title[:250]
                if author:query['author']=author[:250]
            url='https://openlibrary.org/search.json?'+urllib.parse.urlencode(query)
            req=urllib.request.Request(url,headers={'User-Agent':'Bookroom/1.0 (local personal library manager)','Accept':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=25) as response:data=json.loads(response.read(2_000_000))
            except Exception as e:raise ValueError(f'Open Library could not be reached: {e}') from e
            result=[]
            for item in data.get('docs',[]):
                work=str(item.get('key',''));work='/works/'+work if work.startswith('OL') else work
                if not re.fullmatch(r'/works/OL\d+W',work):continue
                result.append(dict(title=str(item.get('title','')),author='; '.join(item.get('author_name',[])),year=str(item.get('first_publish_year','')),source='https://openlibrary.org'+work,isbns=[v for v in item.get('isbn',[]) if valid_isbn(v) and (not isbn or isbn13(v)==isbn13(isbn))][:4]))
            with self.db() as db:db.execute('INSERT OR REPLACE INTO metadata_cache VALUES(?,?,?)',(key,json.dumps(result),time.time()))
            return result

    def _check_selection(self, db, selection_id):
        row=db.execute('SELECT created FROM selections WHERE id=?',(selection_id,)).fetchone()
        if not row or row[0]<time.time()-86400:raise ValueError('Library selection expired. Select all books again.')

    def create_selection(self):
        sid=uuid.uuid4().hex
        with self.db() as db:
            db.execute('DELETE FROM selection_books WHERE selection_id IN (SELECT id FROM selections WHERE created<?)',(time.time()-86400,))
            db.execute('DELETE FROM selections WHERE created<?',(time.time()-86400,))
            db.execute('INSERT INTO selections VALUES(?,?)',(sid,time.time()))
            db.execute('INSERT INTO selection_books SELECT ?,id FROM books',(sid,))
            count=db.execute('SELECT COUNT(*) FROM selection_books WHERE selection_id=?',(sid,)).fetchone()[0]
        return dict(selection_id=sid,count=count)

    def selected_ids(self, values):
        ids=values.get('ids',[]);excluded=values.get('excluded_ids',[])
        if not isinstance(ids,list) or not isinstance(excluded,list) or any(type(i) is not int or i<=0 for i in ids+excluded):raise ValueError('Invalid book selection')
        selected=set()
        with self.db() as db:
            sid=values.get('selection_id')
            if sid:
                self._check_selection(db,sid)
                selected={r[0] for r in db.execute('SELECT s.book_id FROM selection_books s JOIN books b ON b.id=s.book_id WHERE selection_id=?',(sid,))}
            for bid in ids:
                if not db.execute('SELECT 1 FROM books WHERE id=?',(bid,)).fetchone():raise ValueError('A selected book no longer exists; refresh the library')
            selected.update(ids);selected.difference_update(excluded)
        if not selected:raise ValueError('Select at least one book')
        return sorted(selected)

    def selected_books(self, values):
        ids=self.selected_ids(values)
        if len(ids)>50:raise ValueError('Use Auto metadata for more than 50 books; manual review handles up to 50 at once')
        with self.db() as db:
            members={r[0] for r in db.execute('SELECT book_id FROM selection_books WHERE selection_id=?',(values.get('selection_id',''),))}
        return [{**{k:book[k] for k in ('id','title','author','format')},'in_selection':bid in members} for bid in ids for book in [self.get(bid)]]

    def identify_book(self, book_id):
        book=self.get(book_id);path=Path(book['path'])
        before=signature(path)
        if before!=[book['size'],book['mtime']]:raise ValueError('Book changed since indexing; rescan before identifying it')
        evidence=inspect_book(path,extract(path,text_limit=120000,pdf_pages=12),self.converter)
        if signature(path)!=before:raise ValueError('Book changed during inspection; rescan it')
        if not evidence['title'] and not evidence['isbn']:return dict(evidence=evidence,candidates=[],match=None,reason='No usable text or embedded metadata; image-only books need OCR')
        candidates=self.lookup(evidence['title'],evidence['author'],isbn=evidence['isbn'])
        if evidence['isbn'] and not candidates:candidates=self.lookup(evidence['title'],evidence['author']) if evidence['title'] else []
        return dict(evidence=evidence,candidates=candidates,**choose_match(evidence,candidates))

    def automatic_metadata(self, values):
        ids=self.selected_ids(values)
        protect=values.get('protect_edits',True) is not False
        def task(jid):
            for bid in ids:
                if self.cancelled(jid):break
                try:
                    previous=self.get(bid)
                    if protect and previous['metadata_locked'] and previous['metadata_origin']!='automatic':
                        self.progress(jid,f"Preserved edited metadata: {previous['title']}");continue
                    identified=self.identify_book(bid)
                    if self.cancelled(jid):break
                    match=identified['match']
                    with self.db() as db:
                        current=dict(db.execute('SELECT * FROM books WHERE id=?',(bid,)).fetchone() or {})
                        if any(current.get(k)!=previous[k] for k in ('title','author','year','source','metadata_locked','metadata_origin','path','mtime')):raise ValueError('Book changed while checking metadata; retry this book')
                        if match:
                            fields=('title','author','year','source','metadata_locked','metadata_origin')
                            before={k:previous[k] for k in fields}
                            after={k:match.get(k,'') for k in ('title','author','year','source')};after.update(metadata_locked=1,metadata_origin='automatic')
                            db.execute('INSERT INTO metadata_history(job_id,book_id,previous,applied,created) VALUES(?,?,?,?,?)',(jid,bid,json.dumps(before),json.dumps(after),time.time()))
                            db.execute("UPDATE books SET title=?,author=?,year=?,source=?,metadata_locked=1,metadata_origin='automatic' WHERE id=?",(after['title'],after['author'],after['year'],after['source'],bid))
                            db.execute('DELETE FROM metadata_reviews WHERE book_id=?',(bid,))
                        else:
                            db.execute('INSERT OR REPLACE INTO metadata_reviews VALUES(?,?,?,?,?)',(bid,json.dumps(identified['evidence']),json.dumps(identified['candidates']),identified['reason'],time.time()))
                    message=f"Updated: {previous['title']} → {match['title']} — {match['author']} ({identified['reason']})" if match else f"Review needed: {previous['title']} ({identified['reason']})"
                    self.progress(jid,message)
                except Exception as e:self.progress(jid,f'Book {bid}: {e}',True)
        return self.enqueue('Auto metadata',len(ids),task)

    def cancelled(self, jid):
        with self.db() as db:return bool(db.execute('SELECT cancel_requested FROM jobs WHERE id=?',(jid,)).fetchone()[0])

    def cancel_job(self, jid):
        with self.db() as db:
            row=db.execute('SELECT kind,status FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row or row['kind']!='Auto metadata':raise ValueError('This job cannot be cancelled here')
            if row['status'] in ('queued','running','cancelling'):
                db.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END WHERE id=?",(jid,))
        return {'ok':True}

    def undo_metadata(self, jid):
        with self.db() as db:
            job=db.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()
            if not job or job['status'] in ('queued','running','cancelling'):raise ValueError('Wait for the metadata job to stop before undoing it')
            history=[dict(r) for r in db.execute('SELECT * FROM metadata_history WHERE job_id=? AND undone=0 ORDER BY id DESC',(jid,))]
        if not history:raise ValueError('No automatic changes remain to undo')
        def task(undo_jid):
            for change in history:
                try:
                    before=json.loads(change['previous']);applied=json.loads(change['applied'])
                    with self.db() as db:
                        current=dict(db.execute('SELECT * FROM books WHERE id=?',(change['book_id'],)).fetchone() or {})
                        if any(current.get(k)!=v for k,v in applied.items()):raise ValueError('Book was edited after this job; kept its newer details')
                        fields=list(before)
                        db.execute('UPDATE books SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*before.values(),change['book_id']))
                        db.execute('UPDATE metadata_history SET undone=1 WHERE id=?',(change['id'],))
                    self.progress(undo_jid,f"Restored: {before['title']}")
                except Exception as e:self.progress(undo_jid,f"Book {change['book_id']}: {e}",True)
        return self.enqueue('Undo metadata',len(history),task)

    def enqueue(self, kind, total, task):
        jid = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO jobs(id,kind,status,total,created) VALUES(?,?,?,?,?)',(jid,kind,'queued',total,time.time()))
        def run():
            if self.cancelled(jid):return
            with self.db() as db: db.execute("UPDATE jobs SET status='running' WHERE id=?",(jid,))
            try:
                task(jid)
                with self.db() as db: db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' WHEN errors>0 THEN 'completed with errors' ELSE 'completed' END WHERE id=?",(jid,))
            except Exception as e:
                self.progress(jid, str(e), error=True, increment=False)
                with self.db() as db: db.execute("UPDATE jobs SET status='failed' WHERE id=?",(jid,))
        self.pool.submit(run)
        return {'job_id':jid}

    def progress(self, jid, message, error=False, increment=True):
        with self.db() as db:
            row = db.execute('SELECT log FROM jobs WHERE id=?',(jid,)).fetchone()
            log = json.loads(row[0]); log.append({'message':message,'error':error,'at':time.time()});log=log[-1000:]
            db.execute('UPDATE jobs SET done=done+?,errors=errors+?,log=? WHERE id=?',(int(increment),int(error),json.dumps(log),jid))

    def jobs(self):
        with self.db() as db: rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 30').fetchall()
        with self.db() as db: undoable={r[0]:r[1] for r in db.execute('SELECT job_id,COUNT(*) FROM metadata_history WHERE undone=0 GROUP BY job_id')}
        return [{**dict(r),'log':json.loads(r['log']),'undoable':undoable.get(r['id'],0)} for r in rows]

    def scan(self, folder, auto_metadata=False):
        location = resolve_location(folder)
        def task(jid):
            # Network checks run in the worker so import returns a queued job
            # without waiting for the OS to contact a slow/disconnected share.
            root = directory(location).resolve()
            paths = []; walk_errors = []; scanned_ids=[]
            def onerror(e): walk_errors.append(str(e))
            for parent, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
                dirs[:] = [d for d in dirs if not (Path(parent)/d).is_symlink() and (Path(parent)/d).resolve() != self.data_dir]
                for name in files:
                    p = Path(parent)/name
                    if not is_placeholder(p) and not p.is_symlink() and p.suffix.lower()[1:] in INPUTS: paths.append(p)
            if walk_errors and not paths:
                raise ValueError('Cannot scan folder. Reconnect the network share and check read permissions. '+walk_errors[0])
            with self.db() as db: db.execute('UPDATE jobs SET total=? WHERE id=?',(len(paths),jid))
            for error in walk_errors: self.progress(jid,'Folder unavailable; reconnect the share or check read permissions: '+error,True,False)
            for path in paths:
                try: scanned_ids.append(self.index_file(path)); self.progress(jid, f'Indexed {path}')
                except Exception as e: self.progress(jid,f'{path}: {e}',True)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO roots(path,scanned,location) VALUES(?,?,?)',(str(root),time.time(),location.uri or str(root)))
            if auto_metadata and scanned_ids:
                self.automatic_metadata({'ids':scanned_ids,'protect_edits':True})
                self.progress(jid,f'Queued automatic metadata for {len(scanned_ids)} books',increment=False)
        return self.enqueue('Scan folder',0,task)

    def plan(self, values):
        kind = values.get('kind')
        if kind not in {'copy','move','organize','convert'}: raise ValueError('Unknown batch action')
        ids = self.selected_ids(values)
        folder = str(values.get('destination','')).strip()
        if not folder: raise ValueError('Choose a destination folder')
        destination_location = resolve_location(folder)
        if destination_location.boundary:
            directory(resolve_location(str(destination_location.boundary)))
        destination = destination_location.path.resolve()
        if destination.exists() and not destination.is_dir(): raise ValueError('Destination must be a folder')
        formats = list(dict.fromkeys(values.get('formats',[])))
        if kind=='convert':
            if not self.converter: raise ValueError('Install Calibre and restart Bookroom to enable conversion')
            if not formats or any(f not in OUTPUTS for f in formats): raise ValueError('Select supported output formats')
        entries = []; reserved = set()
        for bid in ids:
            book = self.get(bid); src = Path(book['path'])
            if not src.is_file(): raise ValueError(f'Source is missing: {src}. Restore it and rescan.')
            sig = signature(src)
            if sig != [book['size'], book['mtime']]: raise ValueError(f'{src.name} changed since indexing. Rescan before continuing.')
            targets = formats if kind=='convert' else [book['format']]
            for fmt in targets:
                if kind=='convert' and fmt==book['format']: continue
                target = destination
                if kind=='organize': target = target / safe_name(book['author'] or 'Unknown author') / safe_name(book['title'])
                name = safe_name(book['title']) if kind in {'organize','convert'} else safe_name(src.stem)
                output = target / f'{name}.{fmt}'
                serial = 2
                while output.exists() or str(output) in reserved:
                    output = target / f'{name} ({serial}).{fmt}'; serial += 1
                reserved.add(str(output))
                entries.append(dict(id=bid,source=str(src),target=str(output),signature=sig,sha256=book['sha256'],title=book['title'],author=book['author'],format=fmt))
        if not entries: raise ValueError('The selected files already use the chosen output formats')
        pid = uuid.uuid4().hex
        data = {'kind':kind,'entries':entries,'destination_location':destination_location.value,'destination_path':str(destination),'organize_mode':'copy' if values.get('organize_mode')!='move' else 'move'}
        with self.db() as db:
            db.execute('DELETE FROM plans WHERE created<?',(time.time()-900,))
            db.execute('INSERT INTO plans VALUES(?,?,?)',(pid,json.dumps(data),time.time()))
        return {**data,'entries':entries[:200],'total_entries':len(entries),'omitted_entries':max(0,len(entries)-200),'plan_id':pid}

    def execute(self, plan_id):
        with self.lock, self.db() as db:
            row = db.execute('SELECT data,created FROM plans WHERE id=?',(plan_id,)).fetchone()
            if not row or time.time()-row['created']>900: raise ValueError('Preview expired or already used. Create a new preview.')
            data = json.loads(row['data'])
            db.execute('DELETE FROM plans WHERE id=?',(plan_id,))
        def task(jid):
            for entry in data['entries']:
                try:
                    self.perform(data,entry)
                    self.progress(jid,f"{data['kind'].capitalize()}: {entry['source']} → {entry['target']}")
                except Exception as e: self.progress(jid,f"{entry['source']}: {e}",True)
        return self.enqueue(data['kind'].capitalize()+' books',len(data['entries']),task)

    def perform(self, plan, entry):
        source = Path(entry['source']); target = Path(entry['target'])
        if plan.get('destination_location'):
            fresh = resolve_location(plan['destination_location'])
            if fresh.path.resolve() != Path(plan['destination_path']):
                raise ValueError('Network mount changed since preview; create a new preview')
            if fresh.boundary: directory(resolve_location(str(fresh.boundary)))
        current = self.get(entry['id'])
        if current['path']!=str(source) or current['title']!=entry['title'] or current['author']!=entry['author']:
            raise ValueError('Book changed since preview; create a new preview')
        if signature(source)!=entry['signature'] or digest(source)!=entry['sha256']:
            raise ValueError('Source changed since preview; rescan and preview again')
        target.parent.mkdir(parents=True,exist_ok=True)
        move = plan['kind']=='move' or (plan['kind']=='organize' and plan['organize_mode']=='move')
        # Stage in the destination filesystem, then reserve with exclusive creation.
        # Failed conversions are kept out of the catalogue; exclusive creation
        # prevents overwrites. A process crash during copy can leave a partial
        # destination, with the original retained for recovery.
        with tempfile.TemporaryDirectory(prefix='.bookroom-',dir=target.parent) as folder:
            stage = Path(folder)/('converted.'+entry['format'])
            if plan['kind']=='convert':
                run = subprocess.run([self.converter,str(source),str(stage),'--title',entry['title'],'--authors',entry['author'] or 'Unknown'],capture_output=True,timeout=600,env={**os.environ,'QT_QPA_PLATFORM':os.environ.get('QT_QPA_PLATFORM','offscreen')})
                if run.returncode: raise ValueError('Conversion failed: '+run.stderr.decode('utf-8','replace')[-1500:])
                if not stage.is_file() or stage.stat().st_size==0: raise ValueError('Converter did not produce a book')
            else: shutil.copy2(source,stage)
            if signature(source)!=entry['signature'] or digest(source)!=entry['sha256']: raise ValueError('Source changed while processing; no output committed')
            try:
                with target.open('xb') as output, stage.open('rb') as input_file: shutil.copyfileobj(input_file,output)
            except FileExistsError: raise ValueError('Destination appeared after preview; existing file preserved')
            except Exception:
                target.unlink(missing_ok=True); raise
        if move:
            # Index the completed destination before removing the source. On any
            # indexing failure both files remain and the job reports an error.
            new_id = self.index_file(target)
            self.update_metadata(new_id,{k:current[k] for k in ('title','author','year','source')})
            if signature(source)!=entry['signature'] or digest(source)!=entry['sha256']:
                raise ValueError('Source changed before removal; both copies retained')
            source.unlink()
            with self.db() as db: db.execute('DELETE FROM books WHERE id=?',(entry['id'],))
        else:
            new_id = self.index_file(target)
            self.update_metadata(new_id,{k:current[k] for k in ('title','author','year','source')})
