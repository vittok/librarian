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

### Automatic Calibre setup

On startup, Bookroom checks your saved converter, PATH, standard installation locations, and its own managed runtime. If a working `ebook-convert` is missing, it automatically downloads and installs [Calibre](https://calibre-ebook.com/download) before opening the browser. The first launch needs internet access, several minutes, and roughly 1 GB of available disk space (temporary downloads may need additional space). Subsequent launches reuse the converter without downloading it again. Existing working installations are not upgraded or replaced.

The private runtime is stored under `<data-dir>/runtime/calibre`; system packages and PATH are not changed and administrator access is not requested:

- **Linux** uses Calibre's [official isolated installer](https://calibre-ebook.com/download_linux), which verifies its downloaded archive. Required native libraries must already be installed; the setup log explains missing libraries.
- **Windows** uses the [official portable installer's automated destination argument](https://calibre-ebook.com/download_portable), with the download's SHA-512 checksum checked against Calibre's published checksum before it runs. Checksum requests validate TLS against the public Calibre CA distributed in its official Linux installer; this trust is scoped to the checksum connection and does not change system trust.
- **macOS** checks the official DMG's SHA-512 checksum, mounts it read-only, copies `calibre.app` into the private runtime, and detaches the image. Normal macOS security checks remain enabled. Current Calibre releases require a compatible macOS version.

The terminal reports setup progress, with installer output in `<data-dir>/calibre-install.log`. If setup fails (for example, while offline or missing native libraries), Bookroom still opens for indexing and organization, displays the reason conversion is unavailable, and retries on the next startup. Concurrent starts share an OS installation lock. New runtimes are validated before replacing an incomplete managed runtime; previous runtimes are retained until that replacement succeeds.

To skip automatic installation, for example for offline catalogue use:

```bash
python3 run.py --no-install-calibre
```

You can still install Calibre yourself or explicitly choose its **executable**, not the installation directory:

```bash
python3 run.py --converter /opt/calibre/ebook-convert
```

On macOS the executable is commonly `/Applications/calibre.app/Contents/MacOS/ebook-convert`. On Windows it is commonly `C:\Program Files\Calibre2\ebook-convert.exe`. You may also set `BOOKROOM_CONVERTER`. Explicit paths must run successfully; invalid overrides produce a clear startup error. The converter path is remembered in the selected catalogue's settings for later starts.

PDF text indexing uses `pdftotext` if available. Alternatively, install the optional Python reader:

```bash
python3 -m pip install '.[pdf]'
```

## Use your library

1. **Import folder**: type a local path, connected network location, or use Browse. The folder browser includes connected network share and mounted-volume shortcuts. Scans include nested folders. Importing indexes files in place without copying or moving them. Click an imported folder in the sidebar to scan it again after adding or changing books. These are manual scans, not filesystem watchers. Enable **Automatically identify books online after scanning** to queue metadata enrichment once the scan completes; this option is off by default.
2. **Search**: enter words from titles, authors, or extracted book text. Matching is case and accent insensitive; all words must match, and the final letters of each word can match a longer word. Filter by format. Results are sorted by title and paginated at 60 per page.
3. **Duplicates**: shows files with identical SHA-256 content hashes. This detects identical files, rather than different editions or formats of the same work. Bookroom never automatically deletes duplicates.
4. **Edit details**: click a book title or its ellipsis button to edit the catalogue title, author, and first-publication year.
5. **Metadata**: use **Auto metadata** to inspect selected books and automatically apply confident Open Library matches, or **Review matches** to inspect and choose matches manually for up to 50 books. Uncertain automatic results appear in **Metadata review**. Failed lookups leave existing metadata intact. Only inferred titles, authors and ISBNs are sent online; book text and local paths are not uploaded. Work-level results may combine multiple editions and languages, so review carefully.
6. **Batch operations**: select books on the current page, or click **Select all books in library** for a selection across every page and view. Selection persists across result pages until cleared. The former 1,000-book limit is removed. Choose Convert, Copy, Move, or Organize, set a destination, and preview the batch. Large previews show the first 200 output paths plus the full operation count; running the batch processes the entire selection. Previews expire after 15 minutes and can only be executed once.
7. **Activity**: see queue progress, file outcomes, errors, and timestamps. The latest 1,000 log messages per job are displayed. Jobs run sequentially in a background thread while the interface remains usable. Automatic metadata jobs can be cancelled and their applied changes undone here.

### Identify titles and authors automatically

Select books (including the whole library) and click **Auto metadata**. Bookroom reads embedded metadata and a bounded sample of opening text, looks for labelled title/author fields, title-page bylines and checksum-valid ISBNs, then queries Open Library. For automatic inspection, built-in text extraction is limited to 120,000 characters and the first 12 PDF pages. Calibre's read-only `ebook-meta` can inspect other supported formats, including MOBI/AZW3. When usable metadata and text are missing, a temporary TXT conversion can provide title-page evidence; it times out after 90 seconds and does not change the original book.

Automatic updates require a verified ISBN result or strong agreement between inside-book evidence and the online title/author. Ambiguous matches, weak opening-line guesses, and conflicts go to **Metadata review** instead of being applied. Select those books and click **Review matches**, or edit their details. This is a heuristic identification tool: corrupted metadata, unusual layouts, or an ISBN for an advertised book can still lead to mistakes. The review queue and undo history help inspect and correct results. Image-only books still need external OCR.

**Preserve manually edited or previously confirmed metadata** is enabled by default. Uncheck it only when you want to replace those choices. Automatic updates change catalogue fields; source files and filenames stay in place. Subsequent conversions embed the updated title and author where supported. Wait for metadata enrichment to finish before preparing conversion previews, because previews record the current metadata.

Results are cached for seven days to avoid repeated requests for duplicate formats of the same book. Uncached Open Library requests remain at least 1.1 seconds apart; large libraries can take hours. Jobs can be cancelled from Activity, stopping after the current inspection or request. Each automatic change records previous and applied values. **Undo metadata updates** restores prior values only if the book has not been edited since; newer edits are preserved. Per-book change history remains in the local catalogue even when the rolling Activity log is truncated.

Library-wide selection is a server-side snapshot that lasts 24 hours. You can deselect individual books or pages from it. Books imported after the snapshot are not automatically selected, and the selection includes books hidden by the current search or format filter. Clear or reselect the library to take a new snapshot. Large batches use a small selection token rather than sending thousands of IDs from the browser.

### Import network folders

Network folders work through your operating system's filesystem access. Connect the share and sign in using Explorer, Finder, or your Linux file manager first, then enter its location in **Import folder** or **Browse**:

| Location | Example | Support |
| --- | --- | --- |
| Windows UNC | `\\server\Books\Fiction` | Direct on Windows using your Windows session; maps to an already mounted SMB share on Linux/macOS. |
| SMB URL | `smb://server/Books/Fiction` | Translated to UNC on Windows, or resolved to a connected SMB mount on Linux/macOS. `cifs://` also works. |
| File URL | `file://server/Books/Fiction` | Remote share handling as above; `file:///mnt/books` and other local file URLs also work. |
| Mapped network drive | `Z:\Books` | Windows mapped drives visible to the account running Bookroom. |
| Mounted SMB/NFS/SFTP folder | `/mnt/books`, `/media/books`, `/Volumes/Books` | Use the native mount path. |
| GNOME network folder | `smb://server/Books` | Resolves connected GVfs/FUSE shares exposed under `/run/user/<uid>/gvfs` or `~/.gvfs`. |

On macOS, use **Finder → Go → Connect to Server** to connect an SMB share. On Linux, a share opened in the file manager must also be exposed to regular filesystem applications through GVfs/FUSE (often provided by `gvfs-fuse`), or mounted to a local directory. Bookroom discovers CIFS/SMB mounts and connected GNOME shares on Linux, and SMB mounts on macOS. Other mounted protocols can be opened using their native paths. It does not scan the LAN for servers, mount shares, or manage network passwords. URLs containing credentials are rejected; use your operating system's authentication instead. IPv6 servers or custom SMB ports can be used through a mounted native folder path.

For names containing spaces or special characters, paste a normal UNC/native path or use URL encoding in URLs, such as `smb://server/Book%20shelf/Science%20%231`. Importing reads books in place. Rescans remember the logical SMB location so they can resolve a reconnected share again. If a share is offline or permissions are missing, the UI or Activity log explains the failure and previously indexed books remain searchable. Scan checks run in the background. Network delays are subject to the operating system's filesystem timeouts.

Batch destinations accept the same locations. Copying, moving, organizing, and conversion outputs require write access to the destination. Network destinations are resolved again before each file is processed; a disconnected or changed SMB mount causes that operation to fail rather than silently switching to another path. Keep the SQLite catalogue in a **local** data directory; only the book folders should live on network storage.

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

`BOOKROOM_DATA_DIR` also sets the default. Bookroom listens only on `127.0.0.1`, checks Host and Origin, and requires a random session token on API requests. It is a single-user local app; it is not intended to be exposed to a network. After initial Calibre setup, normal library operations work offline; metadata searches and first-time Calibre downloads require internet access. No external fonts, analytics, or cover-image services are loaded.

Missing source files remain in the catalogue until restored or moved through Bookroom. Batch operations report missing files rather than removing catalogue entries silently. On startup, unfinished jobs are marked interrupted and are not replayed automatically. Activity shows the latest 30 jobs. Uncached metadata lookup requests are spaced at least 1.1 seconds apart and time out after 25 seconds. Closing the metadata dialog stops further requests after the current one finishes.

## Development and validation

```bash
python3 -m pip install '.[dev]'
python3 -m pytest -q
```

Tests use temporary books and directories. They cover text/EPUB extraction, FTS search and duplicate hashes, metadata preservation, batch previews, file collisions, source-change protection, moving and organization, conversion failure isolation, metadata response handling, inside-book identification, automatic matching and review, undo and edit protection, library snapshots above 1,000 books, local HTTP authentication, network URL/UNC parsing, connected-share browsing, network imports and disconnect recovery, and migration of existing catalogue folders. Installer unit tests cover detection, platform commands, download verification, failed installs, restart reuse, and concurrent starts without downloading software. Set `BOOKROOM_CONVERTER=/path/to/ebook-convert` when running pytest to also run the real five-format conversion smoke test. The Linux first-run installer has been exercised separately with the official download; Windows and macOS installer commands are covered by unit tests and still need native OS smoke testing.

The UI is plain local HTML/CSS/JavaScript with no frontend build step. The backend uses Python's HTTP server, SQLite FTS5, and a single-worker job queue. SQLite must include FTS5, as it does in standard Python distributions on common desktop systems.

Online metadata API documentation: [Open Library Search API](https://openlibrary.org/dev/docs/api/search).
