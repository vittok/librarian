import base64
import json
from pathlib import Path
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from bookroom.core import Library,extract
from bookroom.preview import Previews,TEXT_LIMIT,find_tool


@pytest.fixture
def library(tmp_path):
    lib=Library(tmp_path/'catalogue');lib.converter=None
    yield lib
    lib.close()


def test_preview_returns_opening_text_and_does_not_modify_or_reindex(library,tmp_path):
    path=tmp_path/'Book.txt';path.write_text('Title: A Book\nAuthor: A Writer\n\nOpening paragraph.')
    bid=library.index_file(path);before=path.read_bytes();count=library.stats()['count']
    result=library.book_preview(bid)
    assert result['book']['id']==bid and result['filename']=='Book.txt'
    assert result['has_text'] and 'Opening paragraph.' in result['text']
    assert not result['pdf_images'] and path.read_bytes()==before
    assert library.stats()['count']==count


def test_preview_truncates_long_text(library,tmp_path):
    path=tmp_path/'Long.txt';path.write_text('opening '*20000)
    bid=library.index_file(path)
    result=library.book_preview(bid)
    assert len(result['text'])==TEXT_LIMIT and result['truncated']


def test_preview_returns_fresh_catalogue_metadata_while_reusing_text_cache(library,tmp_path):
    path=tmp_path/'Book.txt';path.write_text('Read these words.')
    bid=library.index_file(path);calls=[]
    def reader(*args,**kwargs):calls.append(True);return extract(*args,**kwargs)
    library.previews=Previews(reader)
    library.book_preview(bid)
    library.update_metadata(bid,dict(title='Updated title',author='Updated author'))
    result=library.book_preview(bid)
    assert result['book']['title']=='Updated title' and len(calls)==1
    path.write_text('A different opening paragraph, after an external edit.')
    assert 'different opening' in library.book_preview(bid)['text'] and len(calls)==2


def test_missing_network_source_has_actionable_error(library,tmp_path):
    path=tmp_path/'Share.txt';path.write_text('network book')
    bid=library.index_file(path);library.book_preview(bid);path.unlink()
    with pytest.raises(ValueError,match='Reconnect its network share'):library.book_preview(bid)
    assert library.catalogue()['total']==1


def test_network_path_book_can_be_previewed(library,tmp_path,monkeypatch):
    from bookroom import locations
    mount=tmp_path/'mounted';mount.mkdir();(mount/'Network.txt').write_text('Opening text on a network share.')
    monkeypatch.setattr(locations.platform,'system',lambda:'Linux')
    monkeypatch.setattr(locations,'mounted_shares',lambda:[locations.Mount(mount,locations.Share('nas','Books'))])
    source=locations.resolve_location('smb://nas/Books/Network.txt').path
    bid=library.index_file(source)
    assert 'network share' in library.book_preview(bid)['text']


def test_html_preview_is_data_not_executable_html(library,tmp_path):
    path=tmp_path/'Unsafe.html';path.write_text('<h1>A title</h1><script>window.PREVIEW_EXECUTED=true;</script><p>Safe paragraph.</p><img src="https://unrelated.invalid/track">')
    bid=library.index_file(path);result=library.book_preview(bid)
    assert 'Safe paragraph.' in result['text']
    assert 'PREVIEW_EXECUTED' not in result['text'] and '<img' not in result['text']
    assert 'image' not in result


def test_calibre_fallback_is_temporary_and_read_only(tmp_path,monkeypatch):
    path=tmp_path/'Book.mobi';path.write_bytes(b'original')
    service=Previews(lambda *a,**kw:('Book','','','Metadata only'))
    outputs=[]
    def convert(command,**kwargs):
        output=Path(command[2]);outputs.append(output);output.write_text('Converted opening text')
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr('bookroom.preview.subprocess.run',convert)
    result=service.opening(path,'/converter/ebook-convert')
    assert result['source']=='Calibre text conversion' and result['text']=='Converted opening text'
    assert not outputs[0].exists() and path.read_bytes()==b'original'


def test_failed_or_timed_out_conversion_has_clear_result(tmp_path,monkeypatch):
    path=tmp_path/'Book.mobi';path.write_bytes(b'original')
    service=Previews(lambda *a,**kw:('Book','','','Metadata only'))
    def timeout(command,**kwargs):raise subprocess.TimeoutExpired(command,60)
    monkeypatch.setattr('bookroom.preview.subprocess.run',timeout)
    with pytest.raises(ValueError,match='took too long'):service.opening(path,'/converter/ebook-convert')
    monkeypatch.setattr('bookroom.preview.subprocess.run',lambda cmd,**kw:subprocess.CompletedProcess(cmd,1))
    assert not service.opening(path,'/converter/ebook-convert')['has_text']


def pdf_fixture(path):
    """One-page PDF with visible title text and a coloured rectangle."""
    stream=b'0.2 0.4 0.3 rg 30 680 535 70 re f\nBT /F1 24 Tf 50 640 Td (Preview title page) Tj ET\n'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'endstream']
    data=b'%PDF-1.4\n';offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(data);data+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    data+=b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    data+=f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


def test_real_pdf_opening_page_renders_as_bounded_png(library,tmp_path):
    if not find_tool('pdftoppm'):pytest.skip('Install Poppler for the real PDF rendering smoke test')
    path=tmp_path/'Title.pdf';pdf_fixture(path);bid=library.index_file(path)
    result=library.book_preview(bid)
    assert result['pdf_images'] and 1<=result['pdf_pages']<=12
    if result['page_count'] is not None:assert result['page_count']==1
    image=library.preview_page(bid,1)
    png=base64.b64decode(image['image'].split(',',1)[1])
    assert png.startswith(b'\x89PNG\r\n\x1a\n') and len(png)<8_000_000
    with pytest.raises(ValueError):library.preview_page(bid,2)


def test_page_parameters_and_non_pdf_requests_are_rejected(library,tmp_path):
    path=tmp_path/'Book.txt';path.write_text('text');bid=library.index_file(path)
    with pytest.raises(ValueError,match='PDF books only'):library.preview_page(bid,1)
    service=Previews(extract)
    for page in [0,13,-1,True]:
        with pytest.raises(ValueError,match='between 1 and 12'):service.page(tmp_path/'Book.pdf',page)


def test_converter_bundled_renderer_is_discovered(tmp_path,monkeypatch):
    monkeypatch.setattr('bookroom.preview.shutil.which',lambda *a:None)
    tool=tmp_path/'bin/pdftoppm';tool.parent.mkdir();tool.write_text('renderer');tool.chmod(0o755)
    assert find_tool('pdftoppm',str(tmp_path/'ebook-convert'))==str(tool)


def test_preview_cache_is_bounded():
    service=Previews(extract)
    for i in range(25):service.remember(i,{'text':'sample'},3_000_000)
    assert len(service.cache)<=16 and service.bytes<=32_000_000


def test_http_preview_requires_token_and_only_accepts_catalogue_ids(library,tmp_path):
    from bookroom.server import make_server
    path=tmp_path/'Book.txt';path.write_text('<script>literal book text</script>')
    bid=library.index_file(path);server=make_server(library,0)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(url) as response:token=response.read().decode().split('name="bookroom-token" content="')[1].split('"')[0]
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(url+f'/api/book-preview?id={bid}')
        assert error.value.code==403
        request=urllib.request.Request(url+f'/api/book-preview?id={bid}',headers={'X-Bookroom-Token':token})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)['text']=='<script>literal book text</script>'
            assert response.headers['Content-Type']=='application/json'
            assert response.headers['Cache-Control']=='no-store'
        request=urllib.request.Request(url+'/api/book-preview?id=999999&path=/etc/passwd',headers={'X-Bookroom-Token':token})
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
        assert error.value.code==400
    finally:server.shutdown();server.server_close()
