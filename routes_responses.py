from flask import Blueprint, jsonify, request
from database import SessionLocal, Question, Response, TestCompletion

responses_bp = Blueprint("responses", __name__)


# --- Responses ---

@responses_bp.route("/submit", methods=["POST"])
def submit_answer():
    """Save a module's answers. Accepts a bulk payload from the frontend."""
    db = SessionLocal()
    try:
        data = request.get_json()
        test_id = data.get("test_id", 1)
        user_id = data.get("user_id", 1)
        answers = data.get("answers", [])
        session_id = data.get("session_id")
        is_final = data.get("final", False)

        # Clear only the specific questions being resubmitted, not the whole session
        question_ids = [item.get("question_id") for item in answers if item.get("question_id")]
        if question_ids:
            query = db.query(Response).filter(
                Response.user_id == user_id,
                Response.test_id == test_id,
                Response.question_id.in_(question_ids)
            )
            if session_id:
                query = query.filter(Response.session_id == session_id)
            query.delete(synchronize_session=False)

        for item in answers:
            question_id = item.get("question_id")
            selected = item.get("selected_answer") or item.get("selected_choice")

            question = db.query(Question).filter(Question.id == question_id).first()
            is_correct = question.correct_answer.upper() == selected.upper() if question and selected else None

            response = Response(
                user_id=user_id,
                test_id=test_id,
                question_id=question_id,
                selected_answer=selected,
                is_correct=is_correct,
                session_id=session_id
            )
            db.add(response)

        if is_final:
            db.add(TestCompletion(user_id=user_id, test_id=test_id, session_id=session_id))
        db.commit()
        return jsonify({"success": True, "saved": len(answers)})
    finally:
        db.close()


# --- Adaptive ---

DIFFICULTY_WEIGHTS = {"easy": 1, "medium": 2, "hard": 3}
WEIGHTED_THRESHOLD = 25

@responses_bp.route("/adaptive/module2", methods=["POST"])
def get_module2_difficulty():
    """Return easy or hard for Module 2 based on weighted Module 1 score."""
    data = request.get_json()
    user_id = data.get("user_id")
    session_id = data.get("session_id")
    m1_score = data.get("m1_score")  # fallback if no session data

    weighted_score = None

    if user_id and session_id:
        db = SessionLocal()
        try:
            responses = db.query(Response).filter(
                Response.user_id == user_id,
                Response.session_id == session_id
            ).all()

            weighted_score = 0
            for r in responses:
                if r.is_correct:
                    question = db.query(Question).filter(Question.id == r.question_id).first()
                    weight = DIFFICULTY_WEIGHTS.get(
                        (question.difficulty or "easy").lower(), 1
                    ) if question else 1
                    weighted_score += weight
        finally:
            db.close()

    if weighted_score is not None:
        difficulty = "hard" if weighted_score >= WEIGHTED_THRESHOLD else "easy"
    else:
        difficulty = "hard" if (m1_score or 0) >= 0.6 else "easy"

    return jsonify({"difficulty": difficulty, "weighted_score": weighted_score})
