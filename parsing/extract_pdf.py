import re
from collections import Counter
import pymupdf
import supabase_storage
from parsing.ir import Block, Segment

BOILERPLATE_RE = re.compile(
    r'^(Triumph Training Center|Page \d+( of \d+)?)$',
    re.IGNORECASE,
)

# Below this average characters-per-page, treat the file as having no real text
# layer (scanned/flattened image) rather than silently returning "0 questions".
MIN_CHARS_PER_PAGE = 20

# Header/footer band: lines whose bbox falls in the top/bottom 10% of the page.
EDGE_BAND_FRACTION = 0.10

# Horizontal gap between spans on the same row, above which we treat the gap
# as a column boundary (a table cell edge) rather than normal word spacing.
# PDFs don't carry real table structure, so this is a heuristic, not a parser —
# it only recognizes tables laid out as widely-spaced columns on one row.
TABLE_GAP_PT = 18


def extract_blocks(source):
    """Extract text/image blocks (one per visual line, or per embedded image)
    from a PDF, in reading order, with page/bbox position preserved and
    repeated header/footer lines removed.

    Uses PyMuPDF's dict text extraction, which already groups words into lines
    with position and font metadata — no manual word-clustering needed.
    """
    if isinstance(source, bytes):
        doc = pymupdf.open(stream=source, filetype='pdf')
    else:
        doc = pymupdf.open(source)

    try:
        n_pages = doc.page_count
        if n_pages == 0:
            return []

        # items: list[page] of list[(text_or_None, bbox, is_table_row, image_url_or_None)]
        page_items = []
        page_heights = []
        total_chars = 0

        for page in doc:
            page_heights.append(page.rect.height)
            items = []

            # Flatten to individual spans first rather than trusting PyMuPDF's own
            # line-grouping — widely-spaced same-row text (table columns) is often
            # split into separate 'line' entries, so rows are re-clustered below.
            spans = []
            page_dict = page.get_text('dict')
            for block in page_dict.get('blocks', []):
                if block.get('type') != 0:  # skip image blocks here; handled below via get_images
                    continue
                for line in block.get('lines', []):
                    for s in line.get('spans', []):
                        if s['text'].strip():
                            spans.append(s)
            spans.sort(key=lambda s: (round(s['bbox'][1]), s['bbox'][0]))

            # Cluster by vertical overlap/center proximity, not a fixed top-to-top
            # distance — a bold header cell and a plain-text data cell in the same
            # row commonly sit at different baselines (different font metrics/cell
            # padding), so comparing raw 'top' values is too brittle. Each cluster
            # tracks its own running (y0, y1) range so it keeps absorbing spans that
            # belong to it, not just spans close to the first one added.
            rows = []  # each entry: {'y0': .., 'y1': .., 'spans': [...]}
            for s in spans:
                y0, y1 = s['bbox'][1], s['bbox'][3]
                center = (y0 + y1) / 2
                if rows:
                    r = rows[-1]
                    overlaps = min(r['y1'], y1) > max(r['y0'], y0)
                    r_center = (r['y0'] + r['y1']) / 2
                    close_centers = abs(center - r_center) <= 0.6 * max(y1 - y0, r['y1'] - r['y0'])
                    if overlaps or close_centers:
                        r['spans'].append(s)
                        r['y0'] = min(r['y0'], y0)
                        r['y1'] = max(r['y1'], y1)
                        continue
                rows.append({'y0': y0, 'y1': y1, 'spans': [s]})
            rows = [r['spans'] for r in rows]

            for row in rows:
                row.sort(key=lambda s: s['bbox'][0])
                parts, is_table_row = [], False
                prev_x1 = None
                for s in row:
                    text = s['text']
                    x0 = s['bbox'][0]
                    if prev_x1 is not None:
                        gap = x0 - prev_x1
                        if gap > TABLE_GAP_PT:
                            parts.append(' | ')
                            is_table_row = True
                        elif gap > 1:
                            parts.append(' ')
                    parts.append(text)
                    prev_x1 = s['bbox'][2]
                text = ''.join(parts).strip(' |').strip()
                if not text:
                    continue
                x0 = min(s['bbox'][0] for s in row)
                y0 = min(s['bbox'][1] for s in row)
                x1 = max(s['bbox'][2] for s in row)
                y1 = max(s['bbox'][3] for s in row)
                total_chars += len(text)
                items.append((text, (x0, y0, x1, y1), is_table_row, None))

            for img in page.get_images(full=True):
                xref = img[0]
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                rect = rects[0]
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                ext = info.get('ext', 'png')
                url = supabase_storage.upload_image(
                    info['image'], ext=ext, content_type=f'image/{ext}',
                )
                if not url:
                    continue
                items.append((None, (rect.x0, rect.y0, rect.x1, rect.y1), False, url))

            # Reading order: top-to-bottom, then left-to-right. Defensive — PyMuPDF's
            # block/line order is usually already this, but not guaranteed for exotic
            # layouts, and images need interleaving with the text lines around them.
            items.sort(key=lambda t: (round(t[1][1]), t[1][0]))
            page_items.append(items)

        if total_chars / n_pages < MIN_CHARS_PER_PAGE:
            raise ValueError(
                'This PDF has little or no extractable text — it looks like a scanned '
                'or image-only file with no text layer. Re-export it from its source '
                'document, or run OCR on it first.'
            )

        # Boilerplate detection by position, not content: only lines sitting in the
        # header/footer band can be header/footer text, no matter how often a body
        # line happens to repeat (e.g. a stock phrase reused across every question).
        edge_counts = Counter()
        for height, items in zip(page_heights, page_items):
            head, foot = height * EDGE_BAND_FRACTION, height * (1 - EDGE_BAND_FRACTION)
            for text, (_, y0, _, y1), _, url in items:
                if text is not None and (y0 < head or y1 > foot):
                    edge_counts[text] += 1
        boilerplate = {
            text for text, count in edge_counts.items()
            if count >= max(2, n_pages * 0.5)
        }

        blocks = []
        idx = 0
        for page_num, items in enumerate(page_items, 1):
            for text, bbox, is_table_row, url in items:
                if url is not None:
                    blocks.append(Block(
                        index=idx,
                        segments=[Segment(type='image', ref=url)],
                        style={},
                        page=page_num,
                        bbox=bbox,
                    ))
                    idx += 1
                    continue
                if text in boilerplate:
                    continue
                if BOILERPLATE_RE.match(text):
                    continue
                blocks.append(Block(
                    index=idx,
                    segments=[Segment(type='text', value=text)],
                    style={'is_table_row': is_table_row},
                    page=page_num,
                    bbox=bbox,
                ))
                idx += 1

        return blocks
    finally:
        doc.close()
