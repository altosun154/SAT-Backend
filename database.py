import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text, Boolean, UniqueConstraint, LargeBinary, Float
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///sat.db")

# Render gives postgres:// but SQLAlchemy requires postgresql://. Pin the driver
# explicitly to psycopg2 (the one in requirements.txt) rather than leaving it to
# SQLAlchemy's default resolution — that default changed between 2.0.x and 2.1.x
# to prefer the psycopg (v3) package, which isn't installed, and broke the build.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# pool_pre_ping: Render drops idle Postgres connections, so the first query after
# the service wakes would otherwise fail with a stale pooled connection (HTTP 500).
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=False)
    email = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), default="student")  # "student", "admin", or "parent"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    totp_secret = Column(String(32), nullable=True)
    totp_enabled = Column(Boolean, default=False)


class Test(Base):
    __tablename__ = "tests"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # False while a test is incomplete (e.g. its adaptive Module 2 hasn't been built yet);
    # unpublished tests are hidden from students and from the assignment picker.
    is_published = Column(Boolean, default=True, nullable=True)


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=True)
    text = Column(Text, nullable=False)
    choice_a = Column(Text, nullable=False)
    choice_b = Column(Text, nullable=False)
    choice_c = Column(Text, nullable=False)
    choice_d = Column(Text, nullable=False)
    correct_answer = Column(String(10), nullable=False)  # "A", "B", "C", "D", or free response
    subject = Column(String(50))    # "Reading", "Grammar", "Math (No Calculator)", "Math (Calculator)"
    difficulty = Column(String(20))           # "easy", "medium", "hard" — per-question difficulty
    module_variant = Column(String(20), nullable=True)  # "easy" or "hard" — which adaptive module 2 set this belongs to
    passage = Column(Text, nullable=True)  # reading passage for Reading questions
    image_url = Column(String(500), nullable=True)  # URL to graph/image for visual questions
    skill = Column(String(100), nullable=True)  # e.g. "Algebra", "Reading Comprehension", "Grammar & Usage"
    explanation = Column(Text, nullable=True)
    parse_draft_id = Column(String(36), nullable=True)
    archived = Column(Boolean, default=False, nullable=False)
    question_type = Column(String(20), default="mcq", nullable=True)  # "mcq" or "free_response" (typed answer)
    source_key = Column(String(40), nullable=True, index=True)  # e.g. "T03-M-M1-Q05" (PDF test 3, Math, Module 1, Q5)
    pool_question_id = Column(Integer, nullable=True, index=True)  # set when copied from the Module 2 pool


class PoolQuestion(Base):
    """Module 2 question pool. Every Module 2 question from the source tests lives here once.
    Lower/Higher Module 2s are built from it (see adaptive.py / module2_builder.py) and the
    picked questions are copied into `questions` for that test.
    used_in_test_id is empty while a question is unused."""
    __tablename__ = "module2_pool_questions"

    id = Column(Integer, primary_key=True, index=True)
    source_key = Column(String(40), unique=True, nullable=False)   # e.g. "T03-RW-M2-Q12"
    source_label = Column(String(40), nullable=True)                # e.g. "T03" (which PDF it came from)
    source_position = Column(Integer, nullable=True)                # question number in that PDF's Module 2
    section = Column(String(20), nullable=False)                    # "reading_writing" or "math"
    difficulty = Column(String(20), nullable=False)                 # "easy", "medium", "hard"
    question_type = Column(String(20), default="mcq")
    passage = Column(Text, nullable=True)
    text = Column(Text, nullable=False)
    choice_a = Column(Text, nullable=True)
    choice_b = Column(Text, nullable=True)
    choice_c = Column(Text, nullable=True)
    choice_d = Column(Text, nullable=True)
    correct_answer = Column(String(50), nullable=False)
    explanation = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)
    skill = Column(String(100), nullable=True)
    used_in_test_id = Column(Integer, ForeignKey("tests.id"), nullable=True, index=True)
    archived = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TestQuestion(Base):
    """Questions for actual test-taking (Module 1 / Module 2), separate from the
    `questions` table which holds question-bank/practice content. Not yet wired
    into any live route — added ahead of the adaptive Module 2 routing logic
    that will query it once this table has real data."""
    __tablename__ = "test_questions"

    id = Column(Integer, primary_key=True, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    section = Column(String(20), nullable=False)          # "math" | "reading_writing"
    module = Column(Integer, nullable=False)              # 1 or 2
    module2_variant = Column(String(10), nullable=True)   # "easy" | "hard" — null for module 1
    difficulty = Column(String(20), nullable=True)        # "easy", "medium", "hard"
    order = Column(Integer, nullable=True)                # position within the module
    text = Column(Text, nullable=False)
    choice_a = Column(Text, nullable=False)
    choice_b = Column(Text, nullable=False)
    choice_c = Column(Text, nullable=False)
    choice_d = Column(Text, nullable=False)
    correct_answer = Column(String(10), nullable=False)
    explanation = Column(Text, nullable=True)
    image_url = Column(String(500), nullable=True)


class ParentStudent(Base):
    __tablename__ = "parent_students"

    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    linked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("parent_id", "student_id", name="uq_parent_student"),)


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    assigned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    due_date = Column(DateTime, nullable=True)


class TestUnlock(Base):
    """A test a student is allowed to take on their own (without an assignment)."""
    __tablename__ = "test_unlocks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False, index=True)
    unlocked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("user_id", "test_id", name="uq_test_unlock"),)


class Response(Base):
    __tablename__ = "user_responses"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    test_id = Column(Integer, index=True)
    question_id = Column(Integer, index=True)
    selected_answer = Column(String(500))
    is_correct = Column(Boolean, nullable=True)
    time_spent_seconds = Column(Integer, default=0)
    answered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    session_id = Column(String(100), nullable=True, index=True)


class TestCompletion(Base):
    __tablename__ = "test_completions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    test_id = Column(Integer, index=True)
    completed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    session_id = Column(String(100), nullable=True, index=True)


class PracticeResponse(Base):
    __tablename__ = "practice_responses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    question_id = Column(Integer, nullable=True)
    topic = Column(String(100), nullable=True)
    is_correct = Column(Boolean, nullable=True)
    answered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Activity(Base):
    __tablename__ = "activity"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    date = Column(String(10), nullable=False)   # YYYY-MM-DD
    type = Column(String(20), nullable=False, default="login")

    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_activity_user_date"),)


class ParseDraft(Base):
    __tablename__ = "parse_drafts"

    id = Column(String(36), primary_key=True)           # UUID
    file_hash = Column(String(64), nullable=False, index=True)
    original_filename = Column(String(500), nullable=True)
    status = Column(String(20), default="pending_review")  # pending_review | committed | abandoned
    result_json = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)
    error_text = Column(Text, nullable=True)
    file_blob = Column(LargeBinary, nullable=True)  # stored for retry
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TestDraft(Base):
    """An imported practice test awaiting admin review (Upload Test flow). Kept
    separate from ParseDraft, which belongs to the question-bank importer.
    Publishing turns it into Test + Question rows and deletes the draft."""
    __tablename__ = "test_drafts"

    id = Column(String(36), primary_key=True)  # UUID
    test_name = Column(String(200), nullable=False)
    original_filename = Column(String(500), nullable=True)
    questions_json = Column(Text, nullable=False)  # list of parse_test_file question dicts
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TestScore(Base):
    """IRT-computed section and total scores for a completed test session."""
    __tablename__ = "test_scores"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False, index=True)
    session_id = Column(String(100), nullable=False, unique=True, index=True)
    rw_score = Column(Integer, nullable=True)
    math_score = Column(Integer, nullable=True)
    total_score = Column(Integer, nullable=True)
    rw_band = Column(String(30), nullable=True)
    math_band = Column(String(30), nullable=True)
    rw_theta = Column(Float, nullable=True)
    math_theta = Column(Float, nullable=True)
    rw_route = Column(String(10), nullable=True)    # "easy" | "hard"
    math_route = Column(String(10), nullable=True)
    param_version = Column(String(100), nullable=True)
    computed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ItemParams(Base):
    """Calibrated 3PL IRT parameters for each item key."""
    __tablename__ = "item_params"

    id = Column(Integer, primary_key=True, index=True)
    item_key = Column(String(40), nullable=False, unique=True, index=True)
    a = Column(Float, nullable=False, default=1.0)
    b = Column(Float, nullable=False, default=0.0)
    c = Column(Float, nullable=False, default=0.25)
    n_responses = Column(Integer, nullable=True)
    version_id = Column(Integer, ForeignKey("item_param_versions.id"), nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ItemParamVersion(Base):
    """Snapshot of item parameters saved after each successful calibration."""
    __tablename__ = "item_param_versions"

    id = Column(Integer, primary_key=True, index=True)
    version_label = Column(String(100), nullable=False)
    params_json = Column(Text, nullable=False)   # JSON {item_key: {a, b, c}}
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CalibrationRun(Base):
    """One auto-calibration run record."""
    __tablename__ = "calibration_runs"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(String(20), nullable=False, default="running")  # running | success | failed
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    message = Column(Text, nullable=True)
    items_updated = Column(Integer, nullable=True)
    version_id = Column(Integer, ForeignKey("item_param_versions.id"), nullable=True)


class IrtConfig(Base):
    """Single-row config table for IRT feature flags."""
    __tablename__ = "irt_config"

    id = Column(Integer, primary_key=True)
    auto_calibrate_enabled = Column(Boolean, nullable=False, default=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()