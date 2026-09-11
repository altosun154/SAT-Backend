import re

PATTERNS = [
    ('question_word',           r'^\s*Question\s+(\d+)[.:)]?\s*(?:\[?(EASY|MEDIUM|HARD)\]?)?\s*$'),
    ('question_correct_answer', r'^\s*Question\s+(\d+)\s+Correct\s+Answer\s*:\s*[A-D]\s*$'),
    ('dot',                     r'^\s*(\d+)\.\s+\S'),
    ('dot_bare',                r'^\s*(\d+)\.\s*$'),
    ('paren_right',             r'^\s*(\d+)\)\s+\S'),
    ('paren_bare',              r'^\s*(\d+)\)\s*$'),
    ('number_bare',             r'^\s*(\d+)\s*$'),
    ('q_prefix',                r'^\s*Q\s*(\d+)[.:)]?\s+\S'),
    ('wrapped',                 r'^\s*\((\d+)\)\s+\S'),
]

DIFFICULTY_RE = re.compile(r'\[?(EASY|MEDIUM|HARD)\]?', re.IGNORECASE)
# Marks a grid-in / free-response question (no A-D choices) — the College Board's
# own label for these on Digital SAT answer sheets.
GRID_IN_RE = re.compile(r'student[-\s]produced\s+response', re.IGNORECASE)
NUMBER_PREFIX_RE = re.compile(
    r'^\s*(?:Question\s+)?\d+[.):]?\s*(?:\[?(?:EASY|MEDIUM|HARD)\]?)?\s*',
    re.IGNORECASE,
)

# No IGNORECASE: choice markers are always uppercase; lowercase would produce false positives.
# Trailing whitespace optional: PDFs sometimes emit "A)is" with no space after the paren.
_CHOICE_MARKER_RE = re.compile(
    r'(?:^|[\s ])\(?([A-D])[.)][   ]*',
    re.MULTILINE,
)


def _split_choices(body_text):
    """
    Scan body_text for A/B/C/D choice markers that may appear mid-line or in a grid.
    Returns (choices_dict, pre_choice_text).

    Finds the longest ascending A->B->C->D run starting from each 'A' candidate,
    so a stray A-match before the real choices doesn't consume the run.
    """
    matches = list(_CHOICE_MARKER_RE.finditer(body_text))
    if not matches:
        return {}, body_text.strip()

    best = []
    for start in range(len(matches)):
        if matches[start].group(1) != 'A':
            continue
        run, expected = [], 'A'
        for m in matches[start:]:
            if m.group(1) == expected:
                run.append(m)
                expected = chr(ord(expected) + 1)
        if len(run) > len(best):
            best = run

    # Fallback for 2-column grid layouts where PDF text extraction produces reading order
    # A, C, B, D (or similar) instead of A, B, C, D. Collect the first occurrence
    # of each letter, require all four, sort by text position so extraction is correct,
    # then store each slice under its own letter.
    if len(best) < 4:
        seen = {}
        for m in matches:
            letter = m.group(1)
            if letter not in seen:
                seen[letter] = m
        if {'A', 'B', 'C', 'D'} <= set(seen.keys()):
            best = sorted([seen[l] for l in 'ABCD'], key=lambda m: m.start())

    if len(best) < 2:
        return {}, body_text.strip()

    pre_text = body_text[:best[0].start()].strip()
    choices = {}
    for i, m in enumerate(best):
        end = best[i + 1].start() if i + 1 < len(best) else len(body_text)
        choices[m.group(1)] = body_text[m.end():end].strip()
    return choices, pre_text


def _score_matches(matches):
    if not matches:
        return 0
    run = 0
    expected = matches[0][1]
    for _, n in matches:
        if n == expected:
            run += 1
            expected += 1
    return run / len(matches)


def find_best_pattern(blocks):
    best = None
    best_score = -1
    for name, pattern in PATTERNS:
        matches = []
        for b in blocks:
            m = re.match(pattern, b.text, re.IGNORECASE)
            if m:
                matches.append((b.index, int(m.group(1)), b))
        if len(matches) < 2:
            continue
        score = _score_matches([(i, n) for i, n, _ in matches])
        if score > best_score:
            best_score = score
            best = (name, pattern, matches)
    return best, best_score


def split_into_questions(blocks, matches):
    if not matches:
        return []

    questions = []

    for i, (block_idx, number, start_block) in enumerate(matches):
        end_idx = matches[i + 1][0] if i + 1 < len(matches) else max(b.index for b in blocks) + 1
        question_blocks = [b for b in blocks if block_idx <= b.index < end_idx]
        body_blocks = question_blocks[1:]  # skip the question header line

        difficulty = None
        dm = DIFFICULTY_RE.search(start_block.text)
        if dm:
            difficulty = dm.group(1).lower()

        # If the question text is on the same line as the number (e.g. "1. What is..."),
        # extract it by stripping the number prefix from the header block.
        header_remainder = NUMBER_PREFIX_RE.sub('', start_block.text).strip()

        # Concatenate body blocks then scan for choices -- handles both one-per-line
        # and horizontally-laid-out choices (common in PDF verb-form questions).
        body_text = '\n'.join(b.text for b in body_blocks)

        # Grid-in / free-response questions carry no A-D choices by design — don't
        # run choice detection on them (a stray "student-produced response" label
        # plus prose is never going to score as a real A/B/C/D run, but skipping it
        # outright is both cheaper and avoids relying on that being true).
        is_grid_in = bool(GRID_IN_RE.search(header_remainder) or GRID_IN_RE.search(body_text))
        if is_grid_in:
            choices = {}
            body_q_text = GRID_IN_RE.sub('', body_text).strip()
            header_remainder = GRID_IN_RE.sub('', header_remainder).strip()
        else:
            choices, body_q_text = _split_choices(body_text)

        q_text_parts = []
        if header_remainder:
            q_text_parts.append(header_remainder)
        if body_q_text:
            q_text_parts.append(body_q_text)

        # First embedded/extracted image anywhere in the question's span (graph,
        # figure, etc.) — only one image_url slot exists on Question, so later
        # images in a multi-image question are dropped.
        image_url = None
        for b in question_blocks:
            for seg in b.segments:
                if seg.type == 'image' and seg.ref:
                    image_url = seg.ref
                    break
            if image_url:
                break

        questions.append({
            'number': number,
            'text': ' '.join(q_text_parts).strip(),
            'choice_a': choices.get('A', ''),
            'choice_b': choices.get('B', ''),
            'choice_c': choices.get('C', ''),
            'choice_d': choices.get('D', ''),
            'correct_answer': '',
            'explanation': None,
            'difficulty': difficulty,
            'image_url': image_url,
            'is_grid_in': is_grid_in,
        })

    return questions
