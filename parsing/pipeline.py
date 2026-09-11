import os
import re
import hashlib
from parsing.extract_docx import extract_blocks as extract_docx
from parsing.extract_pdf import extract_blocks as extract_pdf
from parsing.numbering import find_best_pattern, split_into_questions
from parsing.sections import detect_explanation_start, KEYWORD_HEADER_RE
from parsing.validate import validate

CORRECT_ANSWER_IN_HEADER_RE = re.compile(r'Correct\s+Answer\s*:\s*([A-D])', re.IGNORECASE)
# Grid-in questions answer with a number/expression, not a letter — same label,
# looser capture. Only applied for questions numbering.py flagged as grid-in.
CORRECT_ANSWER_ANY_RE = re.compile(r'Correct\s+Answer\s*:\s*([^\n]+)', re.IGNORECASE)
# Matches "1. A", "1. A.", "1. A. text", "1. A — text", "1. B — text" formats
ANSWER_LETTER_HEADER_RE = re.compile(r'^\s*\d+[.)]\s+([A-D])[.):)]?\s*(?:—|--)?\s*(.*)', re.IGNORECASE | re.DOTALL)


def _find_keyword_boundary(blocks):
    """Return block index of the first standalone keyword section header, or None.
    Requires the block to be short so we don't match keywords embedded in sentences."""
    for b in blocks:
        text = b.text.strip()
        if KEYWORD_HEADER_RE.search(text) and len(text) < 40:
            return b.index
    return None


def _extract(file_bytes, filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.docx':
        return extract_docx(file_bytes)
    if ext == '.pdf':
        return extract_pdf(file_bytes)
    raise ValueError(f'Unsupported file type: {ext}. Use .pdf or .docx.')


def _parse_explanations(blocks, exp_start_idx, grid_in_numbers=frozenset()):
    """Return {question_number: {'explanation': str, 'correct_answer': str|None}}"""
    if exp_start_idx is None:
        return {}

    exp_blocks = [b for b in blocks if b.index >= exp_start_idx]
    best, score = find_best_pattern(exp_blocks)
    if not best or score < 0.4:
        return {}

    _, _, matches = best
    result = {}
    for i, (block_idx, number, start_block) in enumerate(matches):
        end_idx = matches[i + 1][0] if i + 1 < len(matches) else exp_blocks[-1].index + 1
        body = [b for b in exp_blocks if block_idx <= b.index < end_idx]

        # Extract correct answer from the header block
        # Supports: "Question N Correct Answer: A", "1. A", "1. A Some inline text..."
        correct_answer = None
        inline_explanation = None
        ca_match = CORRECT_ANSWER_IN_HEADER_RE.search(start_block.text)
        if ca_match:
            correct_answer = ca_match.group(1).upper()
        else:
            al_match = ANSWER_LETTER_HEADER_RE.match(start_block.text)
            if al_match:
                correct_answer = al_match.group(1).upper()
                inline_explanation = al_match.group(2).strip() or None

        # Build explanation: inline text from header + any subsequent body blocks
        body_texts = []
        if inline_explanation:
            body_texts.append(inline_explanation)
        body_texts += [b.text for b in body if b.index != block_idx]
        explanation = '\n'.join(body_texts).strip() or None

        # Fall back to scanning the whole explanation entry for "Correct Answer: X" —
        # some templates put this on its own line in the body rather than the header
        # block that carries the question number.
        if not correct_answer and explanation:
            ca_match = CORRECT_ANSWER_IN_HEADER_RE.search(explanation)
            if ca_match:
                correct_answer = ca_match.group(1).upper()

        # Grid-in questions answer with a number/expression rather than a letter —
        # none of the [A-D]-only patterns above can ever match them.
        if not correct_answer and number in grid_in_numbers:
            header_and_body = f'{start_block.text}\n{explanation or ""}'
            ca_match = CORRECT_ANSWER_ANY_RE.search(header_and_body)
            if ca_match:
                correct_answer = ca_match.group(1).strip().rstrip('.')

        result[number] = {
            'explanation': explanation,
            'correct_answer': correct_answer,
        }

    return result


def _questions_with_dropped_math(blocks, matches):
    """Question numbers whose span contains a DOCX paragraph that held a math
    equation (m:oMath) — python-docx's .text silently omits those, so the
    parsed question text may be missing content with no other trace of it."""
    if not matches:
        return set()
    nums = set()
    for i, (block_idx, number, _) in enumerate(matches):
        end_idx = matches[i + 1][0] if i + 1 < len(matches) else max(b.index for b in blocks) + 1
        span = [b for b in blocks if block_idx <= b.index < end_idx]
        if any(b.style.get('has_math') for b in span):
            nums.add(number)
    return nums


def run(file_bytes, filename):
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    blocks = _extract(file_bytes, filename)

    if not blocks:
        raise ValueError('No text could be extracted from the file.')

    # Pre-detect keyword section headers (e.g. "Answer Key", "Explanations") before
    # running pattern detection, so answer sections don't pollute question matching
    # when both sections use the same numbering format.
    pre_boundary = _find_keyword_boundary(blocks)
    candidate_blocks = [b for b in blocks if pre_boundary is None or b.index < pre_boundary]

    best, score = find_best_pattern(candidate_blocks)

    # If pre_boundary cut the document too aggressively (e.g., "explanations" appears
    # in a preamble note before any questions), fall back to all blocks.
    if (not best or score < 0.3) and pre_boundary is not None:
        best, score = find_best_pattern(blocks)
        if best and score >= 0.3:
            pre_boundary = None
            candidate_blocks = blocks

    if not best or score < 0.3:
        sample = ' | '.join(f'[{b.index}]{b.text[:80]}' for b in blocks[:20])
        raise ValueError(
            f'No questions could be detected (best score: {round(score, 3) if best else 0}, '
            f'total blocks: {len(blocks)}). Blocks: {sample}'
        )

    pattern_name, _, all_matches = best

    # Fine-tune explanation boundary (handles Triumph-style and number-reset formats)
    exp_start = detect_explanation_start(blocks, all_matches) or pre_boundary
    q_blocks = [b for b in blocks if exp_start is None or b.index < exp_start]

    # Re-run pattern detection on question-only region for cleaner splits
    best_q, _ = find_best_pattern(q_blocks)
    q_matches = best_q[2] if best_q else all_matches

    questions = split_into_questions(q_blocks, q_matches)
    grid_in_numbers = {q['number'] for q in questions if q.get('is_grid_in')}
    explanations = _parse_explanations(blocks, exp_start, grid_in_numbers)

    for q in questions:
        pair = explanations.get(q['number'], {})
        q['explanation'] = pair.get('explanation')
        if pair.get('correct_answer'):
            q['correct_answer'] = pair['correct_answer']

    exp_texts = {n: d.get('explanation') for n, d in explanations.items()}
    warnings = validate(questions, exp_texts)

    for n in sorted(_questions_with_dropped_math(q_blocks, q_matches)):
        warnings.append({
            'code': 'MATH_CONTENT_DROPPED',
            'severity': 'warning',
            'question_number': n,
            'message': f'Question {n} contains a math equation that could not be '
                       f'extracted from the DOCX — check it manually before committing',
        })

    return {
        'file_hash': file_hash,
        'source_filename': filename,
        'meta': {
            'subject': '',
            'topic': '',
        },
        'questions': questions,
        'warnings': warnings,
        'explanation_count': len(explanations),
        'pattern_used': pattern_name,
        'pattern_score': round(score, 3),
        '_debug': {
            'total_blocks': len(blocks),
            'pre_boundary': pre_boundary,
            'exp_start': exp_start,
            'q_blocks_count': len(q_blocks),
            'first_5_blocks': [b.text[:80] for b in blocks[:5]],
            'boundary_area_blocks': [b.text[:80] for b in blocks if pre_boundary is not None and abs(b.index - pre_boundary) <= 3],
        },
    }
