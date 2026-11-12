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

try:
    from rapidfuzz.fuzz import ratio as _rf_ratio, token_set_ratio as _rf_token_set_ratio
except ImportError:  # Source checkouts still work before optional dependencies are installed.
    _rf_ratio = _rf_token_set_ratio = None


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
        match=re.match(r'^(?:Title|Book title|N[aá]zev|N[aá]zov|Titul|Nazwa)\s*:\s*(.+)',line,re.I)
        if match and useful_title(match[1]):title=match[1]
        match=re.match(r'^(?:Author|Authors|Autor|Autorka|Napsal(?:a)?|Nap[ií]sal(?:a)?)\s*:\s*(.+)',line,re.I)
        if match and useful_author(match[1]):author=match[1]
    if title:return dict(title=title,author=author,source='labelled title page',strength=.98)
    for i,line in enumerate(lines[:40]):
        match=re.match(r'^(?:by|written by|napsal(?:a)?|nap[ií]sal(?:a)?|autor(?:ka)?)\s+(.{3,140})$',line,re.I)
        if match and useful_author(match[1]):
            previous=[v for v in lines[max(0,i-3):i] if useful_title(v) and len(v.split())<=16 and not re.search(r'copyright|publisher|edition|gutenberg',v,re.I)]
            if previous:return dict(title=previous[-1],author=match[1],source='title page byline',strength=.94)
    candidates=[]
    for line in lines[:16]:
        if useful_title(line) and 1<=len(line.split())<=14 and not re.search(r'copyright|publisher|edition|gutenberg|all rights|printed|translated|ISBN|^chapter\b',line,re.I):
            candidates.append(line)
    if candidates:return dict(title=candidates[0],author='',source='opening text (needs corroboration)',strength=.55)
    return dict(title='',author='',source='no usable title page',strength=0)


def _path_hypotheses(path):
    """Return conservative title/author possibilities from filename ancestry."""
    path=Path(path)
    stem=re.sub(r'[_]+',' ',path.stem)
    stem=re.sub(r'\s+',' ',stem).strip(' ._-')
    generic={'books','book','ebooks','ebook','library','knihy','multimedia','documents','downloads','pdb','epub','pdf','mobi','prc','txt'}
    parents=[]
    for parent in path.parents[:3]:
        name=re.sub(r'[_]+',' ',parent.name).strip(' ._-')
        if useful_author(name) and normalized(name)!=normalized(stem) and normalized(name) not in generic and not re.fullmatch(r'\d{4}',name):parents.append(name)
    result=[]
    def add(title,author,source,strength):
        if useful_title(title) and (not author or useful_author(author)):
            item=dict(title=title.strip(' ._-'),author=author.strip(' ._-'),source=source,strength=strength)
            key=(normalized(item['title']),normalized(item['author']))
            if key not in {(normalized(v['title']),normalized(v['author'])) for v in result}:result.append(item)
    for index,parent in enumerate(parents):add(stem,parent,'filename + parent folder',.78-index*.08)
    parts=[v.strip() for v in re.split(r'\s+-\s+|\s+–\s+|\s+—\s+',stem,maxsplit=1)]
    if len(parts)==2:
        left,right=parts
        parent=parents[0] if parents else ''
        if parent:
            left_author=max(author_score(left,parent),author_score(parent,left))
            right_author=max(author_score(right,parent),author_score(parent,right))
            if left_author>right_author:add(right,parent,'filename + parent folder',.84)
            elif right_author>left_author:add(left,parent,'filename + parent folder',.84)
        add(left,right,'filename pattern: title - author',.68)
        add(right,left,'filename pattern: author - title',.64)
    if not result:add(stem,'','filename',.35)
    return result


def _add_hypothesis(items,item):
    if not useful_title(item.get('title','')):return
    clean={k:item.get(k,'') for k in ('title','author','source','strength')}
    clean['author']=clean['author'] if useful_author(clean['author']) else ''
    clean['strength']=max(0,min(1,float(clean['strength'] or 0)))
    key=(normalized(clean['title']),normalized(clean['author']))
    old=next((v for v in items if (normalized(v['title']),normalized(v['author']))==key),None)
    if old:
        if clean['strength']>old['strength']:old.update(clean)
    else:items.append(clean)


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
    hypotheses=[]
    if embedded:_add_hypothesis(hypotheses,evidence)
    if not embedded or not evidence['author']:
        meta=_calibre_metadata(path,converter)
        if useful_title(meta.get('title','')) and (normalized(meta['title'])!=normalized(path.stem.replace('_',' ')) or useful_author(meta.get('author',''))):
            evidence.update(title=meta['title'],author=meta.get('author','') if useful_author(meta.get('author','')) else '',source='embedded metadata (Calibre)',strength=.96 if useful_author(meta.get('author','')) else .85)
            _add_hypothesis(hypotheses,evidence)
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
    _add_hypothesis(hypotheses,page)
    for item in _path_hypotheses(path):_add_hypothesis(hypotheses,item)
    if page['strength']>=.9 or page['strength']>evidence['strength']:
        evidence.update(page)
    if not evidence.get('title') and hypotheses:evidence.update(hypotheses[0])
    evidence['title']=evidence['title'][:1000];evidence['author']=evidence['author'][:1000]
    evidence['hypotheses']=sorted(hypotheses,key=lambda v:v['strength'],reverse=True)[:10]
    return evidence


def title_score(left,right):
    left,right=normalized(left),normalized(right)
    if not left or not right:return 0
    if left==right:return 1.0
    # Different volume numbers should never be treated as near-identical titles.
    if re.findall(r'\b(?:\d+|i{1,3}|iv|vi{0,3}|ix|x)\b',left)!=re.findall(r'\b(?:\d+|i{1,3}|iv|vi{0,3}|ix|x)\b',right):return 0
    sequence=(_rf_ratio(left,right)/100) if _rf_ratio else SequenceMatcher(None,left,right).ratio()
    a=[v for v in left.split() if v not in {'a','an','the'}]
    b=[v for v in right.split() if v not in {'a','an','the'}]
    aset,bset=set(a),set(b)
    token_dice=2*len(aset&bset)/(len(aset)+len(bset)) if aset and bset else 0
    token_set=(_rf_token_set_ratio(left,right)/100) if _rf_token_set_ratio else token_dice
    def trigrams(value):
        padded='  '+value+'  '
        return {padded[i:i+3] for i in range(max(0,len(padded)-2))}
    ta,tb=trigrams(left),trigrams(right)
    trigram=2*len(ta&tb)/(len(ta)+len(tb)) if ta and tb else 0
    # A subtitle or edition phrase may be absent on one source. Requiring at
    # least two shared words avoids treating single-word series titles as the
    # same book merely because one contains the other.
    containment=.94 if min(len(aset),len(bset))>=2 and (aset<=bset or bset<=aset) else 0
    return min(1,max(sequence,token_dice*.98,token_set*.96,trigram*.94,containment))


def author_score(left,right):
    def tokens(value):
        value=re.split(r';|\s+and\s+|&',value,maxsplit=1)[0]
        if ',' in value:
            last,first=value.split(',',1);value=first+' '+last
        return normalized(value).split()
    a,b=tokens(left),tokens(right)
    if a==b and a:return 1.0
    if len(a)<2 or len(b)<2:return 0
    last=SequenceMatcher(None,a[-1],b[-1]).ratio()
    if last<.82:return 0
    last_factor=1 if a[-1]==b[-1] else .9
    first_a,first_b=a[0],b[0]
    if first_a==first_b:return .98*last_factor
    # Initials, omitted middle names, and shortened given names are common in
    # catalogues. Conflicting full given names still receive no credit.
    if first_a[0]==first_b[0] and (len(first_a)==1 or len(first_b)==1):return .96*last_factor
    if first_a[0]==first_b[0] and min(len(first_a),len(first_b))>=3 and (first_a.startswith(first_b) or first_b.startswith(first_a)):return .93*last_factor
    if SequenceMatcher(None,first_a,first_b).ratio()>=.78:return .9*last_factor
    return 0


def match_score(evidence,candidate):
    """Return an explainable 0..1 accuracy score for an online candidate."""
    hypotheses=evidence.get('hypotheses') or [evidence]
    ranked=[]
    for item in hypotheses:
        item_title=title_score(item.get('title',''),candidate.get('title',''))
        item_has_author=bool(item.get('author'))
        item_author=author_score(item['author'],candidate.get('author','')) if item_has_author else 0
        item_strength=max(0,min(1,float(item.get('strength',0))))
        if item_has_author:item_score=.45*item_strength+.40*item_title+.15*item_author
        else:item_score=.45*item_strength+.55*item_title
        ranked.append((item_score,item_title,item_author,item_strength,item_has_author,item))
    _,title,author,strength,has_author,used=max(ranked,key=lambda v:v[0])
    exact_isbn=bool(evidence.get('isbn') and isbn13(evidence['isbn']) in {isbn13(v) for v in candidate.get('isbns',[]) if valid_isbn(v)})
    verified_isbn=exact_isbn and (strength<.85 or title>=.75)
    if verified_isbn:
        score=1.0
    elif strength>=.95 and title==1 and (not has_author or author>=.98):
        score=1.0
    elif has_author:
        score=.45*strength+.40*title+.15*author
    else:
        # When no author was found, its weight moves to title agreement rather
        # than penalizing otherwise strong embedded/title-page evidence.
        score=.45*strength+.55*title
    if not verified_isbn and (title<.70 or (has_author and author==0)):score=min(score,.799)
    score=max(0,min(1,score))
    accuracy=int(score*100+1e-9)
    parts={'contents':round(strength*100),'online_title':round(title*100),'author':round(author*100) if has_author else None,'isbn':verified_isbn,'evidence_source':used.get('source','')}
    if verified_isbn:reason='100% accuracy: verified ISBN match'
    elif score==1:reason='100% accuracy: inside-book title and internet title agree exactly'+(' and the author agrees' if has_author else '')
    else:
        details=f"{parts['evidence_source'] or 'inside-book'} evidence {parts['contents']}%, online title agreement {parts['online_title']}%"
        if has_author:details+=f", author agreement {parts['author']}%"
        reason=f'{accuracy}% accuracy: {details}'
    return score,accuracy,parts,reason


def choose_match(evidence,candidates):
    ranked=[];seen=set()
    for candidate in candidates:
        if not useful_title(candidate.get('title','')) or not useful_author(candidate.get('author','')):continue
        key=(normalized(candidate['title']),normalized(candidate['author']))
        if key in seen:continue
        seen.add(key)
        score,accuracy,parts,reason=match_score(evidence,candidate)
        ranked.append((score,{**candidate,'accuracy':accuracy,'score':round(score,4),'score_components':parts,'score_reason':reason},reason))
    ranked.sort(key=lambda v:v[0],reverse=True)
    scored=[v[1] for v in ranked]
    if not ranked:return dict(match=None,candidates=[],score=0,accuracy=0,reason='No usable internet matches were found')
    best=ranked[0]
    if best[0]<.80:return dict(match=None,candidates=scored,score=best[0],accuracy=best[1]['accuracy'],reason=best[2]+'; below the 80% automatic threshold')
    exact_isbn_winner=bool(best[1]['score_components'].get('isbn'))
    if not exact_isbn_winner and len(ranked)>1 and ranked[1][0]>=.80 and (ranked[1][0]==1 or best[0]-ranked[1][0]<.06):
        return dict(match=None,candidates=scored,score=best[0],accuracy=best[1]['accuracy'],reason=f"{best[1]['accuracy']}% best accuracy, but several candidates are equally plausible; review required")
    components=best[1]['score_components']
    if not components['isbn'] and components['contents']<.75*100:
        return dict(match=None,candidates=scored,score=best[0],accuracy=best[1]['accuracy'],reason=best[2]+'; evidence source is too weak for an automatic update')
    return dict(match=best[1],candidates=scored,score=best[0],accuracy=best[1]['accuracy'],reason=best[2]+'; applied automatically')
