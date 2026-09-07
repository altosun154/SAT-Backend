import os
import tempfile
import jwt
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, jsonify, request
from database import SessionLocal, User, Test, Assignment, Response, Question
from test_file_parser import parse_test_file

ALLOWED_TEST_UPLOAD_EXTENSIONS = (".pdf", ".docx")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth.split(" ", 1)[1]
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.PyJWTError:
            return jsonify({"error": "Invalid token"}), 401
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == payload["user_id"]).first()
            if not user or user.role != "admin":
                return jsonify({"error": "Forbidden"}), 403
        finally:
            db.close()
        return f(*args, **kwargs)
    return decorated


def _compute_scores(db, user_id):
    """Return (mathScore, rwScore) for a user based on their responses. None if no data."""
    responses = db.query(Response).filter(Response.user_id == user_id).all()
    if not responses:
        return None, None

    math_correct = math_total = rw_correct = rw_total = 0

    for r in responses:
        q = db.query(Question).filter(Question.id == r.question_id).first()
        if not q or q.subject is None:
            continue
        is_math = "math" in q.subject.lower()
        if is_math:
            math_total += 1
            if r.is_correct:
                math_correct += 1
        else:
            rw_total += 1
            if r.is_correct:
                rw_correct += 1

    math_score = round(200 + (math_correct / math_total) * 600) if math_total > 0 else None
    rw_score   = round(200 + (rw_correct   / rw_total)   * 600) if rw_total   > 0 else None
    return math_score, rw_score


def _assignment_status(assignment, db):
    """Derive assignment status: done, overdue, or pending."""
    has_response = db.query(Response).filter(
        Response.user_id == assignment.user_id,
        Response.test_id == assignment.test_id
    ).first()
    if has_response:
        return "done"
    if assignment.due_date:
        due = assignment.due_date
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if due < datetime.now(timezone.utc):
            return "overdue"
    return "pending"


# ── GET /admin/users ──────────────────────────────────────────────────────────

@admin_bp.route("/users", methods=["GET"])
@require_admin
def get_users():
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.role.in_(["student", "deactivated"])).all()
        result = []
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

        for u in users:
            math_score, rw_score = _compute_scores(db, u.id)

            last_response = (
                db.query(Response)
                .filter(Response.user_id == u.id)
                .order_by(Response.answered_at.desc())
                .first()
            )
            last_active = last_response.answered_at if last_response else None

            if last_active:
                ts = last_active if last_active.tzinfo else last_active.replace(tzinfo=timezone.utc)
                status = "active" if ts >= thirty_days_ago else "inactive"
            else:
                status = "inactive"

            tests_assigned = db.query(Assignment).filter(Assignment.user_id == u.id).count()

            result.append({
                "id": u.id,
                "name": u.username,
                "email": u.email,
                "status": status,
                "mathScore": math_score,
                "rwScore": rw_score,
                "testsAssigned": tests_assigned,
                "lastActive": last_active.isoformat() if last_active else None,
            })

        return jsonify(result)
    finally:
        db.close()


# ── GET /admin/tests ──────────────────────────────────────────────────────────

@admin_bp.route("/tests", methods=["GET"])
@require_admin
def get_tests():
    db = SessionLocal()
    try:
        tests = db.query(Test).all()
        return jsonify([{"id": t.id, "name": t.title} for t in tests])
    finally:
        db.close()


# ── GET /admin/tests/<test_id>/questions ──────────────────────────────────────

@admin_bp.route("/tests/<int:test_id>/questions", methods=["GET"])
@require_admin
def get_test_questions(test_id):
    db = SessionLocal()
    try:
        questions = db.query(Question).filter(Question.test_id == test_id).all()
        return jsonify([{
            "id": q.id,
            "text": q.text,
            "choice_a": q.choice_a,
            "choice_b": q.choice_b,
            "choice_c": q.choice_c,
            "choice_d": q.choice_d,
            "correct_answer": q.correct_answer,
            "subject": q.subject,
            "difficulty": q.difficulty,
            "passage": q.passage,
            "image_url": q.image_url,
            "skill": q.skill,
            "module_variant": q.module_variant,
        } for q in questions])
    finally:
        db.close()


# ── DELETE /admin/tests/<test_id> ─────────────────────────────────────────────

@admin_bp.route("/tests/<int:test_id>", methods=["DELETE"])
@require_admin
def delete_test(test_id):
    db = SessionLocal()
    try:
        test = db.query(Test).filter(Test.id == test_id).first()
        if not test:
            return jsonify({"error": "Test not found"}), 404

        db.query(Question).filter(Question.test_id == test_id).delete()
        db.query(Assignment).filter(Assignment.test_id == test_id).delete()
        db.delete(test)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── PUT /admin/questions/<question_id> ────────────────────────────────────────

@admin_bp.route("/questions/<int:question_id>", methods=["PUT"])
@require_admin
def update_question(question_id):
    db = SessionLocal()
    try:
        question = db.query(Question).filter(Question.id == question_id).first()
        if not question:
            return jsonify({"error": "Question not found"}), 404

        data = request.get_json(silent=True) or {}
        updatable_fields = [
            "text", "choice_a", "choice_b", "choice_c", "choice_d",
            "correct_answer", "subject", "difficulty", "passage",
            "image_url", "skill", "module_variant",
        ]
        for field in updatable_fields:
            if field in data:
                setattr(question, field, data[field])

        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── DELETE /admin/questions/<question_id> ─────────────────────────────────────

@admin_bp.route("/questions/<int:question_id>", methods=["DELETE"])
@require_admin
def delete_question(question_id):
    db = SessionLocal()
    try:
        question = db.query(Question).filter(Question.id == question_id).first()
        if not question:
            return jsonify({"error": "Question not found"}), 404
        db.delete(question)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── GET /admin/groups ─────────────────────────────────────────────────────────

@admin_bp.route("/groups", methods=["GET"])
@require_admin
def get_groups():
    # Groups not yet implemented — return empty list so the frontend doesn't error
    return jsonify([])


# ── GET /admin/assignments ────────────────────────────────────────────────────

@admin_bp.route("/assignments", methods=["GET"])
@require_admin
def get_assignments():
    db = SessionLocal()
    try:
        assignments = db.query(Assignment).all()
        result = []
        for a in assignments:
            test = db.query(Test).filter(Test.id == a.test_id).first()
            user = db.query(User).filter(User.id == a.user_id).first()
            status = _assignment_status(a, db)

            score = None
            if status == "done":
                math_score, rw_score = _compute_scores(db, a.user_id)
                if math_score is not None and rw_score is not None:
                    score = math_score + rw_score

            result.append({
                "id": a.id,
                "testName": test.title if test else "—",
                "assignedTo": user.username if user else "—",
                "userId": a.user_id,
                "dueDate": a.due_date.isoformat() if a.due_date else None,
                "status": status,
                "score": score,
            })

        return jsonify(result)
    finally:
        db.close()


# ── POST /admin/assignments ───────────────────────────────────────────────────

@admin_bp.route("/assignments", methods=["POST"])
@require_admin
def create_assignment():
    db = SessionLocal()
    try:
        data = request.get_json(silent=True) or {}
        test_id    = data.get("testId")
        due_date   = data.get("dueDate")
        assign_all = data.get("assignAll", False)
        user_id    = data.get("userId")
        group_id   = data.get("groupId")  # groups not implemented yet

        if not test_id:
            return jsonify({"error": "testId is required"}), 400

        if assign_all:
            user_ids = [u.id for u in db.query(User).filter(User.role == "student").all()]
        elif user_id:
            user_ids = [int(user_id)]
        else:
            return jsonify({"error": "userId or assignAll is required"}), 400

        for uid in user_ids:
            db.add(Assignment(user_id=uid, test_id=int(test_id), due_date=due_date))

        db.commit()
        return jsonify({"success": True, "assigned_to": user_ids, "test_id": test_id}), 201
    finally:
        db.close()


# ── DELETE /admin/assignments/<id> ────────────────────────────────────────────

@admin_bp.route("/assignments/<int:assignment_id>", methods=["DELETE"])
@require_admin
def delete_assignment(assignment_id):
    db = SessionLocal()
    try:
        assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
        if not assignment:
            return jsonify({"error": "Assignment not found"}), 404
        db.delete(assignment)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── GET /admin/users/<user_id>/assignments ────────────────────────────────────

@admin_bp.route("/users/<int:user_id>/assignments", methods=["GET"])
@require_admin
def get_user_assignments(user_id):
    db = SessionLocal()
    try:
        assignments = db.query(Assignment).filter(Assignment.user_id == user_id).all()
        result = []
        for a in assignments:
            test = db.query(Test).filter(Test.id == a.test_id).first()
            status = _assignment_status(a, db)

            score = None
            if status == "done":
                math_score, rw_score = _compute_scores(db, user_id)
                if math_score is not None and rw_score is not None:
                    score = math_score + rw_score

            result.append({
                "id": a.id,
                "testName": test.title if test else "—",
                "dueDate": a.due_date.isoformat() if a.due_date else None,
                "status": status,
                "score": score,
            })

        return jsonify(result)
    finally:
        db.close()


# ── DELETE /admin/users/<user_id> ─────────────────────────────────────────────

@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_admin
def delete_user(user_id):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.role == "admin":
            return jsonify({"error": "Cannot delete admin accounts"}), 403
        db.delete(user)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── PATCH /admin/users/<user_id>/deactivate ───────────────────────────────────

@admin_bp.route("/users/<int:user_id>/deactivate", methods=["PATCH"])
@require_admin
def deactivate_user(user_id):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.role == "admin":
            return jsonify({"error": "Cannot deactivate admin accounts"}), 403
        user.role = "deactivated"
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── PATCH /admin/users/<user_id>/reactivate ───────────────────────────────────

@admin_bp.route("/users/<int:user_id>/reactivate", methods=["PATCH"])
@require_admin
def reactivate_user(user_id):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user.role != "deactivated":
            return jsonify({"error": "Account is not deactivated"}), 400
        user.role = "student"
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── POST /admin/upload-test ───────────────────────────────────────────────────

@admin_bp.route("/upload-test", methods=["POST"])
@require_admin
def upload_test():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "file is required (multipart/form-data, field name 'file')"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_TEST_UPLOAD_EXTENSIONS:
        return jsonify({"error": "Only .pdf and .docx files are supported"}), 400

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        data = parse_test_file(tmp_path, original_filename=file.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    finally:
        os.remove(tmp_path)

    test_name = (data.get("test_name") or "").strip()
    questions = data.get("questions", [])

    if not test_name:
        return jsonify({"error": "Could not determine a test name from the uploaded file"}), 400
    if not questions:
        return jsonify({"error": "No questions could be parsed from the uploaded file"}), 400

    db = SessionLocal()
    try:
        test = Test(title=test_name)
        db.add(test)
        db.flush()

        for q in questions:
            db.add(Question(
                test_id=test.id,
                text=q.get("text", ""),
                choice_a=q.get("choice_a", ""),
                choice_b=q.get("choice_b", ""),
                choice_c=q.get("choice_c", ""),
                choice_d=q.get("choice_d", ""),
                correct_answer=q.get("correct_answer", ""),
                subject=q.get("subject"),
                difficulty=q.get("difficulty"),
                passage=q.get("passage"),
                image_url=q.get("image_url"),
                skill=q.get("skill"),
                module_variant=q.get("module_variant"),
            ))

        db.commit()
        return jsonify({
            "success": True,
            "test_id": test.id,
            "test_name": test_name,
            "questions_added": len(questions),
        }), 201
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()
