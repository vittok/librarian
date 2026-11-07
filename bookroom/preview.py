"""Read-only, bounded book previews. Book HTML is returned only as plain text."""
from collections import OrderedDict
import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

TEXT_LIMIT = 60_000
PDF_PAGES = 12
PNG_LIMIT = 8_000_000


def file_signature(path):
    try:
        info=path.stat()
        if not path.is_file():raise OSError('Not a regular book file')
        return info.st_size,info.st_mtime_ns
    except OSError as error:
        raise ValueError('Book file is unavailable. Reconnect its network share or restore the file, then try again.') from error


def find_tool(name, converter=None):
    found=shutil.which(name)
    if found:return found
    if converter:
        parent=Path(converter).parent
        executable=name+('.exe' if os.name=='nt' else '')
        for candidate in (parent/executable,parent/'bin'/executable):
            if candidate.is_file() and os.access(candidate,os.X_OK):return str(candidate)
    return None


def pdf_page_count(path, converter):
    tool=find_tool('pdfinfo',converter)
    if not tool:return None
    try:
        result=subprocess.run([tool,str(path)],capture_output=True,timeout=15)
        match=re.search(rb'^Pages:\s+(\d+)',result.stdout,re.M)
        return int(match[1]) if result.returncode==0 and match else None
    except (OSError,subprocess.SubprocessError):return None


class Previews:
    def __init__(self, extractor):
        self.extractor=extractor
        self.slots=threading.BoundedSemaphore(2)
        self.lock=threading.Lock()
        self.cache=OrderedDict()
        self.bytes=0

    def cached(self,key):
        with self.lock:
            value=self.cache.get(key)
            if value:self.cache.move_to_end(key);return value[0]
        return None

    def remember(self,key,value,size):
        with self.lock:
            previous=self.cache.pop(key,None)
            if previous:self.bytes-=previous[1]
            self.cache[key]=(value,size);self.bytes+=size
            while len(self.cache)>16 or self.bytes>32_000_000:
                _,(_,removed)=self.cache.popitem(last=False);self.bytes-=removed
        return value

    def opening(self,path,converter=None):
        path=Path(path);signature=file_signature(path)
        key=('text',str(path),signature,converter)
        cached=self.cached(key)
        if cached is not None:return cached
        if not self.slots.acquire(timeout=1):raise ValueError('Other book previews are loading. Please try again in a moment.')
        try:
            _,_,text,status=self.extractor(path,text_limit=TEXT_LIMIT,pdf_pages=PDF_PAGES)
            used_calibre=False
            if not text.strip() and converter and path.suffix.lower()!='.pdf':
                try:
                    with tempfile.TemporaryDirectory(prefix='bookroom-preview-') as folder:
                        output=Path(folder)/'opening.txt'
                        result=subprocess.run([converter,str(path),str(output)],capture_output=True,timeout=60,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'})
                        if result.returncode==0 and output.is_file():
                            with output.open('rb') as source:text=source.read(TEXT_LIMIT*4).decode('utf-8','replace')
                            used_calibre=True
                except subprocess.TimeoutExpired as error:raise ValueError('Preview conversion took too long. Try another format of this book.') from error
                except OSError as error:raise ValueError('The ebook preview converter could not run. Check the Calibre installation.') from error
            if file_signature(path)!=signature:raise ValueError('Book changed while loading its preview. Please try again.')
            text=text.replace('\x00','')[:TEXT_LIMIT]
            is_pdf=path.suffix.lower()=='.pdf'
            renderer=bool(is_pdf and find_tool('pdftoppm',converter))
            count=pdf_page_count(path,converter) if is_pdf and renderer else None
            result=dict(text=text,has_text=bool(text.strip()),truncated=len(text)>=TEXT_LIMIT,
                        pdf_images=renderer,pdf_pages=min(count or PDF_PAGES,PDF_PAGES) if renderer else 0,
                        page_count=count,source='Calibre text conversion' if used_calibre else 'Extracted opening text',
                        note='Opening text only (up to 60,000 characters and 12 PDF pages); layout may differ from the original.' if text.strip() else
                        'No readable text was found. Image-only books need OCR for text, but PDF opening pages can be viewed when a renderer is available.')
            return self.remember(key,result,len(text.encode('utf-8'))+1024)
        finally:self.slots.release()

    def page(self,path,page,converter=None):
        if type(page) is not int or not 1<=page<=PDF_PAGES:raise ValueError('Preview page must be between 1 and 12.')
        path=Path(path)
        if path.suffix.lower()!='.pdf':raise ValueError('Page images are available for PDF books only.')
        signature=file_signature(path);key=('page',str(path),signature,page,converter)
        cached=self.cached(key)
        if cached is not None:return cached
        tool=find_tool('pdftoppm',converter)
        if not tool:raise ValueError('PDF page preview needs pdftoppm (Poppler), which may also be bundled with Calibre. Text preview is still available when extractable.')
        if not self.slots.acquire(timeout=1):raise ValueError('Other book previews are loading. Please try again in a moment.')
        try:
            with tempfile.TemporaryDirectory(prefix='bookroom-pdf-preview-') as folder:
                prefix=Path(folder)/'page'
                try:
                    result=subprocess.run([tool,'-f',str(page),'-l',str(page),'-singlefile','-scale-to','1400','-png',str(path),str(prefix)],capture_output=True,timeout=30)
                except subprocess.TimeoutExpired as error:raise ValueError('PDF page preview took too long. Try the text preview or a different book format.') from error
                output=prefix.with_suffix('.png')
                if result.returncode or not output.is_file():raise ValueError('This PDF page could not be rendered. The page may not exist, or the file may be damaged or password-protected.')
                if output.stat().st_size>PNG_LIMIT:raise ValueError('Rendered PDF page exceeds the preview size limit.')
                png=output.read_bytes()
            if not png.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('The PDF renderer did not produce a valid page image.')
            if file_signature(path)!=signature:raise ValueError('Book changed while rendering its preview. Please try again.')
            result=dict(page=page,image='data:image/png;base64,'+base64.b64encode(png).decode('ascii'))
            return self.remember(key,result,len(result['image']))
        finally:self.slots.release()
