from flask import Blueprint, jsonify, request
from database import SessionLocal, Question, Response, TestCompletion

results_bp = Blueprint("results", __name__)


# --- Results ---

@results_bp.route("/results/history", methods=["GET"])
def get_results_history():
    """Return all past test sessions for a user, ordered newest first."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        completions = db.query(TestCompletion).filter(
            TestCompletion.user_id == user_id
        ).order_by(TestCompletion.completed_at.desc()).all()

        # Preload all question metadata for questions in this user's responses
        # to avoid N+1 queries across sessions.
        question_ids = [
            r.question_id
            for c in completions
            for r in db.query(Response.question_id).filter(
                Response.user_id == user_id,
                Response.session_id == c.session_id,
            ).all()
        ]
        questions_by_id = {
            q.id: q
            for q in db.query(Question).filter(Question.id.in_(question_ids)).all()
        } if question_ids else {}

        history = []
        for c in completions:
            responses = db.query(Response).filter(
                Response.user_id == user_id,
                Response.test_id == c.test_id,
                Response.session_id == c.session_id
            ).all()

            correct = incorrect = skipped = 0
            math_correct = math_total = rw_correct = rw_total = 0
            # skill -> {correct, incorrect, skipped}
            skill_breakdown: dict = {}

            for r in responses:
                if r.is_correct is True:
                    correct += 1
                elif r.selected_answer is None:
                    skipped += 1
                else:
                    incorrect += 1

                q = questions_by_id.get(r.question_id)
                if q:
                    is_math = q.subject and "math" in q.subject.lower()
                    if is_math:
                        math_total += 1
                        if r.is_correct:
                            math_correct += 1
                    else:
                        rw_total += 1
                        if r.is_correct:
                            rw_correct += 1

                    skill = q.subject or "Unknown"
                    if skill not in skill_breakdown:
                        skill_breakdown[skill] = {"correct": 0, "incorrect": 0, "skipped": 0}
                    if r.is_correct is True:
                        skill_breakdown[skill]["correct"] += 1
                    elif r.selected_answer is None:
                        skill_breakdown[skill]["skipped"] += 1
                    else:
                        skill_breakdown[skill]["incorrect"] += 1

            math_score = round(200 + (math_correct / math_total) * 600) if math_total > 0 else None
            rw_score = round(200 + (rw_correct / rw_total) * 600) if rw_total > 0 else None
            total_score = (math_score + rw_score) if math_score is not None and rw_score is not None else None

            history.append({
                "session_id": c.session_id,
                "test_id": c.test_id,
                "completed_at": c.completed_at.isoformat() if c.completed_at else None,
                "correct": correct,
                "incorrect": incorrect,
                "skipped": skipped,
                "math_score": math_score,
                "rw_score": rw_score,
                "total_score": total_score,
                "skill_breakdown": skill_breakdown,
            })

        return jsonify(history)
    finally:
        db.close()


@results_bp.route("/results/review-answer", methods=["POST"])
def submit_review_answer():
    """Store a new response row when a user re-answers a question during review."""
    db = SessionLocal()
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        test_id = data.get("test_id")
        question_id = data.get("question_id")
        selected = data.get("selected_answer")

        if not all([user_id, test_id, question_id, selected]):
            return jsonify({"error": "user_id, test_id, question_id, and selected_answer are required"}), 400

        question = db.query(Question).filter(Question.id == question_id).first()
        if not question:
            return jsonify({"error": "Question not found"}), 404

        is_correct = question.correct_answer.upper() == selected.upper()

        db.add(Response(
            user_id=user_id,
            test_id=test_id,
            question_id=question_id,
            selected_answer=selected,
            is_correct=is_correct
        ))
        db.commit()
        return jsonify({"success": True, "is_correct": is_correct, "correct_answer": question.correct_answer})
    finally:
        db.close()


@results_bp.route("/results", methods=["GET"])
def get_results():
    """Return score breakdown for a user and test."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        test_id = request.args.get("test_id")
        session_id = request.args.get("session_id")

        query = db.query(Response).filter(
            Response.user_id == user_id,
            Response.test_id == test_id
        )
        if session_id:
            query = query.filter(Response.session_id == session_id)
        responses = query.all()

        total = len(responses)
        correct = sum(1 for r in responses if r.is_correct is True)
        incorrect = sum(1 for r in responses if r.is_correct is False)
        skipped = sum(1 for r in responses if r.selected_answer is None)

        subjects = {}
        for r in responses:
            question = db.query(Question).filter(Question.id == r.question_id).first()
            if not question:
                continue
            subj = question.subject or "Unknown"
            if subj not in subjects:
                subjects[subj] = {"correct": 0, "incorrect": 0, "skipped": 0}
            if r.is_correct is True:
                subjects[subj]["correct"] += 1
            elif r.selected_answer is None:
                subjects[subj]["skipped"] += 1
            else:
                subjects[subj]["incorrect"] += 1

        accuracy = round((correct / total * 100)) if total > 0 else 0

        by_skill = {}
        for skill, counts in subjects.items():
            answered = counts["correct"] + counts["incorrect"]
            pct_correct = round((counts["correct"] / answered * 100)) if answered > 0 else 0
            by_skill[skill] = {**counts, "pct_correct": pct_correct}

        # Calculate math and R&W scores separately (200-800 scale each)
        math_correct = math_total = rw_correct = rw_total = 0
        for r in responses:
            question = db.query(Question).filter(Question.id == r.question_id).first()
            if not question or not question.subject:
                continue
            if "math" in question.subject.lower():
                math_total += 1
                if r.is_correct:
                    math_correct += 1
            else:
                rw_total += 1
                if r.is_correct:
                    rw_correct += 1

        math_score = round(200 + (math_correct / math_total) * 600) if math_total > 0 else None
        rw_score = round(200 + (rw_correct / rw_total) * 600) if rw_total > 0 else None
        total_score = (math_score + rw_score) if math_score and rw_score else None

        return jsonify({
            "test_id": test_id,
            "user_id": user_id,
            "total": total,
            "correct": correct,
            "incorrect": incorrect,
            "skipped": skipped,
            "accuracy": accuracy,
            "math_score": math_score,
            "rw_score": rw_score,
            "total_score": total_score,
            "percentile": None,
            "by_subject": subjects,
            "by_skill": by_skill
        })
    finally:
        db.close()


@results_bp.route("/results/incorrect", methods=["GET"])
def get_incorrect_questions():
    """Return the full question details for every question the student got wrong."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        test_id = int(request.args.get("test_id"))
        session_id = request.args.get("session_id")

        query = db.query(Response).filter(
            Response.user_id == user_id,
            Response.test_id == test_id,
            Response.is_correct == False
        )
        if session_id:
            query = query.filter(Response.session_id == session_id)
        wrong = query.all()

        result = []
        for r in wrong:
            question = db.query(Question).filter(Question.id == r.question_id).first()
            if not question:
                continue
            result.append({
                "question_id": question.id,
                "text": question.text,
                "passage": question.passage,
                "choice_a": question.choice_a,
                "choice_b": question.choice_b,
                "choice_c": question.choice_c,
                "choice_d": question.choice_d,
                "correct_answer": question.correct_answer,
                "selected_answer": r.selected_answer,
                "subject": question.subject,
                "difficulty": question.difficulty,
                "explanation": question.explanation or "Explanation coming soon."
            })

        return jsonify(result)
    finally:
        db.close()


@results_bp.route("/results/correct", methods=["GET"])
def get_correct_questions():
    """Return full question details for every question the student got right."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        test_id = int(request.args.get("test_id"))
        session_id = request.args.get("session_id")

        query = db.query(Response).filter(
            Response.user_id == user_id,
            Response.test_id == test_id,
            Response.is_correct == True
        )
        if session_id:
            query = query.filter(Response.session_id == session_id)
        correct_responses = query.all()

        result = []
        for r in correct_responses:
            question = db.query(Question).filter(Question.id == r.question_id).first()
            if not question:
                continue
            result.append({
                "question_id": question.id,
                "text": question.text,
                "passage": question.passage,
                "choice_a": question.choice_a,
                "choice_b": question.choice_b,
                "choice_c": question.choice_c,
                "choice_d": question.choice_d,
                "correct_answer": question.correct_answer,
                "selected_answer": r.selected_answer,
                "subject": question.subject,
                "difficulty": question.difficulty
            })

        return jsonify(result)
    finally:
        db.close()


@results_bp.route("/results/skipped", methods=["GET"])
def get_skipped_questions():
    """Return full question details for every question the student skipped."""
    db = SessionLocal()
    try:
        user_id = request.args.get("user_id")
        test_id = int(request.args.get("test_id"))
        session_id = request.args.get("session_id")

        query = db.query(Response).filter(
            Response.user_id == user_id,
            Response.test_id == test_id,
            Response.selected_answer == None
        )
        if session_id:
            query = query.filter(Response.session_id == session_id)
        skipped_responses = query.all()

        result = []
        for r in skipped_responses:
            question = db.query(Question).filter(Question.id == r.question_id).first()
            if not question:
                continue
            result.append({
                "question_id": question.id,
                "text": question.text,
                "passage": question.passage,
                "choice_a": question.choice_a,
                "choice_b": question.choice_b,
                "choice_c": question.choice_c,
                "choice_d": question.choice_d,
                "correct_answer": question.correct_answer,
                "subject": question.subject,
                "difficulty": question.difficulty
            })

        return jsonify(result)
    finally:
        db.close()
