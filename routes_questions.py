from flask import Blueprint, g, jsonify, request
from database import SessionLocal, Question, Test, TestUnlock, Assignment
from admin import require_admin, require_login

questions_bp = Blueprint("questions", __name__)


# --- Questions ---

@questions_bp.route("/questions", methods=["GET"])
def get_questions():
    """Return all questions, optionally filtered by subject or difficulty."""
    db = SessionLocal()
    try:
        query = db.query(Question).filter(Question.archived == False)

        subject = request.args.get("subject")
        difficulty = request.args.get("difficulty")

        if subject:
            query = query.filter(Question.subject == subject)
        if difficulty:
            query = query.filter(Question.difficulty == difficulty)
        if request.args.get("practice") == "true":
            query = query.filter(Question.test_id.is_(None))

        questions = query.all()
        return jsonify([
            {
                "id": q.id,
                "text": q.text,
                "choice_a": q.choice_a,
                "choice_b": q.choice_b,
                "choice_c": q.choice_c,
                "choice_d": q.choice_d,
                "correct_answer": q.correct_answer,
                "explanation": q.explanation,
                "subject": q.subject,
                "difficulty": q.difficulty,
                "passage": q.passage,
                "image_url": q.image_url
            }
            for q in questions
        ])
    finally:
        db.close()


# --- Tests ---

@questions_bp.route("/tests", methods=["GET"])
def get_tests():
    """Return all practice tests."""
    db = SessionLocal()
    try:
        # unpublished tests (e.g. adaptive Module 2 not built yet) are hidden from students
        tests = db.query(Test).filter(Test.is_published.isnot(False)).order_by(Test.id).all()
        return jsonify([
            {
                "id": t.id,
                "title": t.title,
                "description": t.description
            }
            for t in tests
        ])
    finally:
        db.close()


@questions_bp.route("/tests/unlocked", methods=["GET"])
@require_login
def get_unlocked_tests():
    """Return the tests the logged-in student has unlocked."""
    db = SessionLocal()
    try:
        tests = (
            db.query(Test)
            .join(TestUnlock, TestUnlock.test_id == Test.id)
            .filter(TestUnlock.user_id == g.user_id)
            .order_by(Test.id)
            .all()
        )
        return jsonify([
            {
                "id": t.id,
                "title": t.title,
                "description": t.description
            }
            for t in tests
        ])
    finally:
        db.close()


def _can_take_test(db, user_id, role, test_id):
    """Admins can open any test; students need it unlocked or assigned."""
    if role == "admin":
        return True
    unlocked = db.query(TestUnlock.id).filter(
        TestUnlock.user_id == user_id, TestUnlock.test_id == test_id
    ).first()
    if unlocked:
        return True
    assigned = db.query(Assignment.id).filter(
        Assignment.user_id == user_id, Assignment.test_id == test_id
    ).first()
    return assigned is not None


@questions_bp.route("/tests/<int:test_id>/questions", methods=["GET"])
@require_login
def get_test_questions(test_id):
    """Return all questions for a specific test, optionally filtered by variant.

    ?variant=easy|hard  only that Module 2 version
    ?variant=none       only questions without a variant (Module 1s, and Module 2s of
                        non-adaptive tests); the adaptive Module 2 is then fetched with
                        POST /tests/<id>/module2 after Module 1 is finished.
    """
    db = SessionLocal()
    try:
        if not _can_take_test(db, g.user_id, g.user_role, test_id):
            return jsonify({"error": "This test is locked"}), 403

        query = db.query(Question).filter(Question.test_id == test_id)

        variant = request.args.get("variant")
        if variant == "none":
            query = query.filter(Question.module_variant.is_(None))
        elif variant:
            query = query.filter(Question.module_variant == variant)

        questions = query.order_by(Question.id).all()
        return jsonify([
            {
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
                "image_url": q.image_url
            }
            for q in questions
        ])
    finally:
        db.close()


_EDITABLE_FIELDS = ('text', 'choice_a', 'choice_b', 'choice_c', 'choice_d',
                    'correct_answer', 'explanation', 'difficulty', 'subject', 'skill')
_NULLABLE_FIELDS = ('explanation', 'difficulty', 'subject', 'skill')


@questions_bp.route('/questions/<int:question_id>', methods=['PATCH'])
@require_admin
def update_question(question_id):
    """Edit a single committed question directly."""
    db = SessionLocal()
    try:
        q = db.query(Question).filter(Question.id == question_id).first()
        if not q:
            return jsonify({'error': 'Not found'}), 404
        body = request.get_json() or {}
        for field in _EDITABLE_FIELDS:
            if field in body:
                val = body[field]
                setattr(q, field, (val or None) if field in _NULLABLE_FIELDS else val)
        db.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
