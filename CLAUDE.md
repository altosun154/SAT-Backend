# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the dev server (Flask debug mode)
python app.py

# Initialize the database schema
python init_db.py

# Seed questions for the default SAT Practice Test 11
python seed_questions.py

# Add a new test from a JSON file
python add_test.py <path_to_json>

# Seed a specific SAT test (separate script)
python seed_sat_test.py
```

There is no test suite in this project. Manual testing is done against the running Flask server.

## Architecture

This is a flat-file Flask backend — all Python modules live at the root, with no subdirectory package structure.

**Entry point**: [app.py](app.py) creates the Flask app, registers four blueprints, and calls `init_db()` + `seed()` on startup (so the DB and seed data are always in sync when the server starts).

**Database**: [database.py](database.py) defines all SQLAlchemy models and the `SessionLocal` factory. The app uses SQLite locally (`sat.db`) and switches to PostgreSQL on Render via the `DATABASE_URL` env var. The `postgres://` → `postgresql://` prefix fix is handled there. All route handlers open and close a `SessionLocal()` session manually in a try/finally block — there is no dependency injection or context manager pattern.

**Models and their relationships**:
- `User` — roles: `"student"`, `"admin"`, `"parent"`, `"deactivated"`
- `Test` → `Question` (one-to-many via `question.test_id`)
- `Question` — has `module_variant` (`"easy"` or `"hard"`) for adaptive Module 2 routing; `subject` encodes the full section name (e.g. `"Section 2, Module 1: Math"`)
- `Response` — records every answer attempt; `session_id` ties a test-taking session together
- `TestCompletion` — written once per finished test session (triggers score calculation on read)
- `PracticeResponse` — for topic-drill activity (separate from full test `Response`)
- `Assignment` — links a `User` to a `Test` with optional `due_date`
- `ParentStudent` — links parent users to student users
- `Activity` — stores login/activity dates (YYYY-MM-DD) for the calendar heatmap; unique per user+date

**Blueprints**:
- `routes.bp` (no prefix) — questions, tests, submit, adaptive routing, results, assignments, parent views, practice activity
- `auth.auth_bp` (`/auth`) — `/register` and `/login`; JWT tokens (HS256, 7-day expiry) signed with `SECRET_KEY` env var
- `admin.admin_bp` (`/admin`) — admin-only endpoints guarded by `require_admin` decorator that validates a Bearer JWT and checks `user.role == "admin"`; user management, assignment CRUD
- `activity.activity_bp` (no prefix) — `POST /activity` for logging login dates; requires Bearer JWT

**Adaptive testing flow**: The frontend takes Module 1, calls `POST /adaptive/module2` with the score, and receives `"easy"` or `"hard"`. It then fetches `GET /tests/<id>/questions?variant=easy|hard` for Module 2. Threshold is 60% correct → hard.

**Scoring**: Math and R&W scores are each scaled 200–800 using `round(200 + (correct/total) * 600)`. Total score is their sum (400–1600). This calculation is inline in multiple route handlers (`routes.py` and `admin.py`) rather than centralized — if the formula needs to change, update it in both files.

**Question images**: Stored in Supabase (`rgtpylhsewekyepcgxde.supabase.co/storage/v1/object/public/question-images`). The `BASE_URL` constant in `seed_questions.py` constructs `image_url` values. `rename_images.py` and `sync_images.py` are maintenance scripts for that bucket.

**Adding a new test**: Create a JSON file with `test_name` and a `questions` array (each question has the same fields as the `Question` model), then run `python add_test.py <file>`. The `sat_questions.json` file is an example of this format.

## Branching

- `main` — production only; do not push directly
- `dev` — active development; PRs merge here first
- Feature branches: `feature/short-description` or `fix/short-description`

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | Database URL (defaults to `sqlite:///sat.db`) |
| `SECRET_KEY` | JWT signing secret (defaults to `"dev-secret-key"`) |
| `SUPABASE_SERVICE_KEY` | Service-role API key for uploading images extracted during `/api/parse` imports to the Supabase `question-images` bucket ([supabase_storage.py](supabase_storage.py)). Without it, image extraction is silently skipped — imports still succeed, just without images. |
| `SUPABASE_PROJECT_REF` | Supabase project ref for the storage bucket (defaults to `rgtpylhsewekyepcgxde`, the existing bucket referenced elsewhere in this repo) |

## CORS

Allowed origins are hardcoded in [app.py](app.py): `localhost:5500`, `127.0.0.1:5500`, and the Render production frontend URL. Add new origins there when needed.
