import io
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Callable, Optional

import pymupdf
from PIL import Image
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

import supabase_storage

# Word autocorrects " - " to an en dash, so headings may use em dash, en dash or hyphen.
DASH = r"[—–-]"
SECTION_RE = re.compile(rf"^(Reading and Writing|Math)\s*{DASH}\s*Module\s*([12])\s*$", re.IGNORECASE)
# Difficulty tags appear either bracketed ("[EASY]") or as a bare word inside a
# badge/pill shape with no brackets in the underlying text layer ("EASY") — both
# templates are in use, so brackets are optional here. Some templates also tack an
# annotation like "(Student-produced response)" onto the question line itself,
# ahead of the difficulty tag — that's optional too and its contents are ignored
# (grid-in questions are still detected via GRID_IN_RE / the no-choices fallback).
QUESTION_RE = re.compile(
    r"^Question\s+(\d+)\b(?:\s*\([^)]*\))?\s*(?:\[?(EASY|MEDIUM|HARD)\]?)?\s*$",
    re.IGNORECASE,
)
CHOICE_RE = re.compile(r"^([A-D])\)\s*(.*)$")
GRID_IN_RE = re.compile(r"student-produced response", re.IGNORECASE)
# "TEST" occasionally gets split by letter-spacing in styled banner text (e.g.
# "PRACTICE T EST -3"), so allow arbitrary whitespace inside the word.
TITLE_RE = re.compile(r"DIGITAL\s+SAT\s+PRACTICE\s+T\s*E\s*S\s*T\s*-?\s*(\d+)", re.IGNORECASE)
# Matched as a prefix (not the whole line) since some templates title this section
# "Answer Key & Explanations" / "Answer Key with Explanations" rather than the bare
# "Answer Keys" Test 1/2 use.
ANSWER_KEYS_RE = re.compile(r"^Answer\s+Keys?\b", re.IGNORECASE)
# Some templates append a bubble-sheet appendix after the real answer key/explanations;
# per explicit instruction, that section is never used as an answer source — stop there.
BUBBLE_SUMMARY_RE = re.compile(r"Bubble\s+Summary", re.IGNORECASE)
RW_ANSWER_HEADER_RE = re.compile(rf"Reading and Writing\s*{DASH}\s*Answer Key", re.IGNORECASE)
MATH_ANSWER_HEADER_RE = re.compile(rf"Math\s*{DASH}\s*Answer Key", re.IGNORECASE)
MODULE_HEADING_RE = re.compile(r"^Module\s*([12])\s*$", re.IGNORECASE)
# Answer-key entry line, used for both subjects: "N. B — explanation", "N. B.
# explanation" (multiple choice), or "N. 4. explanation" / "N. -2. explanation"
# (grid-in, where some templates number their explanations the same way as MCQs).
ANSWER_ENTRY_RE = re.compile(r"^(\d+)\.\s*([-−]?\d+(?:\.\d+)?|[A-Za-z])\.?\s*(.*)$")
# Legacy 2-column table row used by some templates' Math answer key: "N ans N ans".
MATH_ANSWER_ROW_RE = re.compile(r"^(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s*$")
BOILERPLATE_RE = re.compile(r"^(Triumph Training Center)$", re.IGNORECASE)
DIFFICULTY_TAG_RE = re.compile(r"^\[?(EASY|MEDIUM|HARD)\]?$", re.IGNORECASE)
# A fill-in blank on a line of its own ("________") — belongs to whatever text it sits in.
BLANK_LINE_RE = re.compile(r"^_{3,}$")

# Vertical distance between the tops of two consecutive lines above which they're
# separate paragraphs rather than one wrapped paragraph (known templates: ~14.5pt
# wrap vs ~19pt paragraph spacing).
PARAGRAPH_GAP = 17
# Figures/tables are rasterised at this resolution, with a little breathing room.
RENDER_DPI = 150
VISUAL_PADDING = 4
# How far above a table its caption/title line can sit, in pt.
TABLE_CAPTION_GAP = 12
VISUAL_STACK_GAP = 24  # px between visuals stacked into one question image
UPLOAD_WORKERS = 8  # concurrent image uploads to storage

# Filename → test name cleanup: strips OS duplicate-file prefixes, formats
# camelCase/number-smashed words for readability, and drops a small set of
# filler words that show up as pure noise across known templates.
_COPY_PREFIX_RE = re.compile(r"^copy(?:\s*\(\d+\))?\s+of\s+", re.IGNORECASE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_LETTER_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_NAME_NOISE_WORDS = {"annotated", "difficulty"}

SUBJECT_MAP = {
    ("Reading and Writing", "1"): "Section 1, Module 1: Reading and Writing",
    ("Reading and Writing", "2"): "Section 1, Module 2: Reading and Writing",
    ("Math", "1"): "Section 2, Module 1: Math",
    ("Math", "2"): "Section 2, Module 2: Math",
}


@dataclass
class Line:
    """A line of text (PDF) or a paragraph (DOCX), with the layout facts needed to
    tell a passage (italic in known templates) from the question stem."""
    text: str
    italic: bool = False
    page: int = 0
    top: Optional[float] = None  # None for DOCX, where every item is its own paragraph
    in_table: bool = False       # PDF table text — the table is imported as a Visual instead


@dataclass
class Visual:
    """A figure or table shown with a question. Rendered lazily so that visuals
    outside any question (e.g. answer-key tables) are never rasterised."""
    render: Callable[[], Optional[bytes]]  # PNG bytes, or None if it can't be converted


def _is_italic_font(fontname):
    name = (fontname or "").lower()
    return "italic" in name or "oblique" in name


def _rect_key(rect):
    return tuple(round(v) for v in rect)


def _pdf_visual_rects(doc):
    """Per page, the rects of figures and tables to show with questions, plus the
    pixel sizes of page-decoration images (logo, watermark): images drawn at the
    same spot on most pages. Positions only — asking PyMuPDF for image xrefs here
    would decode every image on every page (~180 MB on a 45-page test)."""
    page_images = [page.get_image_info() for page in doc]
    counts = Counter(key for infos in page_images for key in {_rect_key(i["bbox"]) for i in infos})
    decoration = {key for key, n in counts.items() if n >= max(2, len(doc) * 0.5)}
    decoration_sizes = {
        (i["width"], i["height"])
        for infos in page_images for i in infos if _rect_key(i["bbox"]) in decoration
    }

    result = []
    for page, infos in zip(doc, page_images):
        figures = [pymupdf.Rect(i["bbox"]) for i in infos if _rect_key(i["bbox"]) not in decoration]
        tables = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
        result.append((figures, tables))
    return result, decoration_sizes


def _pdf_renderer(page, rect, decoration_sizes):
    """Render `rect` of `page` to PNG. Decoration images on the page are removed
    from the in-memory document first so they don't show through the figure or
    table (the file on disk is never written)."""
    clip = pymupdf.Rect(rect.x0 - VISUAL_PADDING, rect.y0 - VISUAL_PADDING,
                        rect.x1 + VISUAL_PADDING, rect.y1 + VISUAL_PADDING) & page.rect

    def render():
        for img in page.get_images(full=True):
            xref, width, height = img[0], img[2], img[3]
            if (width, height) in decoration_sizes:
                page.delete_image(xref)
        return page.get_pixmap(clip=clip, dpi=RENDER_DPI).tobytes("png")
    return render


def _is_table_caption(line, table):
    """A plain line sitting just above a table (its title) belongs with the table."""
    return (
        not line["italic"]
        and table.y0 - TABLE_CAPTION_GAP <= line["bottom"] <= table.y0 + 1
        and line["x0"] < table.x1 and line["x1"] > table.x0
        and not QUESTION_RE.match(line["text"]) and not CHOICE_RE.match(line["text"])
    )


def _pdf_words(page, x_tolerance=3):
    """Words on `page` with their bbox and font, in the shape pdfplumber's
    extract_words(extra_attrs=["fontname"]) returns. Built from PyMuPDF's
    per-character output (C code) rather than pdfplumber, whose pure-Python
    parsing took ~2/3 of the import time on a 45-page test — long enough to hit
    the server's request timeout. A word breaks at whitespace or a horizontal gap
    wider than `x_tolerance`, and can span font changes (e.g. bold "A)" + text)."""
    words = []
    # Text only: rawdict otherwise carries every image on the page, decoded.
    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        for line in block.get("lines", []):
            current = None
            for span in line["spans"]:
                for ch in span["chars"]:
                    c = ch["c"]
                    x0, top, x1, bottom = ch["bbox"]
                    if c.isspace():
                        current = None
                        continue
                    if current is None or x0 - current["x1"] > x_tolerance:
                        current = {"text": "", "x0": x0, "x1": x1, "top": top,
                                   "bottom": bottom, "fontname": span["font"]}
                        words.append(current)
                    current["text"] += c
                    current["x1"] = max(current["x1"], x1)
                    current["bottom"] = max(current["bottom"], bottom)
    return words


def extract_items_from_pdf(doc, y_tolerance=3):
    """Return the document as Lines and Visuals in reading order. Text lines are
    reconstructed per page by clustering words with similar vertical position,
    since raw PDF content-stream order can interleave absolutely-positioned labels
    (like difficulty tags) with body text. `doc` is the file opened with PyMuPDF;
    it must stay open until the figures/tables are rendered."""
    visual_rects, decoration_sizes = _pdf_visual_rects(doc)
    items = []
    for page_num, (page, (figures, tables)) in enumerate(zip(doc, visual_rects)):
        words = _pdf_words(page)
        clusters = []
        for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
            for c in clusters:
                if abs(c["top"] - w["top"]) <= y_tolerance:
                    c["words"].append(w)
                    break
            else:
                clusters.append({"top": w["top"], "words": [w]})

        lines = []
        for c in clusters:
            ws = sorted(c["words"], key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in ws).strip()
            if not text or BOILERPLATE_RE.match(text):
                continue
            total_chars = sum(len(w["text"]) for w in ws)
            italic_chars = sum(len(w["text"]) for w in ws if _is_italic_font(w["fontname"]))
            lines.append({
                "text": text, "top": c["top"], "bottom": max(w["bottom"] for w in ws),
                "x0": ws[0]["x0"], "x1": ws[-1]["x1"], "words": ws,
                "italic": italic_chars * 2 > total_chars,
            })

        for i, table in enumerate(tables):
            for line in lines:
                if _is_table_caption(line, table):
                    tables[i] = table = table | pymupdf.Rect(line["x0"], line["top"], line["x1"], line["bottom"])

        positioned = [(r.y0, Visual(_pdf_renderer(doc[page_num], r, decoration_sizes))) for r in figures + tables]
        for line in lines:
            ws = line["words"]
            in_table = sum(
                1 for w in ws
                if any(t.contains(pymupdf.Point((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2))
                       for t in tables)
            ) * 2 > len(ws)
            positioned.append((line["top"], Line(
                line["text"], italic=line["italic"],
                page=page_num, top=line["top"], in_table=in_table,
            )))

        positioned.sort(key=lambda p: p[0])
        items.extend(item for _, item in positioned)

    if not any(isinstance(i, Line) for i in items):
        raise ValueError(
            "Could not extract any text from this PDF — it may be a scanned or "
            "flattened/image-only file with no selectable text layer. Try opening it "
            "in a PDF viewer and checking whether you can select/copy the question text; "
            "if not, re-export or re-generate the PDF from its original source document."
        )
    return items


def _iter_docx_block_items(doc):
    """Yield paragraphs and tables in document order (python-docx exposes them as
    separate collections by default, losing interleaving order)."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _to_png(blob):
    """Convert an embedded image to PNG; None for formats Pillow can't read (EMF/WMF)."""
    try:
        buf = io.BytesIO()
        Image.open(io.BytesIO(blob)).save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _docx_paragraph_visuals(paragraph, doc):
    visuals = []
    for blip in paragraph._p.iter(qn("a:blip")):
        part = doc.part.related_parts.get(blip.get(qn("r:embed")))
        if part is not None:
            visuals.append(Visual(lambda blob=part.blob: _to_png(blob)))
    return visuals


def extract_items_from_docx(docx_path):
    """Word paragraphs/tables are already stored in document order, so no
    position-based reconstruction is needed like with the PDF."""
    doc = Document(docx_path)
    items = []
    for block in _iter_docx_block_items(doc):
        if isinstance(block, Paragraph):
            items.extend(_docx_paragraph_visuals(block, doc))
            text = re.sub(r"\s+", " ", block.text).strip()
            if text and not BOILERPLATE_RE.match(text):
                runs = [r for r in block.runs if r.text.strip()]
                italic_chars = sum(len(r.text) for r in runs if r.italic)
                items.append(Line(text, italic=italic_chars * 2 > sum(len(r.text) for r in runs)))
        else:
            for row in block.rows:
                cells = []
                for cell in row.cells:
                    text = re.sub(r"\s+", " ", cell.text).strip()
                    # merged cells repeat identical text across spanned cells
                    if not cells or cells[-1] != text:
                        cells.append(text)
                line = " ".join(c for c in cells if c)
                if line:
                    items.append(Line(line))
    return items


def _paragraphs(lines):
    """Group lines into (italic, text) paragraphs. A new paragraph starts on a
    style change or a larger-than-wrap vertical gap; lines continuing onto the
    next page stay in the same paragraph."""
    paragraphs = []
    prev = None
    for line in lines:
        starts_new = (
            prev is None or line.top is None or prev.top is None
            or line.italic != prev.italic
            or (line.page == prev.page and line.top - prev.top > PARAGRAPH_GAP)
        )
        if starts_new:
            paragraphs.append([line.italic, line.text])
        else:
            joiner = "" if paragraphs[-1][1].endswith(("-", "—")) else " "
            paragraphs[-1][1] += joiner + line.text
        prev = line
    return paragraphs


def _finalize_question(q, section, number):
    subject = SUBJECT_MAP[(section["subject_name"], section["module"])]
    is_grid_in = q["grid_in"] or not q["choices"]
    paragraphs = _paragraphs(q["body"])

    # Reading and Writing: the italic text is the passage the question refers to,
    # shown in its own panel; the rest is the question stem. Math has no passage
    # panel, so its given equations/data stay in the text, one per line.
    passage = None
    stem = [text for _, text in paragraphs]
    if section["subject_name"] == "Reading and Writing":
        passage_paras = [text for italic, text in paragraphs if italic]
        stem_paras = [text for italic, text in paragraphs if not italic]
        if passage_paras and stem_paras:
            passage, stem = "\n".join(passage_paras), stem_paras

    return {
        "question_number": number,
        "section_key": (section["subject_name"], section["module"]),
        "text": "\n".join(stem).strip(),
        "choice_a": "" if is_grid_in else q["choices"].get("A", ""),
        "choice_b": "" if is_grid_in else q["choices"].get("B", ""),
        "choice_c": "" if is_grid_in else q["choices"].get("C", ""),
        "choice_d": "" if is_grid_in else q["choices"].get("D", ""),
        "subject": subject,
        "difficulty": q["difficulty"].lower() if q["difficulty"] else None,
        "passage": passage,
        "image_url": None,  # uploaded from "visuals" once the answer key checks out
        "visuals": q["visuals"],
        "skill": None,
        "module_variant": None,
        "correct_answer": None,  # filled in from the answer key later
        "explanation": None,
    }


def _parse_questions(items):
    questions = []
    section = None
    current_q = None
    current_number = None

    def flush():
        if current_q is not None:
            questions.append(_finalize_question(current_q, section, current_number))

    for i, item in enumerate(items):
        if isinstance(item, Visual):
            if current_q is not None:
                current_q["visuals"].append(item)
            continue

        line = item.text
        if ANSWER_KEYS_RE.match(line):
            flush()
            return questions, i

        # Table text is imported as part of the table's image; the title banner
        # repeats at the top of every page.
        if item.in_table or TITLE_RE.search(line):
            continue

        m = SECTION_RE.match(line)
        if m:
            flush()
            current_q = None
            section = {"subject_name": m.group(1) if m.group(1) == "Math" else "Reading and Writing", "module": m.group(2)}
            continue

        m = QUESTION_RE.match(line)
        if m:
            if section is None:
                raise ValueError(
                    f"Found \"{line}\" before any section heading. Each section must start "
                    "with a heading like \"Reading and Writing — Module 1\" or \"Math — Module 2\"."
                )
            flush()
            current_number = int(m.group(1))
            current_q = {"body": [], "choices": {}, "last_choice": None, "visuals": [],
                         "grid_in": False, "difficulty": m.group(2)}
            continue

        if current_q is None:
            continue

        m = CHOICE_RE.match(line)
        if m:
            current_q["choices"][m.group(1)] = m.group(2).strip()
            current_q["last_choice"] = m.group(1)
        elif GRID_IN_RE.search(line):
            current_q["grid_in"] = True
        elif DIFFICULTY_TAG_RE.match(line):
            current_q["difficulty"] = DIFFICULTY_TAG_RE.match(line).group(1)
        elif current_q["last_choice"]:
            # a long choice wrapping onto the next line
            letter = current_q["last_choice"]
            current_q["choices"][letter] = f"{current_q['choices'][letter]} {line}".strip()
        else:
            if BLANK_LINE_RE.match(line) and current_q["body"]:
                item = replace(item, italic=current_q["body"][-1].italic)
            current_q["body"].append(item)

    flush()
    return questions, len(items)


def _parse_answer_keys(items):
    """Single pass over the answer-key section, tracking (subject, module) state
    as it goes. Two known template shapes are supported:
      - bare "Module 1"/"Module 2" headings nested under a "Reading and Writing —
        Answer Key ..." / "Math — Answer Key" wrapper heading, with the Math
        answers given as a 2-column table ("N ans N ans")
      - full "Reading and Writing — Module N" / "Math — Module N" headings used
        directly (no separate wrapper heading), with both subjects' answers given
        as numbered entries ("N. B — explanation" / "N. 4. explanation")
    Lines following a numbered entry, up to the next entry or heading, are that
    question's explanation. Stops entirely at a "Bubble Summary" appendix, if
    present — that section is never used as an answer source.
    Returns (answers, explanations), both keyed by (subject, module) then number.
    """
    keys = [("Reading and Writing", "1"), ("Reading and Writing", "2"), ("Math", "1"), ("Math", "2")]
    answers = {key: {} for key in keys}
    explanations = {key: {} for key in keys}
    current_subject = None
    current_module = None
    explaining = None  # (key, number) whose explanation lines are being collected

    for item in items:
        if isinstance(item, Visual):
            continue
        line = item.text
        if BUBBLE_SUMMARY_RE.search(line):
            break
        if TITLE_RE.search(line):
            continue

        m = SECTION_RE.match(line)
        if m:
            current_subject = "Math" if m.group(1) == "Math" else "Reading and Writing"
            current_module = m.group(2)
            explaining = None
            continue

        if RW_ANSWER_HEADER_RE.search(line):
            current_subject = "Reading and Writing"
            current_module = None
            explaining = None
            continue
        if MATH_ANSWER_HEADER_RE.search(line):
            current_subject = "Math"
            current_module = None
            explaining = None
            continue

        m = MODULE_HEADING_RE.match(line)
        if m:
            current_module = m.group(1)
            explaining = None
            continue

        if current_subject is None or current_module is None:
            continue
        key = (current_subject, current_module)

        m = ANSWER_ENTRY_RE.match(line)
        if m:
            num = int(m.group(1))
            value = m.group(2).replace("−", "-")
            answers[key][num] = value.upper() if value.isalpha() else value
            explaining = (key, num)
            # "N. B — explanation" and grid-in "N. 4. explanation" put the
            # explanation inline; "N. B. Ancient" just echoes the choice text.
            rest = m.group(3).strip()
            if rest[:1] in ("—", "–", "-") or not value.isalpha():
                rest = rest.lstrip("—–- ").strip()
                if rest:
                    explanations[key][num] = rest
            continue

        m = MATH_ANSWER_ROW_RE.match(line)
        if m and current_subject == "Math":
            answers[key][int(m.group(1))] = m.group(2)
            answers[key][int(m.group(3))] = m.group(4)
            explaining = None
            continue

        if explaining:
            ekey, num = explaining
            existing = explanations[ekey].get(num)
            explanations[ekey][num] = f"{existing}\n{line}" if existing else line

    return answers, explanations


def _clean_test_name_from_filename(filename):
    base = os.path.splitext(filename)[0]
    base = base.rstrip(". ")
    base = _COPY_PREFIX_RE.sub("", base).strip()
    base = base.replace("_", " ").replace("-", " ")
    base = _CAMEL_BOUNDARY_RE.sub(" ", base)
    base = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", base)
    words = [w for w in base.split() if w.lower() not in _NAME_NOISE_WORDS]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _combine_pngs(pngs):
    """Stack several PNGs vertically, centred, into one image."""
    if len(pngs) == 1:
        return pngs[0]
    images = [Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]
    width = max(img.width for img in images)
    height = sum(img.height for img in images) + VISUAL_STACK_GAP * (len(images) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for img in images:
        canvas.paste(img, ((width - img.width) // 2, y))
        y += img.height + VISUAL_STACK_GAP
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _render_visuals(visuals):
    """Render a question's figures/tables into one PNG, or None if there's nothing to show."""
    pngs = [png for png in (v.render() for v in visuals) if png]
    return _combine_pngs(pngs) if pngs else None


def _upload_question_images(questions, upload_image):
    """Render every question's visuals (sequentially — PyMuPDF isn't thread-safe),
    then upload them in parallel: one at a time, ~20 uploads were a large share of
    the request time. A failed upload leaves image_url as None."""
    pending = []
    for q in questions:
        visuals = q.pop("visuals")
        png = _render_visuals(visuals) if visuals else None
        if png:
            pending.append((q, png))
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        urls = pool.map(lambda item: upload_image(item[1], ext="png", content_type="image/png"), pending)
        for (q, _), url in zip(pending, urls):
            q["image_url"] = url


def _parse_lines(items, original_filename=None, upload_image=None):
    upload_image = upload_image or supabase_storage.upload_image
    test_name = _clean_test_name_from_filename(original_filename) if original_filename else ""
    if not test_name:
        texts = [i.text for i in items if isinstance(i, Line)]
        title_match = next((TITLE_RE.search(t) for t in texts if TITLE_RE.search(t)), None)
        test_name = f"SAT Practice Test {title_match.group(1)}" if title_match else "SAT Practice Test"

    questions, answer_key_start = _parse_questions(items)
    if not questions:
        raise ValueError(
            "Text was extracted from this file, but no \"Question N\" headings matched "
            "the expected template. The file's section headers, question numbering, or "
            "layout may not match the practice-test template this importer expects."
        )
    answer_keys, explanations = _parse_answer_keys(items[answer_key_start:])

    missing_answers = []
    for q in questions:
        key = q.pop("section_key")
        num = q["question_number"]
        answer = answer_keys.get(key, {}).get(num)
        if answer is None:
            missing_answers.append((key, num))
        q["correct_answer"] = answer or ""
        q["explanation"] = explanations.get(key, {}).get(num)
        del q["question_number"]

    if missing_answers:
        details = ", ".join(f"{k[0]} Module {k[1]} Q{n}" for k, n in missing_answers)
        raise ValueError(f"Could not find answer-key entries for: {details}")

    _upload_question_images(questions, upload_image)

    return {"test_name": test_name, "questions": questions}


def parse_pdf(pdf_path, original_filename=None, upload_image=None):
    with pymupdf.open(pdf_path) as doc:
        items = extract_items_from_pdf(doc)
        return _parse_lines(items, original_filename or os.path.basename(pdf_path), upload_image)


def parse_docx(docx_path, original_filename=None, upload_image=None):
    return _parse_lines(extract_items_from_docx(docx_path), original_filename or os.path.basename(docx_path), upload_image)


def parse_test_file(path, original_filename=None, upload_image=None):
    """Dispatch to the right parser based on file extension. `original_filename`
    is used to derive the test name — pass the user-facing upload filename when
    `path` is a temp file (e.g. the admin upload endpoint), since a random temp
    filename wouldn't produce a meaningful test name. `upload_image(png_bytes,
    ext=, content_type=)` stores a question's figure and returns its URL; it
    defaults to the Supabase question-images bucket."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return parse_pdf(path, original_filename, upload_image)
    if ext == ".docx":
        return parse_docx(path, original_filename, upload_image)
    raise ValueError(f"Unsupported test file type: {ext or '(none)'}. Expected .pdf or .docx.")


if __name__ == "__main__":
    import sys
    import json

    result = parse_test_file(sys.argv[1])
    print(json.dumps(result, indent=2))
    print(f"\n{len(result['questions'])} questions parsed.", file=sys.stderr)
