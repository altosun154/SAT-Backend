import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text, Boolean, UniqueConstraint, LargeBinary
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///sat.db")

# Render gives postgres:// but SQLAlchemy requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()