import os
import re
import pdfplumber
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

SECTION_RE = re.compile(r"^(Reading and Writing|Math)\s*[—-]\s*Module\s*([12])\s*$", re.IGNORECASE)
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
RW_ANSWER_HEADER_RE = re.compile(r"Reading and Writing\s*[—-]\s*Answer Key", re.IGNORECASE)
MATH_ANSWER_HEADER_RE = re.compile(r"Math\s*[—-]\s*Answer Key", re.IGNORECASE)
MODULE_HEADING_RE = re.compile(r"^Module\s*([12])\s*$", re.IGNORECASE)
# Answer-key entry line, used for both subjects: "N. B — explanation", "N. B.
# explanation" (multiple choice), or "N. 4. explanation" / "N. -2. explanation"
# (grid-in, where some templates number their explanations the same way as MCQs).
ANSWER_ENTRY_RE = re.compile(r"^(\d+)\.\s*([-−]?\d+(?:\.\d+)?|[A-Za-z])\.?\s*(.*)$")
# Legacy 2-column table row used by some templates' Math answer key: "N ans N ans".
MATH_ANSWER_ROW_RE = re.compile(r"^(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s*$")
BOILERPLATE_RE = re.compile(r"^(Triumph Training Center)$", re.IGNORECASE)
DIFFICULTY_TAG_RE = re.compile(r"^\[?(EASY|MEDIUM|HARD)\]?$", re.IGNORECASE)

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


def extract_lines_from_pdf(pdf_path, y_tolerance=3):
    """Reconstruct visual reading order per page by clustering words with similar
    vertical position into lines, since raw PDF content-stream order can interleave
    absolutely-positioned labels (like difficulty tags) with body text."""
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue
            clusters = []
            for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
                for c in clusters:
                    if abs(c["top"] - w["top"]) <= y_tolerance:
                        c["words"].append(w)
                        break
                else:
                    clusters.append({"top": w["top"], "words": [w]})
            clusters.sort(key=lambda c: c["top"])
            for c in clusters:
                ws = sorted(c["words"], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in ws).strip()
                if text and not BOILERPLATE_RE.match(text):
                    lines.append(text)
    if not lines:
        raise ValueError(
            "Could not extract any text from this PDF — it may be a scanned or "
            "flattened/image-only file with no selectable text layer. Try opening it "
            "in a PDF viewer and checking whether you can select/copy the question text; "
            "if not, re-export or re-generate the PDF from its original source document."
        )
    return lines


def _iter_docx_block_items(doc):
    """Yield paragraphs and tables in document order (python-docx exposes them as
    separate collections by default, losing interleaving order)."""
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def extract_lines_from_docx(docx_path):
    """Word paragraphs/tables are already stored in document order, so no
    position-based reconstruction is needed like with the PDF."""
    doc = Document(docx_path)
    lines = []
    for block in _iter_docx_block_items(doc):
        if isinstance(block, Paragraph):
            text = re.sub(r"\s+", " ", block.text).strip()
            if text and not BOILERPLATE_RE.match(text):
                lines.append(text)
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
                    lines.append(line)
    return lines


def _finalize_question(q, section, number):
    subject = SUBJECT_MAP[(section["subject_name"], section["module"])]
    is_grid_in = q["grid_in"] or not q["choices"]
    return {
        "question_number": number,
        "section_key": (section["subject_name"], section["module"]),
        "text": " ".join(q["body_lines"]).strip(),
        "choice_a": "" if is_grid_in else q["choices"].get("A", ""),
        "choice_b": "" if is_grid_in else q["choices"].get("B", ""),
        "choice_c": "" if is_grid_in else q["choices"].get("C", ""),
        "choice_d": "" if is_grid_in else q["choices"].get("D", ""),
        "subject": subject,
        "difficulty": q["difficulty"].lower() if q["difficulty"] else None,
        "passage": None,
        "image_url": None,
        "skill": None,
        "module_variant": None,
        "correct_answer": None,  # filled in from the answer key later
    }


def _parse_questions(lines):
    questions = []
    section = None
    current_q = None
    current_number = None

    def flush():
        if current_q is not None:
            questions.append(_finalize_question(current_q, section, current_number))

    i = 0
    while i < len(lines):
        line = lines[i]

        if ANSWER_KEYS_RE.match(line):
            flush()
            return questions, i

        m = SECTION_RE.match(line)
        if m:
            flush()
            current_q = None
            section = {"subject_name": m.group(1) if m.group(1) == "Math" else "Reading and Writing", "module": m.group(2)}
            i += 1
            continue

        m = QUESTION_RE.match(line)
        if m:
            flush()
            current_number = int(m.group(1))
            current_q = {"body_lines": [], "choices": {}, "grid_in": False, "difficulty": m.group(2)}
            i += 1
            continue

        if current_q is not None:
            m = CHOICE_RE.match(line)
            if m:
                current_q["choices"][m.group(1)] = m.group(2).strip()
            elif GRID_IN_RE.search(line):
                current_q["grid_in"] = True
            elif DIFFICULTY_TAG_RE.match(line):
                current_q["difficulty"] = DIFFICULTY_TAG_RE.match(line).group(1)
            else:
                current_q["body_lines"].append(line)
        i += 1

    flush()
    return questions, len(lines)


def _parse_answer_keys(lines):
    """Single pass over the answer-key section, tracking (subject, module) state
    as it goes. Two known template shapes are supported:
      - bare "Module 1"/"Module 2" headings nested under a "Reading and Writing —
        Answer Key ..." / "Math — Answer Key" wrapper heading, with the Math
        answers given as a 2-column table ("N ans N ans")
      - full "Reading and Writing — Module N" / "Math — Module N" headings used
        directly (no separate wrapper heading), with both subjects' answers given
        as numbered entries ("N. B — explanation" / "N. 4. explanation")
    Stops entirely at a "Bubble Summary" appendix, if present — that section is
    never used as an answer source.
    """
    answers = {
        ("Reading and Writing", "1"): {},
        ("Reading and Writing", "2"): {},
        ("Math", "1"): {},
        ("Math", "2"): {},
    }
    current_subject = None
    current_module = None

    for line in lines:
        if BUBBLE_SUMMARY_RE.search(line):
            break

        m = SECTION_RE.match(line)
        if m:
            current_subject = "Math" if m.group(1) == "Math" else "Reading and Writing"
            current_module = m.group(2)
            continue

        if RW_ANSWER_HEADER_RE.search(line):
            current_subject = "Reading and Writing"
            current_module = None
            continue
        if MATH_ANSWER_HEADER_RE.search(line):
            current_subject = "Math"
            current_module = None
            continue

        m = MODULE_HEADING_RE.match(line)
        if m:
            current_module = m.group(1)
            continue

        if current_subject is None or current_module is None:
            continue
        key = (current_subject, current_module)

        m = ANSWER_ENTRY_RE.match(line)
        if m:
            value = m.group(2).replace("−", "-")
            answers[key][int(m.group(1))] = value.upper() if value.isalpha() else value
            continue

        m = MATH_ANSWER_ROW_RE.match(line)
        if m and current_subject == "Math":
            answers[key][int(m.group(1))] = m.group(2)
            answers[key][int(m.group(3))] = m.group(4)

    return answers


def _clean_test_name_from_filename(filename):
    base = os.path.splitext(filename)[0]
    base = base.rstrip(". ")
    base = _COPY_PREFIX_RE.sub("", base).strip()
    base = base.replace("_", " ").replace("-", " ")
    base = _CAMEL_BOUNDARY_RE.sub(" ", base)
    base = _LETTER_DIGIT_BOUNDARY_RE.sub(" ", base)
    words = [w for w in base.split() if w.lower() not in _NAME_NOISE_WORDS]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _parse_lines(lines, original_filename=None):
    test_name = _clean_test_name_from_filename(original_filename) if original_filename else ""
    if not test_name:
        title_match = next((TITLE_RE.search(l) for l in lines if TITLE_RE.search(l)), None)
        test_name = f"SAT Practice Test {title_match.group(1)}" if title_match else "SAT Practice Test"

    questions, answer_key_start = _parse_questions(lines)
    if not questions:
        raise ValueError(
            "Text was extracted from this file, but no \"Question N\" headings matched "
            "the expected template. The file's section headers, question numbering, or "
            "layout may not match the practice-test template this importer expects."
        )
    answer_keys = _parse_answer_keys(lines[answer_key_start:])

    missing_answers = []
    for q in questions:
        key = q.pop("section_key")
        num = q["question_number"]
        answer = answer_keys.get(key, {}).get(num)
        if answer is None:
            missing_answers.append((key, num))
        q["correct_answer"] = answer or ""
        del q["question_number"]

    if missing_answers:
        details = ", ".join(f"{k[0]} Module {k[1]} Q{n}" for k, n in missing_answers)
        raise ValueError(f"Could not find answer-key entries for: {details}")

    return {"test_name": test_name, "questions": questions}


def parse_pdf(pdf_path, original_filename=None):
    return _parse_lines(extract_lines_from_pdf(pdf_path), original_filename or os.path.basename(pdf_path))


def parse_docx(docx_path, original_filename=None):
    return _parse_lines(extract_lines_from_docx(docx_path), original_filename or os.path.basename(docx_path))


def parse_test_file(path, original_filename=None):
    """Dispatch to the right parser based on file extension. `original_filename`
    is used to derive the test name — pass the user-facing upload filename when
    `path` is a temp file (e.g. the admin upload endpoint), since a random temp
    filename wouldn't produce a meaningful test name."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return parse_pdf(path, original_filename)
    if ext == ".docx":
        return parse_docx(path, original_filename)
    raise ValueError(f"Unsupported test file type: {ext or '(none)'}. Expected .pdf or .docx.")


if __name__ == "__main__":
    import sys
    import json

    result = parse_test_file(sys.argv[1])
    print(json.dumps(result, indent=2))
    print(f"\n{len(result['questions'])} questions parsed.", file=sys.stderr)
