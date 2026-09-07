import os
import jwt
from flask import Blueprint, request, jsonify
from sqlalchemy.exc import IntegrityError
from database import SessionLocal, Activity

activity_bp = Blueprint("activity", __name__)

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")


def _get_user_id_from_token():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(auth.split(" ", 1)[1], SECRET_KEY, algorithms=["HS256"])
        return payload.get("user_id")
    except jwt.PyJWTError:
        return None


@activity_bp.route("/activity", methods=["POST"])
def log_activity():
    user_id = _get_user_id_from_token()
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    date = data.get("date", "")
    activity_type = data.get("type", "login")

    if not date:
        return jsonify({"error": "date is required"}), 400

    db = SessionLocal()
    try:
        db.add(Activity(user_id=user_id, date=date, type=activity_type))
        db.commit()
    except IntegrityError:
        db.rollback()  # same-day entry already exists — ignore
    finally:
        db.close()

    return jsonify({"success": True}), 200
