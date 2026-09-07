def validate(questions, explanations):
    warnings = []
    q_nums = {q['number'] for q in questions}
    exp_nums = {n for n, text in explanations.items() if text}

    # Numbering gaps
    if q_nums:
        expected = set(range(min(q_nums), max(q_nums) + 1))
        for n in sorted(expected - q_nums):
            warnings.append({
                'code': 'NUMBERING_GAP',
                'severity': 'blocking',
                'question_number': n,
                'message': f'Question {n} is missing — possible parse break',
            })

    # Missing explanations
    for n in sorted(q_nums - exp_nums):
        warnings.append({
            'code': 'MISSING_EXPLANATION',
            'severity': 'info',
            'question_number': n,
            'message': f'Question {n} has no explanation',
        })

    # Orphan explanations (explanation with no matching question)
    for n in sorted(exp_nums - q_nums):
        warnings.append({
            'code': 'ORPHAN_EXPLANATION',
            'severity': 'warning',
            'question_number': n,
            'message': f'Explanation {n} has no matching question',
        })

    # Missing correct answer
    for q in questions:
        if not q.get('correct_answer', '').strip():
            warnings.append({
                'code': 'MISSING_ANSWER',
                'severity': 'blocking',
                'question_number': q['number'],
                'message': f'Question {q["number"]} has no correct answer',
            })

    # Missing choices — blocking because a question with no choices can't be answered
    # correctly and may have a defaulted correct_answer that looks valid but isn't.
    # Grid-in / free-response questions are exempt — they have no A-D choices by design.
    for q in questions:
        if q.get('is_grid_in'):
            continue
        has_choices = any(q.get(f'choice_{c}') for c in ('a', 'b', 'c', 'd'))
        if not has_choices:
            warnings.append({
                'code': 'NO_CHOICES',
                'severity': 'blocking',
                'question_number': q['number'],
                'message': f'Question {q["number"]} has no answer choices — parse error or unsupported layout',
            })

    # Empty question text
    for q in questions:
        if not q.get('text', '').strip():
            warnings.append({
                'code': 'EMPTY_TEXT',
                'severity': 'blocking',
                'question_number': q['number'],
                'message': f'Question {q["number"]} has no text',
            })

    # Short text outlier (likely truncated)
    lengths = [len(q.get('text', '')) for q in questions]
    if lengths:
        median_len = sorted(lengths)[len(lengths) // 2]
        for q in questions:
            if len(q.get('text', '')) < 20 and median_len > 50:
                warnings.append({
                    'code': 'SHORT_TEXT',
                    'severity': 'warning',
                    'question_number': q['number'],
                    'message': f'Question {q["number"]} text is very short — may be truncated',
                })

    return warnings
