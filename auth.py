import os
import jwt
import pyotp
import qrcode
import io
import base64
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError
from database import SessionLocal, User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
TOTP_ISSUER = "Digital SAT Analytics"


def _make_token(user_id):
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def _make_pending_2fa_token(user_id):
    """Short-lived token proving the password step passed, used only to complete 2FA login."""
    payload = {
        "user_id": user_id,
        "purpose": "2fa_pending",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def _verify_pending_2fa_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("purpose") != "2fa_pending":
        return None
    return payload.get("user_id")


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not username or not email or not password:
        return jsonify({"error": "username, email, and password are required"}), 400

    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first():
            return jsonify({"error": "Email already registered"}), 409

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return jsonify({"error": "Email already registered"}), 409
        db.refresh(user)

        return jsonify({
            "token": _make_token(user.id),
            "user_id": user.id,
            "name": user.username,
            "role": user.role,
        }), 201
    finally:
        db.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({"error": "Invalid credentials"}), 401

        if user.totp_enabled:
            return jsonify({
                "requires_2fa": True,
                "pending_token": _make_pending_2fa_token(user.id),
            }), 200

        return jsonify({
            "token": _make_token(user.id),
            "user_id": user.id,
            "name": user.username,
            "role": user.role,
        }), 200
    finally:
        db.close()


@auth_bp.route("/login/verify-2fa", methods=["POST"])
def login_verify_2fa():
    """Complete login by exchanging a pending_token + TOTP code for a real session token."""
    data = request.get_json(silent=True) or {}
    pending_token = data.get("pending_token", "")
    code = data.get("code", "").strip()

    if not pending_token or not code:
        return jsonify({"error": "pending_token and code are required"}), 400

    user_id = _verify_pending_2fa_token(pending_token)
    if not user_id:
        return jsonify({"error": "Invalid or expired pending token"}), 401

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.totp_enabled or not user.totp_secret:
            return jsonify({"error": "2FA is not enabled for this account"}), 400

        if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            return jsonify({"error": "Invalid code"}), 401

        return jsonify({
            "token": _make_token(user.id),
            "user_id": user.id,
            "name": user.username,
            "role": user.role,
        }), 200
    finally:
        db.close()


def _require_user(request):
    """Decode the Authorization bearer token and return the user_id, or None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(header[len("Bearer "):], SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("purpose") == "2fa_pending":
        return None
    return payload.get("user_id")


@auth_bp.route("/2fa/status", methods=["GET"])
def status_2fa():
    """Return whether 2FA is currently enabled for the authenticated user."""
    user_id = _require_user(request)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        return jsonify({"enabled": bool(user.totp_enabled)}), 200
    finally:
        db.close()


@auth_bp.route("/2fa/setup", methods=["POST"])
def setup_2fa():
    """Generate a new TOTP secret for the authenticated user and return a QR code to scan."""
    user_id = _require_user(request)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        secret = pyotp.random_base32()
        user.totp_secret = secret
        user.totp_enabled = False
        db.commit()

        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=TOTP_ISSUER)

        qr_img = qrcode.make(uri)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return jsonify({
            "secret": secret,
            "otp_uri": uri,
            "qr_code_png_base64": qr_base64,
        }), 200
    finally:
        db.close()


@auth_bp.route("/2fa/enable", methods=["POST"])
def enable_2fa():
    """Confirm setup by verifying a code from the authenticator app, then turn 2FA on."""
    user_id = _require_user(request)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.totp_secret:
            return jsonify({"error": "Call /auth/2fa/setup first"}), 400

        if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            return jsonify({"error": "Invalid code"}), 401

        user.totp_enabled = True
        db.commit()
        return jsonify({"success": True}), 200
    finally:
        db.close()


@auth_bp.route("/2fa/disable", methods=["POST"])
def disable_2fa():
    """Disable 2FA for the authenticated user after re-checking their password."""
    user_id = _require_user(request)
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if not password:
        return jsonify({"error": "password is required"}), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({"error": "Invalid credentials"}), 401

        user.totp_enabled = False
        user.totp_secret = None
        db.commit()
        return jsonify({"success": True}), 200
    finally:
        db.close()
