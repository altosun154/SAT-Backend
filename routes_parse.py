import uuid
import json
import os
import hashlib
import traceback as tb_module
from flask import Blueprint, request, jsonify
from database import SessionLocal, Question, ParseDraft
from admin import require_admin
from parsing.pipeline import run as run_pipeline
from parsing.validate import validate

parse_bp = Blueprint('parse', __name__, url_prefix='/api/parse')

ALLOWED_EXTENSIONS = ('.pdf', '.docx')
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


@parse_bp.route('', methods=['POST'])
@require_admin
def create_parse():
    """Upload a PDF or DOCX, run the parser, save as a draft for review."""
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'file is required (multipart/form-data, field name "file")'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'error': 'Only .pdf and .docx files are supported'}), 400

    file_bytes = file.read()

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return jsonify({'error': f'File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.'}), 400

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # B2: catch parse errors and save a failed draft instead of 500ing
    try:
        result = run_pipeline(file_bytes, file.filename)
    except Exception as e:
        print(tb_module.format_exc())  # full traceback server-side only
        draft_id = str(uuid.uuid4())
        db = SessionLocal()
        try:
            db.add(ParseDraft(
                id=draft_id,
                file_hash=file_hash,
                original_filename=file.filename,
                status='failed',
                result_json='{}',
                error_text=str(e),
                file_blob=file_bytes,
            ))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        return jsonify({'status': 'failed', 'draft_id': draft_id, 'error': str(e)}), 200

    db = SessionLocal()
    try:
        # B8: flag duplicates but don't block
        existing = db.query(ParseDraft).filter(
            ParseDraft.file_hash == file_hash,
            ParseDraft.status == 'committed',
        ).first()
        if existing:
            result['duplicate_of'] = existing.id

        draft_id = str(uuid.uuid4())
        result['parse_id'] = draft_id

        db.add(ParseDraft(
            id=draft_id,
            file_hash=file_hash,
            original_filename=file.filename,
            status='pending_review',
            result_json=json.dumps(result),
            file_blob=file_bytes,
        ))
        db.commit()
        return jsonify(result), 201
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'Database error: {str(e)}', 'detail': tb_module.format_exc()}), 500
    finally:
        db.close()


@parse_bp.route('', methods=['GET'])
@require_admin
def list_parses():
    """List all committed and failed question bank drafts (pending_review drafts
    aren't included — they're only reachable via the parse_id the create/retry
    response returns, not by browsing this list)."""
    db = SessionLocal()
    try:
        drafts = db.query(ParseDraft).filter(
            ParseDraft.status.in_(['committed', 'failed'])
        ).order_by(ParseDraft.created_at.desc()).all()
        results = []
        for d in drafts:
            try:
                data = json.loads(d.result_json)
            except Exception:
                data = {}
            meta = data.get('meta', {})
            questions = data.get('questions', [])
            warnings = data.get('warnings', [])
            blocking = [w for w in warnings if w.get('severity') == 'blocking']
            results.append({
                'id': d.id,
                'name': meta.get('name') or d.original_filename or 'Unknown',
                'filename': d.original_filename or 'Unknown',
                'subject': meta.get('subject', ''),
                'topic': meta.get('topic', ''),
                'skill': meta.get('skill', ''),
                'question_count': len(questions),
                'warning_count': len(warnings),
                'blocking_count': len(blocking),
                'created_at': d.created_at.isoformat() if d.created_at else None,
                'notes': d.notes or '',
                'status': d.status,
                'error': d.error_text or '',
            })
        return jsonify(results), 200
    finally:
        db.close()


@parse_bp.route('/<draft_id>', methods=['DELETE'])
@require_admin
def delete_parse(draft_id):
    """Delete a committed question bank and all its questions."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404

        try:
            data = json.loads(draft.result_json)
        except Exception:
            data = {}

        deleted_count = db.query(Question).filter(Question.parse_draft_id == draft_id).delete()
        db.delete(draft)
        db.commit()
        return jsonify({'success': True, 'questions_deleted': deleted_count}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@parse_bp.route('/<draft_id>/notes', methods=['PATCH'])
@require_admin
def update_notes(draft_id):
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404
        draft.notes = request.get_json().get('notes', '')
        db.commit()
        return jsonify({'success': True}), 200
    finally:
        db.close()


@parse_bp.route('/<draft_id>', methods=['GET'])
@require_admin
def get_parse(draft_id):
    """Retrieve a saved draft by ID."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404
        return jsonify(json.loads(draft.result_json)), 200
    finally:
        db.close()


@parse_bp.route('/<draft_id>', methods=['PUT'])
@require_admin
def update_parse(draft_id):
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404

        data = request.get_json()
        if not data:
            return jsonify({'error': 'JSON body required'}), 400

        draft.result_json = json.dumps(data)

        if draft.status == 'committed':
            meta = data.get('meta', {})
            questions = data.get('questions', [])
            db.query(Question).filter(Question.parse_draft_id == draft_id).delete()
            for q in questions:
                db.add(Question(
                    text=q.get('text', ''),
                    choice_a=q.get('choice_a', ''),
                    choice_b=q.get('choice_b', ''),
                    choice_c=q.get('choice_c', ''),
                    choice_d=q.get('choice_d', ''),
                    correct_answer=q.get('correct_answer', ''),
                    subject=meta.get('subject') or None,
                    difficulty=q.get('difficulty') or None,
                    skill=meta.get('skill') or meta.get('topic') or None,
                    explanation=q.get('explanation') or None,
                    test_id=None,
                    module_variant=None,
                    passage=None,
                    image_url=q.get('image_url') or None,
                    parse_draft_id=draft_id,
                ))

        db.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@parse_bp.route('/<draft_id>/commit', methods=['POST'])
@require_admin
def commit_parse(draft_id):
    """Validate and write the reviewed questions to the questions table."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404
        if draft.status == 'committed':
            return jsonify({'error': 'Already committed'}), 409

        # Accept an edited payload from the request body, or fall back to the saved draft
        data = request.get_json() or json.loads(draft.result_json)
        questions = data.get('questions', [])
        meta = data.get('meta', {})

        if not questions:
            return jsonify({'error': 'No questions to commit'}), 422

        # Re-validate server-side so the frontend can't skip it
        explanations = {
            q.get('number'): q.get('explanation')
            for q in questions
            if q.get('explanation') and q.get('number') is not None
        }
        warnings = validate(questions, explanations)
        blocking = [w for w in warnings if w['severity'] == 'blocking']
        if blocking:
            return jsonify({'error': 'Blocking validation errors must be fixed first', 'warnings': blocking}), 422

        # Recommitting (retry -> commit again on a draft that was already committed)
        # must replace the old question rows, not add a second copy alongside them.
        db.query(Question).filter(Question.parse_draft_id == draft_id).delete()

        added = 0
        for q in questions:
            db.add(Question(
                text=q.get('text', ''),
                choice_a=q.get('choice_a', ''),
                choice_b=q.get('choice_b', ''),
                choice_c=q.get('choice_c', ''),
                choice_d=q.get('choice_d', ''),
                correct_answer=q.get('correct_answer', ''),
                subject=meta.get('subject') or None,
                difficulty=q.get('difficulty') or None,
                skill=meta.get('skill') or meta.get('topic') or None,
                explanation=q.get('explanation') or None,
                test_id=None,
                module_variant=None,
                passage=None,
                image_url=q.get('image_url') or None,
                parse_draft_id=draft_id,
            ))
            added += 1

        draft.status = 'committed'
        db.commit()
        return jsonify({'success': True, 'questions_added': added}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@parse_bp.route('/<draft_id>/retry', methods=['POST'])
@require_admin
def retry_parse(draft_id):
    """Re-run the pipeline on the stored file for a failed or already-committed
    draft — e.g. after a parser fix, to pick up data (like image_url) the
    original parse run couldn't extract. Resets status to pending_review;
    committing again re-syncs the questions table with the refreshed result."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Draft not found'}), 404
        if draft.status not in ('failed', 'committed'):
            return jsonify({'error': 'Only failed or committed drafts can be retried'}), 409
        if not draft.file_blob:
            return jsonify({'error': 'No stored file — please upload the file again'}), 422

        file_bytes = draft.file_blob
        filename = draft.original_filename or 'upload.docx'

        try:
            result = run_pipeline(file_bytes, filename)
        except Exception as e:
            print(tb_module.format_exc())
            draft.error_text = str(e)
            db.commit()
            return jsonify({'status': 'failed', 'draft_id': draft_id, 'error': str(e)}), 200

        result['parse_id'] = draft_id
        draft.status = 'pending_review'
        draft.result_json = json.dumps(result)
        draft.error_text = None
        db.commit()
        return jsonify(result), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
