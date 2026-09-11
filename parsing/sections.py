import re

KEYWORD_HEADER_RE = re.compile(
    r'(answer\s+explanation|explanations?|solutions?|rationale|answer\s+key|answer\s+grid)',
    re.IGNORECASE,
)
CORRECT_ANSWER_BLOCK_RE = re.compile(
    r'^\s*Question\s+(\d+)\s+Correct\s+Answer\s*:\s*[A-D]',
    re.IGNORECASE,
)

GRID_TOKEN_RE = re.compile(r'^(\d+|[A-D])\.?$', re.IGNORECASE)


def detect_explanation_start(blocks, question_matches):
    """Return the block index where explanations begin, or None."""
    if not question_matches:
        return None

    last_q_idx = question_matches[-1][0]
    first_q_num = question_matches[0][1]

    # Strategy 1: "Question N Correct Answer: X" format resetting to question 1
    for b in blocks:
        if b.index <= last_q_idx:
            continue
        m = CORRECT_ANSWER_BLOCK_RE.match(b.text)
        if m and int(m.group(1)) == first_q_num:
            return b.index

    # Strategy 2: plain number reset — explanation section re-numbers from 1
    number_patterns = [
        r'^\s*(\d+)\.\s',
        r'^\s*(\d+)\)\s',
        r'^\s*Question\s+(\d+)\b',
        r'^\s*Q\s*(\d+)',
    ]
    for b in blocks:
        if b.index <= last_q_idx:
            continue
        for pat in number_patterns:
            m = re.match(pat, b.text, re.IGNORECASE)
            if m and int(m.group(1)) == first_q_num:
                return b.index

    # Strategy 3: keyword section header
    for b in blocks:
        if b.index <= last_q_idx:
            continue
        if KEYWORD_HEADER_RE.search(b.text):
            return b.index

    return None


def detect_answer_grid_blocks(blocks, after_idx, before_idx):
    """Return set of block indices that look like answer key grids (to skip)."""
    grid_indices = set()
    for b in blocks:
        if b.index <= after_idx or b.index >= before_idx:
            continue
        tokens = b.text.split()
        if not tokens:
            continue
        matches = sum(1 for t in tokens if GRID_TOKEN_RE.match(t))
        if matches / len(tokens) > 0.7:
            grid_indices.add(b.index)
    return grid_indices
