"""Bounded book inspection and conservative matching; never modifies book files."""
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET


def normalized(value):
    return ' '.join(re.findall(r'\w+', ''.join(c for c in unicodedata.normalize('NFKD',str(value)).casefold() if not unicodedata.combining(c))))


def valid_isbn(value):
    value=re.sub(r'[^0-9Xx]','',str(value)).upper()
    if len(value)==13 and value.isdigit() and value.startswith(('978','979')):
        return value if sum(int(c)*(1 if i%2==0 else 3) for i,c in enumerate(value))%10==0 else ''
    if len(value)==10 and value[:9].isdigit() and (value[-1].isdigit() or value[-1]=='X'):
        if sum((10-i)*(10 if c=='X' else int(c)) for i,c in enumerate(value))%11==0:return value
    return ''


def isbn13(value):
    value=valid_isbn(value)
    if len(value)==10:
        base='978'+value[:9]
        return base+str((-sum(int(c)*(1 if i%2==0 else 3) for i,c in enumerate(base)))%10)
    return value


def find_isbn(body):
    for match in re.finditer(r'ISBN(?:-1[03])?\s*:?\s*([0-9Xx][0-9Xx -]{8,24}[0-9Xx])',body[:80000],re.I):
        value=valid_isbn(match[1])
        if value:return value
    return ''


def useful_title(title):
    text=normalized(title)
    noise={'unknown','untitled','title','contents','table of contents','copyright','introduction','preface','cover','chapter one','chapter 1','dedication','epub','document','microsoft word'}
    return bool(2<=len(title.strip())<=250 and text not in noise and not text.isdigit() and not re.search(r'https?://|\.(?:pdf|epub|docx)$',title,re.I))


def useful_author(author):
    text=normalized(author)
    return bool(text and text not in {'unknown','unknown author','anonymous','author','n a'} and len(author)<160 and not re.search(r'copyright|https?://|ISBN',author,re.I))


def title_page(body):
    lines=[' '.join(line.split()).strip(' \t\ufeff') for line in body[:16000].splitlines() if line.strip()][:100]
    title='';author=''
    for line in lines:
        match=re.match(r'^(?:Title|Book title)\s*:\s*(.+)',line,re.I)
        if match and useful_title(match[1]):title=match[1]
        match=re.match(r'^(?:Author|Authors)\s*:\s*(.+)',line,re.I)
        if match and useful_author(match[1]):author=match[1]
    if title:return dict(title=title,author=author,source='labelled title page',strength=.98)
    for i,line in enumerate(lines[:40]):
        match=re.match(r'^(?:by|written by)\s+(.{3,140})$',line,re.I)
        if match and useful_author(match[1]):
            previous=[v for v in lines[max(0,i-3):i] if useful_title(v) and len(v.split())<=16 and not re.search(r'copyright|publisher|edition|gutenberg',v,re.I)]
            if previous:return dict(title=previous[-1],author=match[1],source='title page byline',strength=.94)
    candidates=[]
    for line in lines[:16]:
        if useful_title(line) and 1<=len(line.split())<=14 and not re.search(r'copyright|publisher|edition|gutenberg|all rights|printed|translated|ISBN|^chapter\b',line,re.I):
            candidates.append(line)
    if candidates:return dict(title=candidates[0],author='',source='opening text (needs corroboration)',strength=.55)
    return dict(title='',author='',source='no usable title page',strength=0)


def _calibre_metadata(path,converter):
    if not converter:return {}
    executable=Path(converter).with_name('ebook-meta.exe' if os.name=='nt' else 'ebook-meta')
    engine=str(executable) if executable.is_file() else shutil.which('ebook-meta')
    if not engine:return {}
    try:
        with tempfile.TemporaryDirectory(prefix='bookroom-identify-') as folder:
            opf=Path(folder)/'metadata.opf'
            result=subprocess.run([engine,str(path),'--to-opf',str(opf)],capture_output=True,timeout=45,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'})
            if result.returncode or not opf.is_file() or opf.stat().st_size>2_000_000:return {}
            root=ET.fromstring(opf.read_bytes())
            title=next((e.text for e in root.iter() if e.tag.endswith('}title') and e.text),'')
            author='; '.join(e.text for e in root.iter() if e.tag.endswith('}creator') and e.text)
            isbn=next((valid_isbn(e.text) for e in root.iter() if e.tag.endswith('}identifier') and valid_isbn(e.text)), '')
            return dict(title=title,author=author,isbn=isbn)
    except (OSError,ValueError,ET.ParseError,subprocess.SubprocessError):return {}


def inspect_book(path,extracted,converter=None):
    path=Path(path);title,author,body,status=extracted
    page=title_page(body)
    embedded=useful_title(title) and (normalized(title)!=normalized(path.stem.replace('_',' ')) or useful_author(author))
    evidence=dict(title=title if useful_title(title) else '',author=author if useful_author(author) else '',source='embedded metadata' if embedded else 'filename',strength=.96 if embedded and useful_author(author) else .85 if embedded else .35,isbn=find_isbn(body))
    if not embedded or not evidence['author']:
        meta=_calibre_metadata(path,converter)
        if useful_title(meta.get('title','')) and (normalized(meta['title'])!=normalized(path.stem.replace('_',' ')) or useful_author(meta.get('author',''))):
            evidence.update(title=meta['title'],author=meta.get('author','') if useful_author(meta.get('author','')) else '',source='embedded metadata (Calibre)',strength=.96 if useful_author(meta.get('author','')) else .85)
        evidence['isbn']=evidence['isbn'] or meta.get('isbn','')
    if not body.strip() and converter and page['strength']==0 and evidence['strength']<.9:
        try:
            with tempfile.TemporaryDirectory(prefix='bookroom-title-page-') as folder:
                output=Path(folder)/'opening.txt'
                result=subprocess.run([converter,str(path),str(output)],capture_output=True,timeout=90,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'})
                if result.returncode==0 and output.is_file():
                    with output.open('rb') as f:body=f.read(240000).decode('utf-8','replace')
                    page=title_page(body);evidence['isbn']=evidence['isbn'] or find_isbn(body)
        except (OSError,subprocess.SubprocessError):pass
    # Explicit title-page fields/bylines outrank stale or missing embedded data.
    if page['strength']>=.9 or page['strength']>evidence['strength']:
        evidence.update(page)
    evidence['title']=evidence['title'][:1000];evidence['author']=evidence['author'][:1000]
    return evidence


def title_score(left,right):
    left,right=normalized(left),normalized(right)
    if left==right:return 1.0
    # Different volume numbers should never be treated as near-identical titles.
    if re.findall(r'\b(?:\d+|i{1,3}|iv|vi{0,3}|ix|x)\b',left)!=re.findall(r'\b(?:\d+|i{1,3}|iv|vi{0,3}|ix|x)\b',right):return 0
    return SequenceMatcher(None,left,right).ratio()


def author_score(left,right):
    def tokens(value):
        value=re.split(r';|\s+and\s+|&',value,maxsplit=1)[0]
        if ',' in value:
            last,first=value.split(',',1);value=first+' '+last
        return normalized(value).split()
    a,b=tokens(left),tokens(right)
    if a==b and a:return 1.0
    if len(a)<2 or len(b)<2 or a[-1]!=b[-1]:return 0
    first_a,first_b=''.join(a[:-1]),''.join(b[:-1])
    if first_a==first_b:return .98
    # Initials may abbreviate names, but two conflicting full first names may not.
    if a[0]!=b[0] and len(a[0])>1 and len(b[0])>1:return 0
    if a[0][0]==b[0][0] and (len(a[0])==1 or len(b[0])==1):return .96
    return 0


def choose_match(evidence,candidates):
    ranked=[];seen=set()
    for candidate in candidates:
        if not useful_title(candidate.get('title','')) or not useful_author(candidate.get('author','')):continue
        key=(normalized(candidate['title']),normalized(candidate['author']))
        if key in seen:continue
        seen.add(key)
        title=title_score(evidence['title'],candidate['title'])
        author=author_score(evidence['author'],candidate['author']) if evidence['author'] else 0
        exact_isbn=bool(evidence['isbn'] and isbn13(evidence['isbn']) in {isbn13(v) for v in candidate.get('isbns',[]) if valid_isbn(v)})
        score=0;reason='Insufficient agreement with book contents'
        if exact_isbn and (evidence['strength']<.85 or title>=.75):score=1;reason='Verified ISBN match'
        elif evidence['strength']>=.85 and title>=.94 and author>=.94:score=.98*title;reason='Title and author agree with the book'
        elif evidence['strength']>=.85 and not evidence['author'] and title==1 and len(normalized(evidence['title']).split())>=3:score=.94;reason='Exact distinctive title from inside the book'
        ranked.append((score,candidate,reason))
    ranked.sort(key=lambda v:v[0],reverse=True)
    if not ranked or ranked[0][0]<.94:return dict(match=None,reason='No confident match; review candidates manually')
    if len(ranked)>1 and ranked[0][0]-ranked[1][0]<.04:return dict(match=None,reason='Several equally plausible matches; review required')
    return dict(match=ranked[0][1],reason=ranked[0][2])
