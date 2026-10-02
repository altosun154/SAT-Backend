"""Builds each test's Lower/Higher Module 2 from the Module 2 pool (database side).

  build_module2(db, test)   pick unused pool questions for one test, copy them into `questions`
                            (module_variant "easy" = Lower, "hard" = Higher), mark them used,
                            publish the test. All-or-nothing: raises NotEnoughQuestions and
                            leaves the database untouched if the pool is too small.
  build_pending(db)         do that for every unpublished test that has its Module 1 but no Module 2.
  release_module2(db, id)   remove a test's Module 2 and give its questions back to the pool.
  pool_summary(db)          used / unused counts by section and difficulty.
"""
from database import Question, PoolQuestion, Test, Response
from adaptive import SECTIONS, DIFFICULTIES, VARIANTS, NotEnoughQuestions, choose_module2


def _module1_questions(db, test_id, section):
    return (db.query(Question)
            .filter(Question.test_id == test_id,
                    Question.subject == SECTIONS[section]["module1_subject"],
                    Question.module_variant.is_(None))
            .order_by(Question.id).all())


def has_module2(db, test_id):
    return db.query(Question).filter(Question.test_id == test_id,
                                     Question.module_variant.isnot(None)).first() is not None


def _source_label(db, test_id):
    """Which PDF a test's Module 1 came from, e.g. 'T03' from source_key 'T03-RW-M1-Q01'."""
    q = (db.query(Question).filter(Question.test_id == test_id, Question.source_key.isnot(None))
         .order_by(Question.id).first())
    return q.source_key.split("-")[0] if q and q.source_key else None


def build_module2(db, test):
    """Build Lower + Higher Module 2 for both sections of `test`. Commits on success."""
    for section, cfg in SECTIONS.items():
        if len(_module1_questions(db, test.id, section)) == 0:
            raise ValueError(f"Test {test.id} has no {cfg['module1_subject']} questions")
    if has_module2(db, test.id):
        raise ValueError(f"Test {test.id} already has a Module 2 (use release_module2 first)")

    prefer = _source_label(db, test.id)
    plan = {}
    for section in SECTIONS:                      # decide everything before writing anything
        pool = (db.query(PoolQuestion)
                .filter(PoolQuestion.section == section,
                        PoolQuestion.used_in_test_id.is_(None),
                        PoolQuestion.archived.is_(False))
                .all())
        plan[section] = choose_module2(pool, section, prefer_source=prefer)

    try:
        for section, variants in plan.items():
            subject = SECTIONS[section]["module2_subject"]
            for variant in VARIANTS:                     # insertion order = display order
                for p in variants[variant]:
                    db.add(Question(
                        test_id=test.id, subject=subject, module_variant=variant,
                        text=p.text, passage=p.passage,
                        choice_a=p.choice_a or "", choice_b=p.choice_b or "",
                        choice_c=p.choice_c or "", choice_d=p.choice_d or "",
                        correct_answer=p.correct_answer, explanation=p.explanation,
                        difficulty=p.difficulty, image_url=p.image_url, skill=p.skill,
                        question_type=p.question_type or "mcq",
                        source_key=p.source_key, pool_question_id=p.id,
                    ))
                    p.used_in_test_id = test.id
        test.is_published = True
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {s: {v: len(plan[s][v]) for v in VARIANTS} for s in plan}


def build_pending(db):
    """Build Module 2 for every unpublished test that has Module 1 but no Module 2, in id order.
    Returns a list of (test_id, title, message)."""
    results = []
    tests = db.query(Test).filter(Test.is_published.is_(False)).order_by(Test.id).all()
    for t in tests:
        if has_module2(db, t.id) or not all(_module1_questions(db, t.id, s) for s in SECTIONS):
            continue
        try:
            build_module2(db, t)
            results.append((t.id, t.title, "READY"))
        except NotEnoughQuestions as e:
            results.append((t.id, t.title, f"NOT BUILT - {e}"))
    return results


def release_module2(db, test_id, force=False):
    """Delete a test's pool-built Module 2 and return those questions to the pool.
    Refuses if students already answered them, unless force=True."""
    rows = db.query(Question).filter(Question.test_id == test_id,
                                     Question.pool_question_id.isnot(None)).all()
    ids = [q.id for q in rows]
    if ids and not force and db.query(Response).filter(Response.question_id.in_(ids)).first():
        raise ValueError("Students have already answered this Module 2; pass force=True to release anyway")
    for q in rows:
        db.delete(q)
    db.query(PoolQuestion).filter(PoolQuestion.used_in_test_id == test_id).update(
        {PoolQuestion.used_in_test_id: None}, synchronize_session=False)
    test = db.query(Test).filter(Test.id == test_id).first()
    if test and rows:
        test.is_published = False
    db.commit()
    return len(rows)


def pool_summary(db):
    out = []
    for section in SECTIONS:
        for d in DIFFICULTIES:
            base = db.query(PoolQuestion).filter(PoolQuestion.section == section,
                                                 PoolQuestion.difficulty == d,
                                                 PoolQuestion.archived.is_(False))
            used = base.filter(PoolQuestion.used_in_test_id.isnot(None)).count()
            total = base.count()
            out.append({"section": section, "difficulty": d, "used": used,
                        "unused": total - used, "total": total})
    return out
