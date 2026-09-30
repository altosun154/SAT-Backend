"""Auto-calibration of IRT item parameters.

Only one calibration may run at a time.  Any exception is caught, logged,
and stored in calibration_runs — scoring always uses the last good parameters
and never blocks on a calibration result.

Public API
----------
maybe_trigger_calibration()
    Called after each TestCompletion.  Opens its own DB session.  Starts a
    background thread if all conditions are met.

run_calibration()
    Full calibration in a background thread (can also be called directly for
    an admin-triggered run).  Opens its own DB session.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from math import exp, isfinite, log

from irt_config import (
    A_BOUNDS, B_BOUNDS, CALIBRATION_LOCK_TIMEOUT_MINUTES,
    IRT_D, MAX_B_CHANGE_PER_RUN, MIN_DAYS_BETWEEN_CALIBRATIONS,
    MIN_RESPONSES_FOR_ITEM_FIT, MIN_SESSIONS_FOR_CALIBRATION,
    PARAM_VERSION_DEFAULT,
)
from irt_scoring import _item_key, _default_params, eap_theta, _load_item_params, _GRID

_logger = logging.getLogger(__name__)
_calibration_lock = threading.Lock()   # in-process guard (DB lock is the authoritative one)


# ---------------------------------------------------------------------------
# MLE b estimation via bisection
# ---------------------------------------------------------------------------

def _mle_b(
    thetas: list[float],
    responses: list[int],
    a: float,
    c: float,
    b_init: float,
    bounds: tuple[float, float] = B_BOUNDS,
) -> float:
    """Find b that maximises log-likelihood via bisection on the gradient.

    Returns b_init (clamped) if no root is found in *bounds*.
    """
    D = IRT_D

    def grad(b: float) -> float:
        total = 0.0
        for theta, u in zip(thetas, responses):
            z = max(-30.0, min(30.0, D * a * (theta - b)))
            sigma = 1.0 / (1.0 + exp(-z))
            p = c + (1.0 - c) * sigma
            p = max(1e-10, min(1.0 - 1e-10, p))
            dp_db = -(1.0 - c) * D * a * sigma * (1.0 - sigma)
            total += dp_db * (u - p) / (p * (1.0 - p))
        return total

    lo, hi = bounds
    g_lo = grad(lo)
    g_hi = grad(hi)

    if g_lo * g_hi > 0:
        # No root in range — all-correct or all-wrong item; return nearest bound
        return max(lo, min(hi, b_init))

    for _ in range(60):
        mid = (lo + hi) / 2.0
        g_mid = grad(mid)
        if abs(g_mid) < 1e-6 or (hi - lo) < 1e-7:
            return mid
        if g_lo * g_mid <= 0:
            hi = mid
        else:
            lo = mid
            g_lo = g_mid

    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Condition checks
# ---------------------------------------------------------------------------

def _auto_calibrate_enabled(db) -> bool:
    """Env var + admin toggle both gate the feature."""
    if os.environ.get("IRT_AUTO_CALIBRATE", "1") == "0":
        return False
    from database import IrtConfig
    cfg = db.query(IrtConfig).filter_by(id=1).first()
    return cfg.auto_calibrate_enabled if cfg else True


def _should_run(db) -> bool:
    from database import CalibrationRun, TestCompletion

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=CALIBRATION_LOCK_TIMEOUT_MINUTES)

    # Already running?
    running = (db.query(CalibrationRun)
               .filter(CalibrationRun.status == "running",
                       CalibrationRun.started_at > stale_cutoff)
               .first())
    if running:
        return False

    last_ok = (db.query(CalibrationRun)
               .filter(CalibrationRun.status == "success")
               .order_by(CalibrationRun.finished_at.desc())
               .first())

    if last_ok and last_ok.finished_at:
        finished = last_ok.finished_at
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if (now - finished).days < MIN_DAYS_BETWEEN_CALIBRATIONS:
            return False
        new_count = db.query(TestCompletion).filter(
            TestCompletion.completed_at > last_ok.finished_at
        ).count()
        return new_count >= MIN_SESSIONS_FOR_CALIBRATION
    else:
        return db.query(TestCompletion).count() >= MIN_SESSIONS_FOR_CALIBRATION


# ---------------------------------------------------------------------------
# Public triggers
# ---------------------------------------------------------------------------

def maybe_trigger_calibration():
    """Check conditions; if met, start a background calibration thread."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        if not _auto_calibrate_enabled(db):
            return
        if not _should_run(db):
            return
    except Exception:
        _logger.exception("Error in calibration condition check")
        return
    finally:
        db.close()

    # Start background thread (daemon so it doesn't block server shutdown)
    if _calibration_lock.acquire(blocking=False):
        t = threading.Thread(target=_run_calibration_guarded, daemon=True)
        t.start()
    # If lock already held another thread got here first; skip silently


def _run_calibration_guarded():
    try:
        run_calibration()
    finally:
        try:
            _calibration_lock.release()
        except RuntimeError:
            pass


def run_calibration():
    """Full calibration run.  Safe to call directly (e.g. from admin endpoint).

    Acquires a DB-level lock row.  Any exception marks the run as failed and
    preserves the previous parameters unchanged.
    """
    from database import (
        SessionLocal, Question, Response, TestScore,
        ItemParams, ItemParamVersion, CalibrationRun, IrtConfig,
    )
    from adaptive import section_for_subject

    db = SessionLocal()
    run_row = None
    try:
        now = datetime.now(timezone.utc)

        # --- Double-check lock (race-condition guard) -------------------------
        stale_cutoff = now - timedelta(minutes=CALIBRATION_LOCK_TIMEOUT_MINUTES)
        if (db.query(CalibrationRun)
                .filter(CalibrationRun.status == "running",
                        CalibrationRun.started_at > stale_cutoff)
                .first()):
            return

        run_row = CalibrationRun(status="running", started_at=now)
        db.add(run_row)
        db.commit()

        # --- Gather item observations ----------------------------------------
        # For each item_key: list of (theta, u) pairs drawn from stored scores
        scores = (db.query(TestScore)
                  .filter(TestScore.rw_theta.isnot(None))
                  .all())
        if not scores:
            _finish(db, run_row, "success", "No scored sessions yet; nothing to calibrate.", 0)
            return

        session_theta: dict[tuple[str, str], float] = {}
        for s in scores:
            if s.rw_theta is not None:
                session_theta[(s.session_id, "reading_writing")] = s.rw_theta
            if s.math_theta is not None:
                session_theta[(s.session_id, "math")] = s.math_theta

        session_ids = [s.session_id for s in scores]
        # Process in chunks to avoid huge IN() clauses
        responses_all: list = []
        chunk = 500
        for i in range(0, len(session_ids), chunk):
            responses_all.extend(
                db.query(Response)
                .filter(Response.session_id.in_(session_ids[i:i + chunk]))
                .all()
            )

        q_ids = list({r.question_id for r in responses_all})
        questions_by_id: dict[int, object] = {}
        for i in range(0, len(q_ids), chunk):
            questions_by_id.update(
                {q.id: q for q in db.query(Question).filter(Question.id.in_(q_ids[i:i + chunk])).all()}
            )

        item_obs: dict[str, list[tuple[float, int]]] = {}
        item_q_cache: dict[str, object] = {}   # key -> any Question with that key (for defaults)
        for r in responses_all:
            q = questions_by_id.get(r.question_id)
            if not q:
                continue
            sec = section_for_subject(q.subject)
            if not sec:
                continue
            theta = session_theta.get((r.session_id, sec))
            if theta is None:
                continue
            key = _item_key(q)
            item_obs.setdefault(key, []).append((theta, 1 if r.is_correct else 0))
            item_q_cache.setdefault(key, q)

        # --- Determine Module 1 item keys for re-anchoring -------------------
        m1_keys: set[str] = {
            _item_key(q)
            for q in db.query(Question).filter(
                Question.module_variant.is_(None),
                Question.subject.like("%Module 1%"),
            ).all()
        }

        # --- Refit items with enough data ------------------------------------
        new_b: dict[str, float] = {}
        new_a: dict[str, float] = {}
        current_rows: dict[str, object] = {
            row.item_key: row
            for row in db.query(ItemParams).all()
        }

        for key, obs in item_obs.items():
            if len(obs) < MIN_RESPONSES_FOR_ITEM_FIT:
                continue
            ref_q = item_q_cache.get(key)
            cur = current_rows.get(key)
            if cur and isfinite(cur.a) and isfinite(cur.b) and isfinite(cur.c):
                a, b_old, c = cur.a, cur.b, cur.c
            else:
                a, b_old, c = _default_params(ref_q) if ref_q else (1.0, 0.0, 0.25)

            thetas_list = [o[0] for o in obs]
            us_list = [o[1] for o in obs]
            try:
                b_new = _mle_b(thetas_list, us_list, a, c, b_old)
                if not isfinite(b_new):
                    continue
            except Exception:
                continue

            # Limit per-run shift
            shift = b_new - b_old
            if abs(shift) > MAX_B_CHANGE_PER_RUN:
                b_new = b_old + MAX_B_CHANGE_PER_RUN * (1.0 if shift > 0 else -1.0)
            b_new = max(B_BOUNDS[0], min(B_BOUNDS[1], b_new))

            new_b[key] = b_new
            new_a[key] = max(A_BOUNDS[0], min(A_BOUNDS[1], a))   # a refitting not implemented yet

        if not new_b:
            _finish(db, run_row, "success", "Insufficient item data; no parameters updated.", 0)
            return

        # --- Re-anchor: keep mean(b of Module 1 items that were updated) -----
        updated_m1 = [k for k in new_b if k in m1_keys]
        if updated_m1:
            def _old_b(k):
                cur = current_rows.get(k)
                if cur and isfinite(cur.b):
                    return cur.b
                q = item_q_cache.get(k)
                return _default_params(q)[1] if q else 0.0

            mean_b_before = sum(_old_b(k) for k in updated_m1) / len(updated_m1)
            mean_b_after = sum(new_b[k] for k in updated_m1) / len(updated_m1)
            anchor_shift = mean_b_before - mean_b_after
            new_b = {k: b + anchor_shift for k, b in new_b.items()}

        # --- Persist new version and update item_params ----------------------
        ver_count = db.query(ItemParamVersion).count()
        version_label = (
            f"v{ver_count + 2}-calibrated-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        )
        params_snapshot = {
            k: {"a": new_a.get(k, current_rows[k].a if k in current_rows else 1.0),
                "b": new_b[k],
                "c": current_rows[k].c if k in current_rows else 0.25}
            for k in new_b
        }
        ver_row = ItemParamVersion(
            version_label=version_label,
            params_json=json.dumps(params_snapshot),
        )
        db.add(ver_row)
        db.flush()

        for key, b_new in new_b.items():
            a_new = new_a.get(key, 1.0)
            n_obs = len(item_obs.get(key, []))
            cur = current_rows.get(key)
            if cur:
                cur.a = a_new
                cur.b = b_new
                cur.n_responses = n_obs
                cur.version_id = ver_row.id
                cur.updated_at = datetime.now(timezone.utc)
            else:
                ref_q = item_q_cache.get(key)
                c_val = _default_params(ref_q)[2] if ref_q else 0.25
                db.add(ItemParams(
                    item_key=key, a=a_new, b=b_new, c=c_val,
                    n_responses=n_obs, version_id=ver_row.id,
                ))

        run_row.version_id = ver_row.id
        _finish(db, run_row, "success", f"Updated {len(new_b)} items.", len(new_b))

    except Exception as exc:
        _logger.exception("Calibration run failed")
        if run_row:
            try:
                _finish(db, run_row, "failed", str(exc)[:500], 0)
            except Exception:
                pass
    finally:
        db.close()


def _finish(db, run_row, status: str, message: str, n: int):
    run_row.status = status
    run_row.finished_at = datetime.now(timezone.utc)
    run_row.message = message
    run_row.items_updated = n
    db.commit()


# ---------------------------------------------------------------------------
# Admin helper: rollback
# ---------------------------------------------------------------------------

def rollback_to_version(version_id: int) -> dict:
    """Restore item_params from a saved ItemParamVersion row."""
    from database import SessionLocal, ItemParams, ItemParamVersion

    db = SessionLocal()
    try:
        ver = db.query(ItemParamVersion).filter_by(id=version_id).first()
        if not ver:
            return {"error": f"Version {version_id} not found"}

        params = json.loads(ver.params_json)
        existing = {row.item_key: row for row in db.query(ItemParams).all()}
        n = 0
        for key, p in params.items():
            a, b, c = p["a"], p["b"], p["c"]
            if not (isfinite(a) and isfinite(b) and isfinite(c)):
                continue
            if key in existing:
                r = existing[key]
                r.a, r.b, r.c = a, b, c
                r.version_id = version_id
                r.updated_at = datetime.now(timezone.utc)
            else:
                db.add(ItemParams(item_key=key, a=a, b=b, c=c, version_id=version_id))
            n += 1
        db.commit()
        return {"restored": n, "version_label": ver.version_label}
    finally:
        db.close()
