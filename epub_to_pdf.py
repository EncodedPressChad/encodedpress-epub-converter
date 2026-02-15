"""
Local EPUB to PDF converter.
Uses the same approach as Calibre: unpack EPUB, render HTML+CSS with a real
browser engine (Chromium via Playwright), then output a single merged PDF.
No data is sent online - everything runs 100% locally.

Requirements:
    pip install ebooklib playwright pypdf beautifulsoup4 Pillow
    playwright install chromium
"""

import sys
import os
import shutil
import tempfile
import zipfile
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright
from pypdf import PdfWriter, PdfReader
from PIL import Image


def extract_epub(epub_path, output_dir):
    """Extract EPUB (which is just a ZIP) to a directory."""
    with zipfile.ZipFile(epub_path, 'r') as z:
        z.extractall(output_dir)


def parse_opf(extract_dir):
    """Parse the OPF file to get spine order, manifest items, and metadata."""
    # Find the OPF file via container.xml
    container_path = os.path.join(extract_dir, "META-INF", "container.xml")
    if not os.path.exists(container_path):
        raise FileNotFoundError("No META-INF/container.xml found - not a valid EPUB")

    tree = ET.parse(container_path)
    root = tree.getroot()
    ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
    rootfile_elem = root.find('.//c:rootfile', ns)
    if rootfile_elem is None:
        raise ValueError("No rootfile found in container.xml")

    opf_relative = rootfile_elem.get('full-path')
    opf_path = os.path.join(extract_dir, opf_relative)
    opf_dir = os.path.dirname(opf_path)

    # Parse the OPF
    tree = ET.parse(opf_path)
    root = tree.getroot()

    # Handle OPF namespace
    opf_ns = re.match(r'\{.*\}', root.tag)
    ns_prefix = opf_ns.group(0) if opf_ns else ''

    # Build manifest: id -> {href, media_type, properties}
    manifest = {}
    for item in root.iter(f'{ns_prefix}item'):
        item_id = item.get('id')
        href = item.get('href')
        media_type = item.get('media-type', '')
        properties = item.get('properties', '')
        manifest[item_id] = {
            'href': href,
            'media_type': media_type,
            'properties': properties,
            'full_path': os.path.normpath(os.path.join(opf_dir, href))
        }

    # Build spine order
    spine = []
    for itemref in root.iter(f'{ns_prefix}itemref'):
        idref = itemref.get('idref')
        if idref in manifest:
            spine.append(manifest[idref])

    # Get metadata
    metadata = {}
    dc_ns = 'http://purl.org/dc/elements/1.1/'
    for elem in root.iter(f'{{{dc_ns}}}title'):
        metadata['title'] = elem.text
    for elem in root.iter(f'{{{dc_ns}}}creator'):
        metadata['creator'] = elem.text

    # Find cover image
    cover_image_path = None
    for item_id, item_data in manifest.items():
        if 'cover-image' in item_data.get('properties', ''):
            cover_image_path = item_data['full_path']
            break
    # Fallback: check for meta name="cover"
    if not cover_image_path:
        for meta in root.iter(f'{ns_prefix}meta'):
            if meta.get('name') == 'cover':
                cover_id = meta.get('content')
                if cover_id in manifest:
                    cover_image_path = manifest[cover_id]['full_path']
                    break

    return spine, manifest, metadata, opf_dir, cover_image_path


def create_cover_pdf(cover_image_path, output_path, page_width_in=5.5, page_height_in=8.5):
    """Create a full-bleed cover page PDF from the cover image.

    Scales the cover image to fill the entire page with no margins,
    similar to how Calibre handles cover pages.
    """
    img = Image.open(cover_image_path)

    # Convert to RGB if needed
    if img.mode in ('RGBA', 'P', 'LA'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        if img.mode in ('RGBA', 'LA'):
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert('RGB')

    # Page dimensions in pixels at 150 DPI (good balance of quality and size)
    dpi = 150
    page_w_px = int(page_width_in * dpi)
    page_h_px = int(page_height_in * dpi)

    # Scale image to fit the page while maintaining aspect ratio
    img_w, img_h = img.size
    img_aspect = img_w / img_h
    page_aspect = page_w_px / page_h_px

    if img_aspect > page_aspect:
        # Image is wider than page - fit to height, crop width
        new_h = page_h_px
        new_w = int(page_h_px * img_aspect)
    else:
        # Image is taller than page - fit to width, crop height
        new_w = page_w_px
        new_h = int(page_w_px / img_aspect)

    img_resized = img.resize((new_w, new_h), Image.LANCZOS)

    # Center crop to page size
    left = (new_w - page_w_px) // 2
    top = (new_h - page_h_px) // 2
    img_cropped = img_resized.crop((left, top, left + page_w_px, top + page_h_px))

    # Save as PDF
    img_cropped.save(output_path, 'PDF', resolution=dpi)

    return output_path


def is_cover_page(html_path, item):
    """Detect if a spine item is a cover page that wraps the cover image.

    Many EPUBs include a cover.xhtml that simply wraps the cover image in
    an HTML page. When we've already generated a full-bleed cover PDF,
    including this page results in a duplicate (poorly-formatted) cover.

    Detection heuristics:
    - The item ID or href contains 'cover'
    - The HTML body is very small and contains just an <img> tag
    - The body has a 'cover' class
    """
    href_lower = item.get('href', '').lower()
    # Check if filename/id suggests a cover page
    if 'cover' not in os.path.basename(href_lower):
        return False

    # Read and verify it's just wrapping an image
    try:
        with open(html_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL | re.IGNORECASE)
        if body_match:
            body = body_match.group(1).strip()
            # A cover page typically has minimal content: just a div with an img
            text_content = re.sub(r'<[^>]+>', '', body).strip()
            has_img = bool(re.search(r'<img\s', body, re.IGNORECASE))
            # If body text is very short (just whitespace/alt text) and has an image
            if has_img and len(text_content) < 100:
                return True
        # Also check for body class="cover"
        if re.search(r'<body[^>]*class="[^"]*cover[^"]*"', content, re.IGNORECASE):
            return True
    except Exception:
        pass

    return False


def build_combined_html(spine, opf_dir, skip_cover=False):
    """Combine all spine chapters into a single HTML document.

    This ensures that internal links (e.g., TOC links to chapter-001.xhtml)
    resolve correctly within the PDF, since they become anchor links
    within the same document.

    IMPORTANT: The output must be saved as .html (not .xhtml) to avoid
    Chromium's strict XHTML parser which silently fails on EPUB namespace
    attributes, resulting in an empty body and broken pagination.

    Args:
        spine: List of spine items from the OPF.
        opf_dir: Directory containing the OPF and content files.
        skip_cover: If True, detect and skip cover HTML pages from the spine
                    (to avoid duplicating the generated full-bleed cover).
    """
    # Collect all CSS files referenced by any chapter
    all_css = set()
    all_bodies = []
    skipped_cover = False

    for i, item in enumerate(spine):
        html_path = item['full_path']
        if not os.path.exists(html_path):
            continue

        # Skip cover HTML pages when we've generated our own cover PDF
        if skip_cover and not skipped_cover and is_cover_page(html_path, item):
            skipped_cover = True
            print(f"    Skipping cover page: {os.path.basename(item['href'])} (already generated full-bleed cover)")
            continue

        with open(html_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Extract CSS references from <link> tags
        for match in re.finditer(r'<link[^>]+href=["\']([^"\']+\.css)["\']', content, re.IGNORECASE):
            css_href = match.group(1)
            css_full = os.path.normpath(os.path.join(os.path.dirname(html_path), css_href))
            if os.path.exists(css_full):
                all_css.add(css_full)

        # Extract the <body> content
        body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL | re.IGNORECASE)
        if body_match:
            body_html = body_match.group(1)
        else:
            head_end = re.search(r'</head>', content, re.IGNORECASE)
            if head_end:
                body_html = content[head_end.end():]
                body_html = re.sub(r'</?html[^>]*>', '', body_html, flags=re.IGNORECASE)
            else:
                body_html = content

        # Create an anchor ID from the filename so TOC links resolve
        filename = os.path.basename(item['href'])
        anchor_id = filename.replace('.xhtml', '').replace('.html', '')

        # Rewrite internal links: href="chapter-001.xhtml" -> href="#chapter-001"
        body_html = re.sub(
            r'href="([^"#]+?\.xhtml)(?:#([^"]*?))?"',
            lambda m: f'href="#{m.group(1).replace(".xhtml", "")}"',
            body_html
        )

        # Strip XHTML namespace attributes that break HTML5 parsing
        body_html = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', body_html)
        body_html = re.sub(r'\s+epub:type="[^"]*"', '', body_html)

        # Use CSS class for page breaks (more reliable than inline styles)
        cls = 'epub-chapter-break' if i > 0 else 'epub-chapter-first'
        all_bodies.append(f'<div id="{anchor_id}" class="{cls}">\n{body_html}\n</div>\n')

    # Build the combined HTML document (as HTML5, NOT XHTML)
    combined_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    <title>Combined EPUB</title>
    {''.join(f'<link rel="stylesheet" type="text/css" href="{Path(css).as_uri()}"/>' for css in sorted(all_css))}
    <style type="text/css">
    /* Chapter page breaks */
    .epub-chapter-break {{
        page-break-before: always !important;
        break-before: page !important;
    }}
    /* PDF print optimization */
    @media print {{
        body {{
            orphans: 3;
            widows: 3;
        }}
        img {{
            max-width: 100% !important;
            height: auto !important;
            page-break-inside: avoid;
        }}
        p {{
            page-break-inside: avoid;
        }}
        h1, h2, h3, h4, h5, h6 {{
            page-break-after: avoid;
        }}
    }}
    </style>
</head>
<body>
{''.join(all_bodies)}
</body>
</html>"""

    # Save as .html (NOT .xhtml) - critical for Chromium compatibility
    combined_path = os.path.join(opf_dir, '_combined_epub.html')
    with open(combined_path, 'w', encoding='utf-8') as f:
        f.write(combined_html)

    return combined_path


def render_to_pdf(html_path, output_pdf_path):
    """Render a single HTML file to PDF using headless Chromium.

    Uses Chromium's print-to-PDF engine for pixel-perfect rendering
    of HTML, CSS, SVG, fonts, and images - the same engine Calibre uses
    (Qt WebEngine is also Chromium-based).
    """
    print("  Launching Chromium...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # Set viewport to match page dimensions at 96 DPI (Chromium default)
        # This prevents small-screen @media queries from activating
        page = browser.new_page(viewport={'width': 528, 'height': 816})

        file_url = Path(html_path).as_uri()
        print(f"  Loading combined HTML ({os.path.getsize(html_path) / 1024:.0f} KB)...")
        page.goto(file_url, wait_until='networkidle', timeout=120000)

        # Wait for fonts and images to load
        page.wait_for_timeout(2000)

        print("  Rendering PDF with Chromium print engine...")
        page.pdf(
            path=str(output_pdf_path),
            width='5.5in',
            height='8.5in',
            margin={
                'top': '0.75in',
                'bottom': '0.75in',
                'left': '0.65in',
                'right': '0.65in',
            },
            print_background=True,
            outline=True,
        )

        page.close()
        browser.close()

    return str(output_pdf_path)


def epub_to_pdf(epub_path, pdf_path=None):
    """Convert an EPUB file to PDF using Chromium rendering.

    Pipeline (mirrors Calibre's approach):
    1. Extract EPUB (it's just a ZIP)
    2. Parse OPF manifest and spine for reading order
    3. Extract cover image and create a full-bleed cover page
    4. Combine all chapters into a single HTML with internal anchor links
    5. Render the combined HTML to PDF via headless Chromium
    6. Prepend the cover page and add metadata
    """
    epub_path = Path(epub_path).resolve()
    if pdf_path is None:
        pdf_path = epub_path.with_suffix('.pdf')
    else:
        pdf_path = Path(pdf_path).resolve()

    print(f"EPUB to PDF Converter (Chromium-based)")
    print(f"{'='*50}")
    print(f"Input:  {epub_path}")
    print(f"Output: {pdf_path}")
    print()

    extract_dir = tempfile.mkdtemp(prefix="epub2pdf_extract_")
    try:
        # Step 1: Extract EPUB
        print("Step 1: Extracting EPUB...")
        extract_epub(str(epub_path), extract_dir)

        # Step 2: Parse OPF
        print("Step 2: Parsing OPF manifest...")
        spine, manifest, metadata, opf_dir, cover_image_path = parse_opf(extract_dir)
        print(f"  Title:    {metadata.get('title', 'Unknown')}")
        print(f"  Author:   {metadata.get('creator', 'Unknown')}")
        print(f"  Chapters: {len(spine)}")
        if cover_image_path:
            print(f"  Cover:    {os.path.basename(cover_image_path)}")
        print()

        # Step 3: Create cover page PDF
        cover_pdf_path = None
        if cover_image_path and os.path.exists(cover_image_path):
            print("Step 3: Creating cover page...")
            cover_pdf_path = os.path.join(extract_dir, '_cover.pdf')
            create_cover_pdf(cover_image_path, cover_pdf_path)
            print(f"  Cover page created")
        else:
            print("Step 3: No cover image found, skipping cover page")
        print()

        # Step 4: Combine all chapters into single HTML
        print("Step 4: Combining chapters into single HTML...")
        has_cover = cover_pdf_path is not None
        combined_html_path = build_combined_html(spine, opf_dir, skip_cover=has_cover)
        print(f"  Combined {len(spine)} chapters")
        print(f"  Internal TOC links rewritten to anchors")
        print()

        # Step 5: Render combined HTML to PDF via Chromium
        print("Step 5: Rendering with Chromium...")
        content_pdf_path = os.path.join(extract_dir, '_content.pdf')
        render_to_pdf(combined_html_path, content_pdf_path)
        print()

        # Step 6: Merge cover + content and add metadata
        print("Step 6: Merging final PDF...")
        writer = PdfWriter()

        # Add cover page first (if exists)
        if cover_pdf_path and os.path.exists(cover_pdf_path):
            writer.append(cover_pdf_path)
            print(f"  Added cover page")

        # Append content PDF - using append() preserves named destinations
        # and link annotations, which is critical for working TOC links.
        # (add_page() drops named destinations, breaking internal links)
        content_reader = PdfReader(content_pdf_path)
        writer.append(content_pdf_path)
        print(f"  Added {len(content_reader.pages)} content pages")

        # Add metadata
        if metadata:
            writer.add_metadata({
                '/Title': metadata.get('title', ''),
                '/Author': metadata.get('creator', ''),
                '/Producer': 'EPUB to PDF Converter (Chromium/Playwright)',
            })

        writer.write(str(pdf_path))
        writer.close()

        # Report
        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        total_pages = (1 if cover_pdf_path else 0) + len(content_reader.pages)
        print(f"\n{'='*50}")
        print(f"Done! PDF saved to: {pdf_path}")
        print(f"  Total pages: {total_pages}")
        print(f"  File size:   {size_mb:.2f} MB")

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    return str(pdf_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python epub_to_pdf.py <input.epub> [output.pdf]")
        print()
        print("Converts EPUB to PDF using headless Chromium (same approach as Calibre).")
        print("All processing is done locally - no data is sent online.")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    epub_to_pdf(input_file, output_file)
