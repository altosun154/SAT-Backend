import re
import io
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table
import supabase_storage
from parsing.ir import Block, Segment

_LETTERS = 'abcdefghijklmnopqrstuvwxyz'
_ROMAN_VALUES = [
    (1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'), (100, 'c'), (90, 'xc'),
    (50, 'l'), (40, 'xl'), (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i'),
]


def _iter_body_items(doc):
    for child in doc.element.body.iterchildren():
        if child.tag == qn('w:p'):
            yield Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield Table(child, doc)


def _load_numbering(doc):
    """Return (num_id -> abstract_num_id, abstract_num_id -> {ilvl: {fmt, start}}),
    or (None, None) if the document has no usable numbering definitions."""
    try:
        root = doc.part.numbering_part.element
    except Exception:
        return None, None
    if root is None:
        return None, None

    num_to_abstract = {}
    for num_el in root.findall(qn('w:num')):
        abstract_el = num_el.find(qn('w:abstractNumId'))
        if abstract_el is not None:
            num_to_abstract[num_el.get(qn('w:numId'))] = abstract_el.get(qn('w:val'))

    abstract_formats = {}
    for abs_el in root.findall(qn('w:abstractNum')):
        levels = {}
        for lvl_el in abs_el.findall(qn('w:lvl')):
            fmt_el = lvl_el.find(qn('w:numFmt'))
            start_el = lvl_el.find(qn('w:start'))
            levels[lvl_el.get(qn('w:ilvl'))] = {
                'fmt': fmt_el.get(qn('w:val')) if fmt_el is not None else 'decimal',
                'start': int(start_el.get(qn('w:val'))) if start_el is not None else 1,
            }
        abstract_formats[abs_el.get(qn('w:abstractNumId'))] = levels

    return num_to_abstract, abstract_formats


def _format_counter(n, fmt):
    if fmt == 'decimal':
        return str(n)
    if fmt == 'lowerLetter':
        return _LETTERS[(n - 1) % 26]
    if fmt == 'upperLetter':
        return _LETTERS[(n - 1) % 26].upper()
    if fmt in ('lowerRoman', 'upperRoman'):
        s, rem = '', n
        for value, sym in _ROMAN_VALUES:
            while rem >= value:
                s += sym
                rem -= value
        return s.upper() if fmt == 'upperRoman' else s
    return str(n)  # bullets / unrecognized formats — fall back to a plain counter


def _resolve_list_label(num_pr, num_to_abstract, abstract_formats, counters):
    """Render the auto-number Word would display for this paragraph (e.g. '1.'),
    given its <w:numPr>, advancing/restarting the running per-level counters."""
    if num_to_abstract is None:
        return ''

    num_id_el = num_pr.find(qn('w:numId'))
    ilvl_el = num_pr.find(qn('w:ilvl'))
    num_id = num_id_el.get(qn('w:val')) if num_id_el is not None else None
    ilvl = ilvl_el.get(qn('w:val')) if ilvl_el is not None else '0'
    if num_id is None:
        return ''

    abstract_id = num_to_abstract.get(num_id)
    level_info = abstract_formats.get(abstract_id, {}).get(ilvl, {'fmt': 'decimal', 'start': 1})

    key = (num_id, ilvl)
    counters[key] = counters.get(key, level_info['start'] - 1) + 1

    # A shallower level advancing resets any deeper levels under the same list.
    for other_key in [k for k in counters if k[0] == num_id and int(k[1]) > int(ilvl)]:
        del counters[other_key]

    label = _format_counter(counters[key], level_info['fmt'])
    return label if level_info['fmt'] == 'bullet' else f'{label}.'


def _extract_paragraph_image(item, doc):
    """If this paragraph embeds an inline image (w:drawing/a:blip), upload it
    and return the public URL, or None if there's no image or the upload
    fails/isn't configured."""
    blip = item._p.find('.//' + qn('a:blip'))
    if blip is None:
        return None
    r_id = blip.get(qn('r:embed'))
    if not r_id:
        return None
    try:
        image_part = doc.part.related_parts[r_id]
    except KeyError:
        return None
    ext = (image_part.content_type or '').split('/')[-1] or 'png'
    return supabase_storage.upload_image(image_part.blob, ext=ext, content_type=image_part.content_type)


def extract_blocks(source):
    if isinstance(source, bytes):
        doc = Document(io.BytesIO(source))
    else:
        doc = Document(source)

    num_to_abstract, abstract_formats = _load_numbering(doc)
    list_counters = {}

    blocks = []
    idx = 0

    for item in _iter_body_items(doc):
        if isinstance(item, Paragraph):
            text = re.sub(r'\s+', ' ', item.text).strip()

            ppr = item._p.find(qn('w:pPr'))
            num_pr = ppr.find(qn('w:numPr')) if ppr is not None else None
            has_math = item._p.find('.//' + qn('m:oMath')) is not None
            image_url = _extract_paragraph_image(item, doc)

            if not text and not has_math and not image_url:
                continue

            style_name = item.style.name if item.style else ''
            is_bold = any(r.bold for r in item.runs if r.bold is not None)
            font_size = None
            for run in item.runs:
                if run.font.size:
                    font_size = run.font.size.pt
                    break

            list_label = None
            if num_pr is not None:
                list_label = _resolve_list_label(num_pr, num_to_abstract, abstract_formats, list_counters)

            segments = [Segment(type='text', value=text)] if text else []
            if image_url:
                segments.append(Segment(type='image', ref=image_url))

            blocks.append(Block(
                index=idx,
                segments=segments,
                style={
                    'bold': is_bold,
                    'style_name': style_name,
                    'font_size': font_size,
                    'has_math': has_math,
                },
                list_label=list_label,
            ))
            idx += 1

        elif isinstance(item, Table):
            for row in item.rows:
                cells = []
                for cell in row.cells:
                    t = re.sub(r'\s+', ' ', cell.text).strip()
                    if not cells or cells[-1] != t:
                        cells.append(t)
                # Pipe-delimited rather than space-joined so column structure survives
                # (a client putting questions/choices in a table would otherwise have
                # them merge into one unreadable line).
                line = ' | '.join(c for c in cells if c)
                if line:
                    blocks.append(Block(
                        index=idx,
                        segments=[Segment(type='text', value=line)],
                        style={'in_table': True, 'is_table_row': True},
                    ))
                    idx += 1

    return blocks
