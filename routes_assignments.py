from flask import Blueprint, jsonify, request
from database import SessionLocal, Test, Response, Assignment, PracticeResponse

assignments_bp = Blueprint("assignments", __name__)


# --- Assignments ---

@assignments_bp.route("/assignments", methods=["POST"])
def create_assignments():
    """Admin assigns a test to one or more students."""
    db = SessionLocal()
    try:
        data = request.get_json()
        test_id = data.get("test_id")
        user_ids = data.get("user_ids", [])
        due_date = data.get("due_date")  # optional, ISO string e.g. "2026-04-01"

        if not test_id or not user_ids:
            return jsonify({"error": "test_id and user_ids are required"}), 400

        created = []
        for user_id in user_ids:
            assignment = Assignment(
                user_id=user_id,
                test_id=test_id,
                due_date=due_date
            )
            db.add(assignment)
            created.append(user_id)

        db.commit()
        return jsonify({"success": True, "assigned_to": created, "test_id": test_id})
    finally:
        db.close()


@assignments_bp.route("/assignments", methods=["GET"])
def get_assignments():
    """Get all tests assigned to a user."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        assignments = db.query(Assignment).filter(Assignment.user_id == user_id).all()
        return jsonify([
            {
                "assignment_id": a.id,
                "test_id": a.test_id,
                "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
                "due_date": a.due_date.isoformat() if a.due_date else None
            }
            for a in assignments
        ])
    finally:
        db.close()


@assignments_bp.route("/assignments/<int:user_id>/status", methods=["GET"])
def get_assignment_status(user_id):
    """Return each assigned test with completion status for a student."""
    db = SessionLocal()
    try:
        assignments = db.query(Assignment).filter(Assignment.user_id == user_id).all()
        result = []
        for a in assignments:
            test = db.query(Test).filter(Test.id == a.test_id).first()
            has_response = db.query(Response).filter(
                Response.user_id == user_id,
                Response.test_id == a.test_id
            ).first()

            if has_response:
                status = "done"
            elif a.due_date and a.due_date < __import__('datetime').datetime.utcnow():
                status = "overdue"
            else:
                status = "pending"

            result.append({
                "assignment_id": a.id,
                "test_id": a.test_id,
                "test_name": test.title if test else None,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
                "status": status
            })

        return jsonify(result)
    finally:
        db.close()


# --- Practice Results ---

@assignments_bp.route("/practice-results", methods=["GET"])
def get_practice_results():
    """Return practice session results for a user, optionally filtered by topic."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        topic = request.args.get("topic")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        query = db.query(PracticeResponse).filter(PracticeResponse.user_id == user_id)
        if topic:
            query = query.filter(PracticeResponse.topic == topic)

        responses = query.all()
        total = len(responses)
        correct = sum(1 for r in responses if r.is_correct is True)
        incorrect = sum(1 for r in responses if r.is_correct is False)
        accuracy = round((correct / total * 100)) if total > 0 else 0

        return jsonify({
            "user_id": user_id,
            "topic": topic,
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "accuracy": accuracy
        })
    finally:
        db.close()
