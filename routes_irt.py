"""Admin endpoints for IRT scoring system.

All routes require a valid admin Bearer JWT (via require_admin from admin.py).

GET  /admin/irt/status
POST /admin/irt/calibrate
POST /admin/irt/rollback/<version_id>
POST /admin/irt/auto-calibrate
"""
from flask import Blueprint, jsonify, request
from admin import require_admin

irt_bp = Blueprint("irt", __name__, url_prefix="/admin/irt")


@irt_bp.route("/status", methods=["GET"])
@require_admin
def irt_status():
    from database import SessionLocal, CalibrationRun, ItemParamVersion, ItemParams, IrtConfig
    db = SessionLocal()
    try:
        last_run = (db.query(CalibrationRun)
                    .order_by(CalibrationRun.started_at.desc())
                    .first())
        last_ok = (db.query(CalibrationRun)
                   .filter(CalibrationRun.status == "success")
                   .order_by(CalibrationRun.finished_at.desc())
                   .first())
        versions = (db.query(ItemParamVersion)
                    .order_by(ItemParamVersion.id.desc())
                    .limit(10)
                    .all())
        n_items = db.query(ItemParams).count()
        cfg = db.query(IrtConfig).filter_by(id=1).first()
        auto_enabled = cfg.auto_calibrate_enabled if cfg else True

        return jsonify({
            "auto_calibrate_enabled": auto_enabled,
            "n_calibrated_items": n_items,
            "last_run": {
                "id": last_run.id,
                "status": last_run.status,
                "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
                "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
                "message": last_run.message,
                "items_updated": last_run.items_updated,
            } if last_run else None,
            "last_success": {
                "id": last_ok.id,
                "finished_at": last_ok.finished_at.isoformat() if last_ok.finished_at else None,
                "items_updated": last_ok.items_updated,
                "version_id": last_ok.version_id,
            } if last_ok else None,
            "versions": [
                {
                    "id": v.id,
                    "version_label": v.version_label,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ],
        })
    finally:
        db.close()


@irt_bp.route("/calibrate", methods=["POST"])
@require_admin
def trigger_calibration():
    """Kick off a calibration run immediately (ignores day/count gates)."""
    import threading
    from irt_calibration import run_calibration, _calibration_lock, _run_calibration_guarded

    if _calibration_lock.acquire(blocking=False):
        t = threading.Thread(target=_run_calibration_guarded, daemon=True)
        t.start()
        return jsonify({"started": True, "message": "Calibration started in background."})
    return jsonify({"started": False, "message": "A calibration run is already in progress."}), 409


@irt_bp.route("/rollback/<int:version_id>", methods=["POST"])
@require_admin
def rollback_version(version_id):
    from irt_calibration import rollback_to_version
    result = rollback_to_version(version_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@irt_bp.route("/auto-calibrate", methods=["POST"])
@require_admin
def set_auto_calibrate():
    """Enable or disable auto-calibration.  Body: {"enabled": true|false}"""
    data = request.get_json() or {}
    if "enabled" not in data:
        return jsonify({"error": "enabled field required"}), 400

    from database import SessionLocal, IrtConfig
    db = SessionLocal()
    try:
        cfg = db.query(IrtConfig).filter_by(id=1).first()
        if not cfg:
            cfg = IrtConfig(id=1, auto_calibrate_enabled=bool(data["enabled"]))
            db.add(cfg)
        else:
            cfg.auto_calibrate_enabled = bool(data["enabled"])
        db.commit()
        return jsonify({"auto_calibrate_enabled": cfg.auto_calibrate_enabled})
    finally:
        db.close()
