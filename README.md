# Bookroom

A local Python app for bringing scattered ebooks into one searchable catalogue, reviewing online metadata, converting formats, and safely organizing files in batches. The interface runs in your browser; the application and catalogue run on your computer.

## Start

Python 3.11 or newer is required. The application itself has **no required Python dependencies**.

From this folder:

```bash
python3 run.py
```

On Windows, use `py run.py`. Bookroom opens [http://127.0.0.1:8765](http://127.0.0.1:8765). Keep the terminal open while using it; press Ctrl+C to stop. Queued jobs finish before shutdown. To use a different port:

```bash
python3 run.py --port 8766
```

You can also install it into a virtual environment with `pip install .` and run `bookroom`.

### Enable conversion

Install [Calibre](https://calibre-ebook.com/download), which provides `ebook-convert`, then restart Bookroom. Verify the command is available with `ebook-convert --version` in your terminal. Bookroom disables Convert and displays setup guidance when the converter is unavailable.

If Calibre is not on PATH, point Bookroom at the **executable**, not its installation directory:

```bash
python3 run.py --converter /opt/calibre/ebook-convert
```

On macOS the executable is commonly `/Applications/calibre.app/Contents/MacOS/ebook-convert`. On Windows it is commonly `C:\Program Files\Calibre2\ebook-convert.exe`. You may also set the `BOOKROOM_CONVERTER` environment variable. The `--converter` option is remembered in the selected catalogue's settings for later starts.

PDF text indexing uses `pdftotext` if available. Alternatively, install the optional Python reader:

```bash
python3 -m pip install '.[pdf]'
```

## Use your library

1. **Import folder**: type a path or use Browse. Scans include nested folders. Importing indexes files in place without copying or moving them. Click an imported folder in the sidebar to scan it again after adding or changing books. These are manual scans, not filesystem watchers.
2. **Search**: enter words from titles, authors, or extracted book text. Matching is case and accent insensitive; all words must match, and the final letters of each word can match a longer word. Filter by format. Results are sorted by title and paginated at 60 per page.
3. **Duplicates**: shows files with identical SHA-256 content hashes. This detects identical files, rather than different editions or formats of the same work. Bookroom never automatically deletes duplicates.
4. **Edit details**: click a book title or its ellipsis button to edit the catalogue title, author, and first-publication year.
5. **Find metadata**: select up to 50 books and fetch candidate matches from Open Library. Choose a match and click Apply for each book. No match is applied automatically. Failed lookups leave existing metadata intact. Online lookups send the title and optional author to Open Library; book contents and paths are not sent. Work-level results may combine multiple editions and languages, so review carefully.
6. **Batch operations**: select books, then choose Convert, Copy, Move, or Organize. Selection persists across result pages until cleared. A batch accepts up to 1,000 selected files. Choose a destination and preview every resulting path before running it. Previews expire after 15 minutes and can only be executed once.
7. **Activity**: see queue progress, individual file outcomes, errors, and timestamps. Jobs run sequentially in a background thread while the interface remains usable.

### Operations

- **Convert** creates one or several output formats per selected source. Outputs already matching the source format are skipped. Successful outputs are indexed and inherit catalogue metadata. Originals are kept. A failed conversion is reported per file; other conversions continue. Each conversion has a ten-minute timeout.
- **Copy** keeps the original filename and source file, and indexes the destination.
- **Move** copies and indexes the destination before removing the source, then removes the source's catalogue entry. If indexing or verification fails, the source is retained. The operation is **not an undoable transaction**. If a crash or disk failure occurs between steps, both copies may remain. Review the log and scan affected folders before retrying interrupted jobs.
- **Organize** uses `Author / Title / Title.format` under your destination folder. Choose copy or move. Invalid filename characters are replaced, and metadata cannot escape the destination directory. Empty source folders are left in place.
- Filename collisions get numbered names such as `Title (2).epub`. A file appearing after preview causes that operation to fail instead of overwriting it. Source changes after preview are also rejected. Batch jobs may partially succeed; the log records each outcome.

Catalogue metadata changes do not modify original book files. Convert embeds the current title and author into the new output when the format supports them. Copy and Move preserve the original file bytes. Rescans retain manually edited or online-applied metadata.

## Formats and indexing

Recognized inputs:

`AZW, AZW3, AZW4, CBZ, CBR, CB7, CBC, CHM, DJVU, DOCX, EPUB, FB2, FBZ, HTML, HTM, HTMLZ, KEPUB, LIT, LRF, MOBI, ODT, PDF, PRC, PDB, PML, RB, RTF, SNB, TCR, TXT, TXTZ`

Conversion outputs:

`EPUB, PDF, AZW3, MOBI, DOCX, TXT, FB2, RTF, HTMLZ, KEPUB, LIT, LRF, PDB, PMLZ, RB, SNB, TCR, TXTZ, ZIP`

Conversion relies on the installed Calibre version and its available plugins. A recognized extension does not guarantee a valid or convertible file. Unsupported legacy files, corruption, and DRM protection can cause a conversion to fail. Bookroom does not remove DRM. PDF reflow and complex layouts may lose formatting; review converted output. See [Calibre's format support](https://manual.calibre-ebook.com/faq.html#what-formats-does-calibre-support-conversion-to-from).

Built-in content extraction covers TXT, PML, HTML, EPUB/KEPUB, DOCX, ODT, FB2, FBZ, HTMLZ and TXTZ. PDF extraction requires `pdftotext` or the optional `pypdf` package. Other formats remain searchable by catalogue title and author. EPUB, DOCX, and FB2 embedded title/author metadata is extracted when present; other entries initially use the filename as the title. Each file shows **Text indexed** or **Metadata only**, with an explanation in its details.

Text indexing is limited to the first two million characters per book, archive contents to 100 MB uncompressed, individual archive members to 16 MB, and PDF extraction to the first 1,000 pages. This is a text search engine, not OCR: image-only PDFs and comic pages need external OCR before their contents become searchable. Books that fail extraction are still catalogued.

## Storage and privacy

By default, the SQLite catalogue and job log are stored in `~/.local/share/bookroom`. The database contains local paths, hashes, metadata, and extracted text. Back up that directory while the app is stopped; the database is an index, **not a backup of your books**.

Choose a separate catalogue if needed:

```bash
python3 run.py --data-dir /path/to/my-catalogue
```

`BOOKROOM_DATA_DIR` also sets the default. Bookroom listens only on `127.0.0.1`, checks Host and Origin, and requires a random session token on API requests. It is a single-user local app; it is not intended to be exposed to a network. Normal library operations work offline; metadata searches require internet access. No external fonts, analytics, or cover-image services are loaded.

Missing source files remain in the catalogue until restored or moved through Bookroom. Batch operations report missing files rather than removing catalogue entries silently. On startup, unfinished jobs are marked interrupted and are not replayed automatically. Activity shows the latest 30 jobs. Metadata lookup requests are spaced at least 1.1 seconds apart and time out after 25 seconds. Closing the metadata dialog stops further requests after the current one finishes.

## Development and validation

```bash
python3 -m pip install '.[dev]'
python3 -m pytest -q
```

Tests use temporary books and directories. They cover text/EPUB extraction, FTS search and duplicate hashes, metadata preservation, batch previews, file collisions, source-change protection, moving and organization, conversion failure isolation, metadata response handling, and local HTTP authentication. Converter unit tests use a substitute executable, so they do not replace a real Calibre smoke test.

The UI is plain local HTML/CSS/JavaScript with no frontend build step. The backend uses Python's HTTP server, SQLite FTS5, and a single-worker job queue. SQLite must include FTS5, as it does in standard Python distributions on common desktop systems.

Online metadata API documentation: [Open Library Search API](https://openlibrary.org/dev/docs/api/search).
