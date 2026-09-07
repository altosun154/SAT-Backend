from flask import Blueprint, jsonify, request
from database import SessionLocal, Response, PracticeResponse, Activity

activity_data_bp = Blueprint("activity_data", __name__)


# --- Activity ---

@activity_data_bp.route("/activity", methods=["GET"])
def get_activity():
    """Return distinct dates on which a user submitted at least one answer."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        test_responses = db.query(Response.answered_at).filter(
            Response.user_id == user_id,
            Response.answered_at != None
        ).all()

        practice_responses = db.query(PracticeResponse.answered_at).filter(
            PracticeResponse.user_id == user_id,
            PracticeResponse.answered_at != None
        ).all()

        login_dates = db.query(Activity.date).filter(
            Activity.user_id == user_id
        ).all()

        dates = sorted(
            {r.answered_at.date().isoformat() for r in test_responses + practice_responses}
            | {r.date for r in login_dates}
        )
        return jsonify({"user_id": user_id, "activity_dates": dates})
    finally:
        db.close()


@activity_data_bp.route("/activity/practice", methods=["POST"])
def log_practice_activity():
    """Receive and store topic practice question completions for a user."""
    db = SessionLocal()
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        answers = data.get("answers", [])

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        for item in answers:
            record = PracticeResponse(
                user_id=user_id,
                question_id=item.get("question_id"),
                topic=item.get("topic"),
                is_correct=item.get("is_correct")
            )
            db.add(record)

        db.commit()
        return jsonify({"success": True, "saved": len(answers)})
    finally:
        db.close()
