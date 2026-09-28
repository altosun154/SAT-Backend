import json
import os
import tempfile
import uuid
import jwt
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Blueprint, g, jsonify, request
from sqlalchemy.exc import IntegrityError
from database import SessionLocal, User, Test, Assignment, Response, Question, TestUnlock, TestDraft
from test_file_parser import SUBJECT_MAP, parse_test_file

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


def require_login(f):
    """Require a valid Bearer JWT for an active account; exposes the caller as
    g.user_id / g.user_role. The short-lived 2FA-pending token doesn't count."""
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
        if payload.get("purpose") == "2fa_pending":
            return jsonify({"error": "Invalid token"}), 401
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == payload.get("user_id")).first()
            if not user or user.role == "deactivated":
                return jsonify({"error": "Unauthorized"}), 401
            g.user_id = user.id
            g.user_role = user.role
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
            "explanation": q.explanation,
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
        db.query(TestUnlock).filter(TestUnlock.test_id == test_id).delete()
        db.delete(test)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()


# ── GET/PUT /admin/tests/<test_id>/unlocks ────────────────────────────────────

def _unlocked_students(db, test_id):
    rows = (
        db.query(TestUnlock, User)
        .join(User, User.id == TestUnlock.user_id)
        .filter(TestUnlock.test_id == test_id)
        .order_by(User.username)
        .all()
    )
    return [{
        "id": user.id,
        "name": user.username,
        "email": user.email,
        "unlockedAt": unlock.unlocked_at.isoformat() if unlock.unlocked_at else None,
    } for unlock, user in rows]


@admin_bp.route("/tests/<int:test_id>/unlocks", methods=["GET"])
@require_admin
def get_test_unlocks(test_id):
    db = SessionLocal()
    try:
        if not db.query(Test).filter(Test.id == test_id).first():
            return jsonify({"error": "Test not found"}), 404
        return jsonify({"test_id": test_id, "students": _unlocked_students(db, test_id)})
    finally:
        db.close()


@admin_bp.route("/tests/<int:test_id>/unlocks", methods=["PUT"])
@require_admin
def set_test_unlocks(test_id):
    """Replace the set of students who have this test unlocked with `user_ids`."""
    data = request.get_json(silent=True) or {}
    user_ids = data.get("user_ids")
    if not isinstance(user_ids, list) or not all(isinstance(u, int) and not isinstance(u, bool) for u in user_ids):
        return jsonify({"error": "user_ids must be a list of user ids"}), 400
    wanted = set(user_ids)

    db = SessionLocal()
    try:
        if not db.query(Test).filter(Test.id == test_id).first():
            return jsonify({"error": "Test not found"}), 404

        students = {
            u.id for u in db.query(User.id).filter(User.id.in_(wanted), User.role == "student").all()
        } if wanted else set()
        invalid = sorted(wanted - students)
        if invalid:
            return jsonify({"error": "Not student accounts", "user_ids": invalid}), 400

        current = {
            u.user_id for u in db.query(TestUnlock.user_id).filter(TestUnlock.test_id == test_id).all()
        }
        removed = current - wanted
        if removed:
            db.query(TestUnlock).filter(
                TestUnlock.test_id == test_id, TestUnlock.user_id.in_(removed)
            ).delete(synchronize_session=False)
        for user_id in wanted - current:
            db.add(TestUnlock(user_id=user_id, test_id=test_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return jsonify({"error": "Unlocks were changed at the same time — reload and try again"}), 409

        return jsonify({"test_id": test_id, "students": _unlocked_students(db, test_id)})
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
            "image_url", "skill", "explanation", "module_variant",
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


# ── Test file import helpers ──────────────────────────────────────────────────

def _parse_uploaded_test():
    """Parse the uploaded test file in request.files["file"]. Returns
    (test_name, questions, filename, None) on success, or
    (None, None, None, error_response) on failure."""
    file = request.files.get("file")
    if not file or not file.filename:
        return None, None, None, (jsonify({"error": "file is required (multipart/form-data, field name 'file')"}), 400)

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_TEST_UPLOAD_EXTENSIONS:
        return None, None, None, (jsonify({"error": "Only .pdf and .docx files are supported"}), 400)

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        data = parse_test_file(tmp_path, original_filename=file.filename)
    except ValueError as e:
        return None, None, None, (jsonify({"error": str(e)}), 400)
    except Exception as e:
        return None, None, None, (jsonify({"error": f"Could not parse this file: {e}"}), 400)
    finally:
        os.remove(tmp_path)

    test_name = (data.get("test_name") or "").strip()
    questions = data.get("questions", [])

    if not test_name:
        return None, None, None, (jsonify({"error": "Could not determine a test name from the uploaded file"}), 400)
    if not questions:
        return None, None, None, (jsonify({"error": "No questions could be parsed from the uploaded file"}), 400)
    return test_name, questions, file.filename, None


def _add_test_with_questions(db, test_name, questions):
    """Add a Test and its Question rows to the session (caller commits)."""
    test = Test(title=test_name)
    db.add(test)
    db.flush()

    for q in questions:
        db.add(Question(
            test_id=test.id,
            text=q.get("text") or "",
            choice_a=q.get("choice_a") or "",
            choice_b=q.get("choice_b") or "",
            choice_c=q.get("choice_c") or "",
            choice_d=q.get("choice_d") or "",
            correct_answer=q.get("correct_answer") or "",
            subject=q.get("subject"),
            difficulty=q.get("difficulty") or None,
            passage=q.get("passage") or None,
            image_url=q.get("image_url") or None,
            skill=q.get("skill") or None,
            explanation=q.get("explanation") or None,
            module_variant=q.get("module_variant") or None,
        ))
    return test


# ── POST /admin/upload-test ───────────────────────────────────────────────────
# Imports straight to the DB. Superseded by the /admin/test-drafts review flow.

@admin_bp.route("/upload-test", methods=["POST"])
@require_admin
def upload_test():
    test_name, questions, _, error = _parse_uploaded_test()
    if error:
        return error

    db = SessionLocal()
    try:
        test = _add_test_with_questions(db, test_name, questions)
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


# ── Test drafts (review before publish) ───────────────────────────────────────

VALID_SUBJECTS = set(SUBJECT_MAP.values())
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
CHOICE_KEYS = ("choice_a", "choice_b", "choice_c", "choice_d")
MAX_ANSWER_LENGTH = 10  # Question.correct_answer is String(10)


def _iso(dt):
    return dt.isoformat() if dt else None


def _draft_json(draft):
    return {
        "id": draft.id,
        "test_name": draft.test_name,
        "filename": draft.original_filename,
        "created_at": _iso(draft.created_at),
        "updated_at": _iso(draft.updated_at),
        "questions": json.loads(draft.questions_json),
    }


def _text(value):
    """A draft field as stripped text; drafts are saved unvalidated, so any
    field may be missing, null or a non-string."""
    return "" if value is None else str(value).strip()


def _validate_draft(test_name, questions):
    """Check a draft is publishable. Returns (problems, cleaned_questions), where
    cleaned questions have stripped text, an uppercased multiple-choice answer
    and a lowercased difficulty."""
    problems = []

    def problem(index, field, message):
        problems.append({"index": index, "field": field, "message": message})

    if not _text(test_name):
        problem(None, "test_name", "Test name is required.")
    if not questions:
        problem(None, "questions", "The test has no questions.")

    cleaned = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            problem(i, "question", "Question data is malformed.")
            continue
        q = dict(q)

        if not _text(q.get("text")):
            problem(i, "text", "Question text is required.")
        if q.get("subject") not in VALID_SUBJECTS:
            problem(i, "subject", "Subject must be one of: " + ", ".join(SUBJECT_MAP.values()) + ".")

        answer = _text(q.get("correct_answer"))
        choices = [_text(q.get(k)) for k in CHOICE_KEYS]
        if not answer:
            problem(i, "correct_answer", "Correct answer is required.")
        elif any(choices):
            for key, choice in zip(CHOICE_KEYS, choices):
                if not choice:
                    problem(i, key, f"Choice {key[-1].upper()} is empty; a multiple-choice question needs all four choices.")
            answer = answer.upper()
            if answer not in ("A", "B", "C", "D"):
                problem(i, "correct_answer", "A multiple-choice answer must be A, B, C or D.")
        if len(answer) > MAX_ANSWER_LENGTH:
            problem(i, "correct_answer", f"Correct answer can be at most {MAX_ANSWER_LENGTH} characters.")
        q["correct_answer"] = answer

        difficulty = _text(q.get("difficulty")).lower()
        if difficulty and difficulty not in VALID_DIFFICULTIES:
            problem(i, "difficulty", "Difficulty must be easy, medium or hard (or left empty).")
        q["difficulty"] = difficulty or None

        cleaned.append(q)
    return problems, cleaned


# ── POST /admin/test-drafts ───────────────────────────────────────────────────

@admin_bp.route("/test-drafts", methods=["POST"])
@require_admin
def create_test_draft():
    test_name, questions, filename, error = _parse_uploaded_test()
    if error:
        return error

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        draft = TestDraft(
            id=str(uuid.uuid4()),
            test_name=test_name,
            original_filename=filename,
            questions_json=json.dumps(questions),
            created_at=now,
            updated_at=now,
        )
        db.add(draft)
        db.commit()
        return jsonify(_draft_json(draft)), 201
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ── GET /admin/test-drafts ────────────────────────────────────────────────────

@admin_bp.route("/test-drafts", methods=["GET"])
@require_admin
def list_test_drafts():
    db = SessionLocal()
    try:
        drafts = db.query(TestDraft).order_by(TestDraft.updated_at.desc()).all()
        return jsonify([{
            "id": d.id,
            "test_name": d.test_name,
            "filename": d.original_filename,
            "question_count": len(json.loads(d.questions_json)),
            "created_at": _iso(d.created_at),
            "updated_at": _iso(d.updated_at),
        } for d in drafts])
    finally:
        db.close()


# ── GET /admin/test-drafts/<id> ───────────────────────────────────────────────

@admin_bp.route("/test-drafts/<draft_id>", methods=["GET"])
@require_admin
def get_test_draft(draft_id):
    db = SessionLocal()
    try:
        draft = db.get(TestDraft, draft_id)
        if not draft:
            return jsonify({"error": "Draft not found"}), 404
        return jsonify(_draft_json(draft))
    finally:
        db.close()


# ── PUT /admin/test-drafts/<id> ───────────────────────────────────────────────
# Saves work in progress as-is; validation happens at publish.

@admin_bp.route("/test-drafts/<draft_id>", methods=["PUT"])
@require_admin
def update_test_draft(draft_id):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    questions = body.get("questions")
    if not isinstance(questions, list):
        return jsonify({"error": "questions must be a list"}), 400

    db = SessionLocal()
    try:
        draft = db.get(TestDraft, draft_id)
        if not draft:
            return jsonify({"error": "Draft not found"}), 404
        # test_name is NOT NULL and String(200); store what fits, publish validates it.
        draft.test_name = _text(body.get("test_name"))[:200]
        draft.questions_json = json.dumps(questions)
        draft.updated_at = datetime.now(timezone.utc)
        db.commit()
        return jsonify(_draft_json(draft))
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()


# ── POST /admin/test-drafts/<id>/publish ──────────────────────────────────────

@admin_bp.route("/test-drafts/<draft_id>/publish", methods=["POST"])
@require_admin
def publish_test_draft(draft_id):
    db = SessionLocal()
    try:
        draft = db.get(TestDraft, draft_id)
        if not draft:
            return jsonify({"error": "Draft not found"}), 404

        test_name = _text(draft.test_name)
        problems, questions = _validate_draft(test_name, json.loads(draft.questions_json))
        if problems:
            return jsonify({
                "error": f"Fix {len(problems)} problem(s) before publishing",
                "problems": problems,
            }), 422

        test = _add_test_with_questions(db, test_name, questions)
        db.delete(draft)
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


# ── DELETE /admin/test-drafts/<id> ────────────────────────────────────────────

@admin_bp.route("/test-drafts/<draft_id>", methods=["DELETE"])
@require_admin
def delete_test_draft(draft_id):
    db = SessionLocal()
    try:
        draft = db.get(TestDraft, draft_id)
        if not draft:
            return jsonify({"error": "Draft not found"}), 404
        db.delete(draft)
        db.commit()
        return jsonify({"success": True})
    finally:
        db.close()
