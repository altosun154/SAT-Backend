from flask import Blueprint, jsonify, request
from database import SessionLocal, Response, TestCompletion, ParentStudent, User

parent_bp = Blueprint("parent", __name__)


# --- Parent ---

@parent_bp.route("/parent/link", methods=["POST"])
def link_parent_student():
    """Link a parent account to a student account."""
    db = SessionLocal()
    try:
        data = request.get_json()
        parent_id = data.get("parent_id")
        student_id = data.get("student_id")

        if not parent_id or not student_id:
            return jsonify({"error": "parent_id and student_id are required"}), 400

        parent = db.query(User).filter(User.id == parent_id, User.role == "parent").first()
        if not parent:
            return jsonify({"error": "Parent account not found"}), 404

        student = db.query(User).filter(User.id == student_id, User.role == "student").first()
        if not student:
            return jsonify({"error": "Student account not found"}), 404

        link = ParentStudent(parent_id=parent_id, student_id=student_id)
        db.add(link)
        db.commit()
        return jsonify({"success": True, "parent_id": parent_id, "student_id": student_id})
    finally:
        db.close()


@parent_bp.route("/parent/<int:parent_id>/students", methods=["GET"])
def get_parent_students(parent_id):
    """Return all students linked to a parent."""
    db = SessionLocal()
    try:
        links = db.query(ParentStudent).filter(ParentStudent.parent_id == parent_id).all()
        students = []
        for link in links:
            student = db.query(User).filter(User.id == link.student_id).first()
            if student:
                students.append({
                    "student_id": student.id,
                    "username": student.username,
                    "email": student.email
                })
        return jsonify(students)
    finally:
        db.close()


@parent_bp.route("/parent/<int:parent_id>/student/<int:student_id>/results", methods=["GET"])
def get_parent_student_results(parent_id, student_id):
    """Allow a parent to view their linked student's test history."""
    db = SessionLocal()
    try:
        link = db.query(ParentStudent).filter(
            ParentStudent.parent_id == parent_id,
            ParentStudent.student_id == student_id
        ).first()
        if not link:
            return jsonify({"error": "This student is not linked to this parent"}), 403

        completions = db.query(TestCompletion).filter(
            TestCompletion.user_id == student_id
        ).order_by(TestCompletion.completed_at.desc()).all()

        history = []
        for c in completions:
            responses = db.query(Response).filter(
                Response.user_id == student_id,
                Response.test_id == c.test_id,
                Response.session_id == c.session_id
            ).all()
            correct = sum(1 for r in responses if r.is_correct is True)
            incorrect = sum(1 for r in responses if r.is_correct is False)
            skipped = sum(1 for r in responses if r.selected_answer is None)
            history.append({
                "session_id": c.session_id,
                "test_id": c.test_id,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                "correct": correct,
                "incorrect": incorrect,
                "skipped": skipped,
                "total": len(responses)
            })

        return jsonify(history)
    finally:
        db.close()
