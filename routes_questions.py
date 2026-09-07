from flask import Blueprint, jsonify, request
from database import SessionLocal, Question, Test
from admin import require_admin

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
        tests = db.query(Test).all()
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


@questions_bp.route("/tests/<int:test_id>/questions", methods=["GET"])
def get_test_questions(test_id):
    """Return all questions for a specific test, optionally filtered by variant (easy/hard)."""
    db = SessionLocal()
    try:
        query = db.query(Question).filter(Question.test_id == test_id)

        variant = request.args.get("variant")
        if variant:
            query = query.filter(Question.module_variant == variant)

        questions = query.all()
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
