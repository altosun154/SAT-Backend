from flask import Blueprint, jsonify, request
from database import SessionLocal, Question, Test
from admin import require_admin
from adaptive import SECTIONS, pick_variant, routing_threshold
from grading import answers_match
import module2_builder

adaptive_bp = Blueprint("adaptive", __name__)


def question_json(q):
    """Question as sent to students — never includes the answer or explanation."""
    return {
        "id": q.id,
        "text": q.text,
        "choice_a": q.choice_a,
        "choice_b": q.choice_b,
        "choice_c": q.choice_c,
        "choice_d": q.choice_d,
        "subject": q.subject,
        "difficulty": q.difficulty,
        "module_variant": q.module_variant,
        "question_type": q.question_type or "mcq",
        "skill": q.skill,
        "passage": q.passage,
        "image_url": q.image_url,
    }


@adaptive_bp.route("/tests/<int:test_id>/module2", methods=["POST"])
def get_adaptive_module2(test_id):
    """Grade a finished Module 1 and return the Module 2 the student should get.

    Body: {"section": "reading_writing" | "math",
           "answers": [{"question_id": 12, "selected_answer": "B"}, ...]}
    Returns: {"module2_variant": "easy"|"hard"|null, "module1_correct": n, "module1_total": n,
              "threshold": n, "questions": [...]}
    Routing: 19+ of 27 correct (R&W) / 16+ of 22 (Math) -> "hard" (Higher Module 2).
    """
    data = request.get_json(silent=True) or {}
    section = data.get("section")
    if section not in SECTIONS:
        return jsonify({"error": "section must be 'reading_writing' or 'math'"}), 400
    cfg = SECTIONS[section]

    db = SessionLocal()
    try:
        module1 = (db.query(Question)
                   .filter(Question.test_id == test_id,
                           Question.subject == cfg["module1_subject"],
                           Question.module_variant.is_(None))
                   .all())
        key = {q.id: q.correct_answer for q in module1}
        given = {}
        for a in data.get("answers") or []:
            try:
                given[int(a.get("question_id"))] = a.get("selected_answer")
            except (TypeError, ValueError):
                continue
        correct = sum(1 for qid, ans in key.items() if answers_match(given.get(qid), ans))
        variant = pick_variant(section, correct, len(key))

        base = db.query(Question).filter(Question.test_id == test_id,
                                         Question.subject == cfg["module2_subject"])
        questions = base.filter(Question.module_variant == variant).order_by(Question.id).all()
        if not questions:
            # older tests without Lower/Higher versions: everyone gets the same Module 2
            questions = base.filter(Question.module_variant.is_(None)).order_by(Question.id).all()
            variant = None

        return jsonify({
            "module2_variant": variant,
            "module1_correct": correct,
            "module1_total": len(key),
            "threshold": routing_threshold(section, len(key)),
            "questions": [question_json(q) for q in questions],
        })
    finally:
        db.close()


# ── Admin: Module 2 pool ────────────────────────────────────────────────────

@adaptive_bp.route("/admin/module2-pool", methods=["GET"])
@require_admin
def module2_pool_status():
    """Used/unused pool counts, plus which tests are waiting for a Module 2."""
    db = SessionLocal()
    try:
        waiting = [{"id": t.id, "title": t.title}
                   for t in db.query(Test).filter(Test.is_published.is_(False)).order_by(Test.id)]
        return jsonify({"pool": module2_builder.pool_summary(db), "unpublished_tests": waiting})
    finally:
        db.close()


@adaptive_bp.route("/admin/module2/build", methods=["POST"])
@require_admin
def build_module2s():
    """Build Module 2 for every test that is waiting for one (run after adding pool questions)."""
    db = SessionLocal()
    try:
        results = module2_builder.build_pending(db)
        return jsonify([{"test_id": i, "title": t, "result": r} for i, t, r in results])
    finally:
        db.close()
