"""Local check of the adaptive import + Module 2 flow. Uses a throwaway SQLite file,
never your real database, and doesn't upload images.

    python smoke_test_adaptive.py

Prints PASS/FAIL for each check.
"""
import os
import sys
import tempfile

DB_FILE = os.path.join(tempfile.mkdtemp(), "smoke.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE}"      # must be set before importing database.py
os.environ.pop("SUPABASE_SERVICE_KEY", None)

from database import Base, engine, SessionLocal, Test, Question, PoolQuestion, Response  # noqa: E402
import import_practice_tests as imp  # noqa: E402
import module2_builder  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


# SQLite can't run init_db's Postgres-only ALTER COLUMN, so create tables directly,
# and fake the image upload.
imp.ensure_schema = lambda: Base.metadata.create_all(bind=engine)
imp.upload_images = lambda files: {f: f"https://example.test/question-images/practice-tests/{f}" for f in files}

sys.argv = ["import_practice_tests.py"]
imp.main()

db = SessionLocal()
tests = db.query(Test).order_by(Test.id).all()
published = [t for t in tests if t.is_published]
check("10 tests created", len(tests) == 10, f"{len(tests)}")
check("3 tests published (enough hard Math for 3)", len(published) == 3, f"{[t.title for t in published]}")
check("490 pool questions", db.query(PoolQuestion).count() == 490)
used = db.query(PoolQuestion).filter(PoolQuestion.used_in_test_id.isnot(None)).all()
check("no pool question used by two tests", len(used) == len({p.id for p in used}))
check("images linked", db.query(Question).filter(Question.image_url.isnot(None)).count() > 0)

# running the import again must not duplicate anything
before = db.query(Question).count()
db.close()
imp.main()
db = SessionLocal()
check("re-running the import adds nothing", db.query(Question).count() == before)

t = published[0]
for subject, n in [("Section 1, Module 1: Reading and Writing", 27), ("Section 2, Module 1: Math", 22)]:
    c = db.query(Question).filter(Question.test_id == t.id, Question.subject == subject).count()
    check(f"{subject}: {n} questions", c == n, str(c))
for subject, variant, mix in [("Section 1, Module 2: Reading and Writing", "easy", (12, 11, 4)),
                              ("Section 1, Module 2: Reading and Writing", "hard", (4, 11, 12)),
                              ("Section 2, Module 2: Math", "easy", (10, 9, 3)),
                              ("Section 2, Module 2: Math", "hard", (3, 9, 10))]:
    qs = db.query(Question).filter(Question.test_id == t.id, Question.subject == subject,
                                   Question.module_variant == variant).all()
    got = tuple(sum(1 for q in qs if q.difficulty == d) for d in ("easy", "medium", "hard"))
    check(f"{subject} [{variant}] mix {mix}", got == mix, str(got))

rw = [(q.id, q.correct_answer) for q in db.query(Question).filter(
    Question.test_id == t.id, Question.subject == "Section 1, Module 1: Reading and Writing").order_by(Question.id)]
fr = db.query(Question).filter(Question.test_id == t.id, Question.question_type == "free_response").first()
fr = (fr.id, fr.correct_answer) if fr else None
published_ids = sorted(x.id for x in published)
test_id = t.id
db.close()   # SQLite: no open read transaction while the routes write

# HTTP flow with the real blueprints
import jwt as _jwt  # noqa: E402
from flask import Flask  # noqa: E402
from routes_questions import questions_bp  # noqa: E402
from routes_responses import responses_bp  # noqa: E402
from routes_results import results_bp  # noqa: E402
from routes_adaptive import adaptive_bp  # noqa: E402
from routes_irt import irt_bp  # noqa: E402

# Insert a smoke-test student so require_login is satisfied
_SMOKE_USER_ID = 999
db = SessionLocal()
from database import User  # noqa: E402
from database import TestUnlock  # noqa: E402
_su = db.query(User).filter_by(id=_SMOKE_USER_ID).first()
if not _su:
    import hashlib
    db.add(User(id=_SMOKE_USER_ID, username="smoke", email="smoke@test.local",
                password_hash=hashlib.sha256(b"x").hexdigest(), role="student"))
    db.flush()
# Unlock all published tests for the smoke user
for _t in db.query(Test).filter(Test.is_published == True).all():
    if not db.query(TestUnlock).filter_by(user_id=_SMOKE_USER_ID, test_id=_t.id).first():
        db.add(TestUnlock(user_id=_SMOKE_USER_ID, test_id=_t.id))
db.commit()
db.close()

_SMOKE_TOKEN = _jwt.encode({"user_id": _SMOKE_USER_ID, "role": "student"}, "dev-secret-key", algorithm="HS256")
_AUTH = {"Authorization": f"Bearer {_SMOKE_TOKEN}"}

app = Flask(__name__)
for bp in (questions_bp, responses_bp, results_bp, adaptive_bp, irt_bp):
    app.register_blueprint(bp)
client = app.test_client()

listed = client.get("/tests", headers=_AUTH).get_json()
check("/tests lists only the published tests", sorted(x["id"] for x in listed) == published_ids)
m1 = client.get(f"/tests/{test_id}/questions?variant=none", headers=_AUTH).get_json()
check("?variant=none returns Module 1 only (49)", len(m1) == 49, str(len(m1)))
check("answers are not sent to students", all("correct_answer" not in q for q in m1))

for n_right, expect in [(19, "hard"), (18, "easy")]:
    answers = [{"question_id": qid, "selected_answer": key if i < n_right else "Z"}
               for i, (qid, key) in enumerate(rw)]
    r = client.post(f"/tests/{test_id}/module2", json={"section": "reading_writing", "answers": answers}, headers=_AUTH).get_json()
    check(f"R&W {n_right}/27 correct -> {expect}", r["module2_variant"] == expect and len(r["questions"]) == 27,
          f"{r['module2_variant']}, {len(r['questions'])} questions")

# ---- IRT: final submit should create a TestScore row ----
db = SessionLocal()
math_qs = db.query(Question).filter(
    Question.test_id == test_id,
    Question.subject == "Section 2, Module 1: Math",
).order_by(Question.id).all()
rw_qs = db.query(Question).filter(
    Question.test_id == test_id,
    Question.subject == "Section 1, Module 1: Reading and Writing",
).order_by(Question.id).all()
db.close()

irt_session = "smoke-irt"
irt_answers = [
    {"question_id": q.id, "selected_answer": q.correct_answer}
    for q in math_qs + rw_qs
]
submit_resp = client.post("/submit", json={
    "test_id": test_id,
    "user_id": 99,
    "session_id": irt_session,
    "answers": irt_answers,
    "final": True,
}).get_json()
check("final submit returns success", submit_resp and submit_resp.get("success") is True)

from database import TestScore  # noqa: E402
db = SessionLocal()
ts = db.query(TestScore).filter_by(session_id=irt_session).first()
check("final submit creates TestScore row", ts is not None)
if ts:
    check("TestScore has a total_score", ts.total_score is not None,
          f"total={ts.total_score}, rw={ts.rw_score}, math={ts.math_score}")
    check("total_score in 400-1600 range",
          ts.total_score is None or (400 <= ts.total_score <= 1600),
          str(ts.total_score))
    check("band labels present", ts.rw_band is not None or ts.math_band is not None,
          f"rw_band={ts.rw_band}, math_band={ts.math_band}")
db.close()

# /results should return IRT fields
results_resp = client.get(
    f"/results?user_id=99&test_id={test_id}&session_id={irt_session}"
).get_json()
check("/results returns math_band field", "math_band" in results_resp,
      str(results_resp.keys() if results_resp else "no response"))

# Admin IRT status endpoint (no auth in smoke test — expect 401)
status_resp = client.get("/admin/irt/status")
check("/admin/irt/status returns 401 without token", status_resp.status_code == 401)

# ---- typed math answer grading ----
if fr:
    ans = fr[1].split("|")[0]
    client.post("/submit", json={"test_id": test_id, "user_id": 1, "session_id": "smoke",
                                 "answers": [{"question_id": fr[0], "selected_answer": f" {ans} "}]})
    db = SessionLocal()
    saved = db.query(Response).filter(Response.session_id == "smoke").first()
    check("typed math answer graded correct by /submit", saved is not None and saved.is_correct is True,
          f"key {fr[1]!r}")
    db.close()

# release gives questions back to the pool
db = SessionLocal()
freed = module2_builder.release_module2(db, test_id, force=True)
check("release_module2 returns questions to the pool",
      freed > 0 and db.query(PoolQuestion).filter(PoolQuestion.used_in_test_id == test_id).count() == 0)
db.close()

print("\nALL CHECKS PASSED" if not failures else f"\n{len(failures)} CHECK(S) FAILED: {failures}")
sys.exit(1 if failures else 0)
