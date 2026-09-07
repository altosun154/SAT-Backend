import json
from flask import Blueprint, request, jsonify
from database import SessionLocal, Question, ParseDraft
from admin import require_admin

imports_bp = Blueprint('imports', __name__, url_prefix='/api/imports')

_EDITABLE_META = ('subject', 'topic', 'skill')


def _draft_row(d, data):
    meta = data.get('meta', {})
    questions = data.get('questions', [])
    warnings = data.get('warnings', [])
    return {
        'id': d.id,
        'kind': 'batch' if d.status == 'committed' else 'draft',
        'status': d.status,
        'filename': d.original_filename or 'Unknown',
        'subject': meta.get('subject', ''),
        'topic': meta.get('topic', ''),
        'skill': meta.get('skill', ''),
        'question_count': len(questions),
        'warning_count': len(warnings),
        'notes': d.notes or '',
        'created_at': d.created_at.isoformat() if d.created_at else None,
        'error': d.error_text or None,
    }


def _question_dict(q):
    return {
        'id': q.id,
        'text': q.text,
        'choice_a': q.choice_a,
        'choice_b': q.choice_b,
        'choice_c': q.choice_c,
        'choice_d': q.choice_d,
        'correct_answer': q.correct_answer,
        'explanation': q.explanation,
        'difficulty': q.difficulty,
        'subject': q.subject,
        'skill': q.skill,
    }


@imports_bp.route('', methods=['GET'])
@require_admin
def list_imports():
    """Unified list of all drafts and committed batches, newest first."""
    db = SessionLocal()
    try:
        drafts = db.query(ParseDraft).order_by(ParseDraft.created_at.desc()).all()
        results = []
        for d in drafts:
            try:
                data = json.loads(d.result_json) if d.result_json else {}
            except Exception:
                data = {}
            results.append(_draft_row(d, data))
        return jsonify(results), 200
    finally:
        db.close()


@imports_bp.route('/<draft_id>', methods=['GET'])
@require_admin
def get_import(draft_id):
    """Detail for any draft. Committed batches return live questions from the DB."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Not found'}), 404
        try:
            data = json.loads(draft.result_json) if draft.result_json else {}
        except Exception:
            data = {}
        if draft.status == 'committed':
            qs = db.query(Question).filter(
                Question.parse_draft_id == draft_id,
                Question.archived == False,
            ).all()
            data['questions'] = [_question_dict(q) for q in qs]
        return jsonify(data), 200
    finally:
        db.close()


@imports_bp.route('/<draft_id>', methods=['PATCH'])
@require_admin
def update_import_meta(draft_id):
    """Edit batch metadata (subject, topic, skill, notes). Syncs to live question rows."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Not found'}), 404
        body = request.get_json() or {}

        # Update stored JSON meta
        try:
            data = json.loads(draft.result_json) if draft.result_json else {}
        except Exception:
            data = {}
        meta = data.get('meta', {})
        for field in _EDITABLE_META:
            if field in body:
                meta[field] = body[field]
        data['meta'] = meta
        draft.result_json = json.dumps(data)

        if 'notes' in body:
            draft.notes = body['notes']

        # Sync subject/skill to live question rows if committed
        if draft.status == 'committed':
            qs = db.query(Question).filter(Question.parse_draft_id == draft_id).all()
            for q in qs:
                if 'subject' in body:
                    q.subject = body['subject'] or None
                if 'skill' in body:
                    q.skill = body['skill'] or None

        db.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@imports_bp.route('/<draft_id>', methods=['DELETE'])
@require_admin
def delete_import(draft_id):
    """Soft-archive all questions from this batch, then delete the draft record."""
    db = SessionLocal()
    try:
        draft = db.query(ParseDraft).filter(ParseDraft.id == draft_id).first()
        if not draft:
            return jsonify({'error': 'Not found'}), 404

        archived_count = 0
        if draft.status == 'committed':
            qs = db.query(Question).filter(Question.parse_draft_id == draft_id).all()
            for q in qs:
                q.archived = True
            archived_count = len(qs)

        db.delete(draft)
        db.commit()
        return jsonify({'success': True, 'archived_count': archived_count}), 200
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()
