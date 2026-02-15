# EPUB to PDF Converter

A local, privacy-first EPUB to PDF converter that uses headless Chromium for pixel-perfect rendering. No data is sent online — everything runs 100% on your machine.

## Why This Exists

Most "free" online EPUB-to-PDF converters are sketchy — they upload your files to unknown servers, inject ads, or produce terrible output. This tool uses the same rendering approach as [Calibre](https://calibre-ebook.com/) (headless Chromium / Qt WebEngine) to produce high-quality PDFs while keeping your files completely local.

## Features

- **Chromium rendering engine** — same approach as Calibre for pixel-perfect CSS, fonts, SVGs, and images
- **Full-bleed cover page** — extracts the EPUB cover image and renders it as the first page
- **Working table of contents** — internal TOC links are clickable and jump to the correct chapter
- **Preserves EPUB styling** — CSS stylesheets, custom fonts, and layout are faithfully rendered
- **SVG support** — scene break ornaments and vector graphics render correctly
- **PDF metadata** — title, author, and producer are embedded in the output PDF
- **PDF bookmarks** — chapter headings are extracted as navigable PDF outline entries
- **Trade paperback size** — outputs at 5.5" x 8.5" (configurable in code)
- **100% local** — no network requests, no uploads, no tracking

## How It Works

The conversion pipeline mirrors Calibre's architecture:

1. **Extract** — EPUB files are just ZIP archives; contents are extracted to a temp directory
2. **Parse** — the OPF manifest is read for spine order, metadata, and cover image location
3. **Cover** — the cover image is scaled to a full-bleed PDF page
4. **Combine** — all chapters are merged into a single HTML document with internal anchor links (this is what makes TOC links work)
5. **Render** — headless Chromium loads the combined HTML and prints to PDF via `page.pdf()`
6. **Merge** — the cover page is prepended and PDF metadata is added

## Installation

Requires **Python 3.8+**.

```bash
# Install Python dependencies
pip install -r requirements.txt

# Download headless Chromium (one-time, ~170 MB)
playwright install chromium
```

## Usage

### Basic

```bash
python epub_to_pdf.py "MyBook.epub"
```

Output will be saved as `MyBook.pdf` in the same directory.

### Custom Output Path

```bash
python epub_to_pdf.py "MyBook.epub" "output/MyBook-print.pdf"
```

### Example Output

```
EPUB to PDF Converter (Chromium-based)
==================================================
Input:  MyBook.epub
Output: MyBook.pdf

Step 1: Extracting EPUB...
Step 2: Parsing OPF manifest...
  Title:    My Book Title
  Author:   Author Name
  Chapters: 57
  Cover:    cover.jpg

Step 3: Creating cover page...
  Cover page created

Step 4: Combining chapters into single HTML...
  Combined 57 chapters
  Internal TOC links rewritten to anchors

Step 5: Rendering with Chromium...
  Launching Chromium...
  Loading combined HTML (958 KB)...
  Rendering PDF with Chromium print engine...

Step 6: Merging final PDF...
  Added cover page
  Added 462 content pages

==================================================
Done! PDF saved to: MyBook.pdf
  Total pages: 463
  File size:   2.33 MB
```

## Requirements

| Package | Purpose |
|---------|---------|
| [playwright](https://playwright.dev/python/) | Headless Chromium browser for HTML-to-PDF rendering |
| [pypdf](https://pypdf.readthedocs.io/) | PDF merging (cover + content) with link preservation |
| [Pillow](https://pillow.readthedocs.io/) | Cover image processing and PDF page generation |

## Technical Notes

- **Why .html not .xhtml?** Chromium's strict XHTML parser silently fails on EPUB namespace attributes (`xmlns:epub`, `epub:type`), producing an empty body with zero pagination. Saving as HTML5 and stripping namespaces fixes this.
- **Why `append()` not `add_page()`?** pypdf's `add_page()` copies page content but drops named destinations. `append()` preserves the full PDF structure including link targets, which is critical for working TOC links.
- **Why combine chapters?** Rendering each chapter as a separate PDF breaks cross-file links (e.g., TOC pointing to `chapter-001.xhtml`). Combining into one HTML with rewritten anchor links lets Chromium resolve everything internally.
- **Viewport sizing** — the viewport is set to 528x816px (5.5"x8.5" at 96 DPI) to prevent small-screen `@media` queries from activating and changing the layout.

## Compatibility

Tested with:
- EPUB 2 and EPUB 3 files
- Vellum-generated EPUBs
- EPUBs with embedded fonts, SVGs, and complex CSS
- Windows, macOS, and Linux

## License

MIT License. See [LICENSE](LICENSE) for details.
