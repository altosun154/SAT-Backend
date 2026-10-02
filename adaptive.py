"""Adaptive Module 2 rules — pure Python, no database code (easy to test and to reuse).

How a practice test works
  * Module 1 of each section is fixed: every student gets the same questions.
  * Module 2 comes in two versions stored on the Question rows as module_variant:
        "easy"  = Lower Module 2  (45% easy / 40% medium / 15% hard)
        "hard"  = Higher Module 2 (15% easy / 40% medium / 45% hard)
  * A student gets the "hard" version when their Module 1 score reaches the threshold.

Module 2 questions come from a shared pool (PoolQuestion). choose_module2() picks the
questions one test needs from the unused part of the pool.
"""
import math

SECTIONS = {
    "reading_writing": {
        "module1_subject": "Section 1, Module 1: Reading and Writing",
        "module2_subject": "Section 1, Module 2: Reading and Writing",
        "length": 27,
        "threshold": 19,          # 19+ correct of 27 in Module 1 -> Higher Module 2
        "mix": {"easy": {"easy": 12, "medium": 11, "hard": 4},
                "hard": {"easy": 4, "medium": 11, "hard": 12}},
    },
    "math": {
        "module1_subject": "Section 2, Module 1: Math",
        "module2_subject": "Section 2, Module 2: Math",
        "length": 22,
        "threshold": 16,          # 16+ correct of 22 in Module 1 -> Higher Module 2
        "mix": {"easy": {"easy": 10, "medium": 9, "hard": 3},
                "hard": {"easy": 3, "medium": 9, "hard": 10}},
    },
}
DIFFICULTIES = ("easy", "medium", "hard")
VARIANTS = ("easy", "hard")        # easy = Lower Module 2, hard = Higher Module 2


class NotEnoughQuestions(Exception):
    def __init__(self, section, difficulty, needed, available):
        self.section, self.difficulty, self.needed, self.available = section, difficulty, needed, available
        super().__init__(f"Not enough unused {difficulty} {section.replace('_', ' & ')} questions "
                         f"in the Module 2 pool: need {needed}, have {available}")


def section_for_subject(subject):
    s = (subject or "").lower()
    if "math" in s:
        return "math"
    if "reading" in s or "writing" in s:
        return "reading_writing"
    return None


def routing_threshold(section, module1_count):
    """Correct answers needed in Module 1 for the Higher Module 2.
    Uses 19/27 and 16/22; older tests with a different Module 1 length use the same ratio."""
    cfg = SECTIONS[section]
    if module1_count == cfg["length"] or module1_count == 0:
        return cfg["threshold"]
    return math.ceil(module1_count * cfg["threshold"] / cfg["length"])


def pick_variant(section, module1_correct, module1_count):
    return "hard" if module1_correct >= routing_threshold(section, module1_count) else "easy"


def needed_per_test(section):
    """Unique pool questions one test uses per difficulty. The two Module 2 versions share
    questions (a student only ever sees one of them), so a test needs the larger of the two counts."""
    mix = SECTIONS[section]["mix"]
    return {d: max(mix["easy"][d], mix["hard"][d]) for d in DIFFICULTIES}


def _spread(items, k):
    """k items spread evenly through a list (keeps a variety of question types)."""
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step + step / 2)] for i in range(k)]


def choose_module2(pool, section, prefer_source=None):
    """Pick one test's Lower and Higher Module 2 for a section.

    pool          – objects with .id, .difficulty, .source_label, .source_position
                    (only UNUSED questions of this section should be passed in)
    prefer_source – source_label to take first (the test's own PDF), then an even
                    spread over the other PDFs.
    Returns {"easy": [questions in order], "hard": [questions in order]}.
    Raises NotEnoughQuestions if the pool is too small.
    """
    cfg = SECTIONS[section]
    need = needed_per_test(section)
    chosen = {}
    for d in DIFFICULTIES:
        cands = [q for q in pool if q.difficulty == d]
        if len(cands) < need[d]:
            raise NotEnoughQuestions(section, d, need[d], len(cands))
        rank = {}
        per_source = {}
        for q in sorted(cands, key=lambda q: (q.source_label or "", q.source_position or 0, q.id)):
            per_source[q.source_label] = per_source.get(q.source_label, 0) + 1
            rank[q.id] = per_source[q.source_label]
        cands.sort(key=lambda q: (q.source_label != prefer_source, rank[q.id], q.source_label or "", q.id))
        chosen[d] = sorted(cands[:need[d]], key=lambda q: (q.source_position or 0, q.source_label or "", q.id))

    result = {}
    for variant in VARIANTS:
        picked = []
        for d in DIFFICULTIES:
            picked += _spread(chosen[d], cfg["mix"][variant][d])
        if section == "reading_writing":
            # SAT R&W order: vocabulary -> structure -> data -> grammar -> transitions,
            # which follows each question's original position in its module
            picked.sort(key=lambda q: ((q.source_position or 0) / cfg["length"], DIFFICULTIES.index(q.difficulty)))
        else:
            picked.sort(key=lambda q: (DIFFICULTIES.index(q.difficulty), q.source_position or 0))
        result[variant] = picked
    return result
