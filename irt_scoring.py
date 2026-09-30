"""3PL IRT scoring: EAP ability estimation, score scaling, band assignment.

Public API
----------
compute_and_store_score(user_id, test_id, session_id) -> TestScore | None
    Called after a TestCompletion.  Opens its own DB session.  Idempotent.

get_stored_score(db, user_id, test_id, session_id) -> TestScore | None
    Read a previously stored score row.

score_band(score) -> str
    Band label for a 200-800 score.
"""
from __future__ import annotations

import logging
from math import exp, log as _log, sqrt, pi, isfinite

from irt_config import (
    DEFAULT_A, DEFAULT_B, DEFAULT_C, IRT_D,
    SCORE_MIN, SCORE_MAX, SCORE_STEP, LOWER_ROUTE_MAX_SCORE,
    THETA_GRID_N, THETA_GRID_LO, THETA_GRID_HI,
    SCORE_BANDS, PARAM_VERSION_DEFAULT,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EAP grid  (module-level constant, built once)
# ---------------------------------------------------------------------------

def _build_grid(n: int, lo: float, hi: float):
    thetas = [lo + i * (hi - lo) / (n - 1) for i in range(n)]
    # log prior: log N(0,1) up to an additive constant (we normalise anyway)
    log_prior = [-0.5 * t * t for t in thetas]
    return thetas, log_prior


_GRID = _build_grid(THETA_GRID_N, THETA_GRID_LO, THETA_GRID_HI)


# ---------------------------------------------------------------------------
# 3PL model helpers
# ---------------------------------------------------------------------------

def p3pl(theta: float, a: float, b: float, c: float, D: float = IRT_D) -> float:
    """Probability of a correct response under the 3PL model."""
    z = D * a * (theta - b)
    z = max(-30.0, min(30.0, z))          # prevent overflow
    logistic = 1.0 / (1.0 + exp(-z))
    return c + (1.0 - c) * logistic


# ---------------------------------------------------------------------------
# EAP estimation
# ---------------------------------------------------------------------------

def eap_theta(
    item_params: list[tuple[float, float, float]],
    responses: list[int],
    grid=None,
) -> float:
    """Expected A-Posteriori theta estimate.

    item_params : list of (a, b, c) tuples
    responses   : list of 0/1 (unanswered treated as 0 by caller)
    grid        : (thetas, log_prior) from _build_grid; uses module default.

    Returns a float theta.  Falls back to prior mean (0.0) on degenerate input.
    """
    if grid is None:
        grid = _GRID
    thetas, log_prior = grid

    if not item_params:
        return 0.0

    # Work in log-space to stay numerically stable
    log_post = list(log_prior)
    for (a, b, c), u in zip(item_params, responses):
        for i, theta in enumerate(thetas):
            p = p3pl(theta, a, b, c)
            p = max(1e-10, min(1.0 - 1e-10, p))
            log_post[i] += _log(p) if u else _log(1.0 - p)

    # Subtract max to avoid underflow before exp
    max_lp = max(log_post)
    posterior = [exp(lp - max_lp) for lp in log_post]
    total = sum(posterior)
    if total == 0.0:
        return 0.0

    return sum(t * w / total for t, w in zip(thetas, posterior))


# ---------------------------------------------------------------------------
# Score scaling
# ---------------------------------------------------------------------------

def _scale_score(
    theta: float,
    theta_min: float,
    theta_max: float,
    is_lower_route: bool,
) -> int:
    """Map theta to the 200-800 scale (multiples of 10)."""
    if theta_max <= theta_min:
        raw = (SCORE_MIN + SCORE_MAX) / 2
    else:
        raw = SCORE_MIN + (theta - theta_min) / (theta_max - theta_min) * (SCORE_MAX - SCORE_MIN)
    clamped = max(float(SCORE_MIN), min(float(SCORE_MAX), raw))
    if is_lower_route:
        clamped = min(clamped, float(LOWER_ROUTE_MAX_SCORE))
    return round(clamped / SCORE_STEP) * SCORE_STEP


def score_band(score: int) -> str:
    """Return the performance band for a section score."""
    for cutoff, label in SCORE_BANDS:
        if score <= cutoff:
            return label
    return SCORE_BANDS[-1][1]


# ---------------------------------------------------------------------------
# Anchor theta computation
# ---------------------------------------------------------------------------

def _anchor_thetas(
    all_items: list[tuple[float, float, float]],
    hard_m2_indices: list[int],
) -> tuple[float, float]:
    """Return (theta_min, theta_max) for the scale anchors.

    theta_min: all questions wrong
    theta_max: all correct except one hard Module-2 miss
    """
    n = len(all_items)
    theta_min = eap_theta(all_items, [0] * n)

    responses_one_miss = [1] * n
    for idx in hard_m2_indices:
        responses_one_miss[idx] = 0
        break   # one miss is enough

    theta_max = eap_theta(all_items, responses_one_miss)
    return theta_min, theta_max


# ---------------------------------------------------------------------------
# Item-parameter lookup
# ---------------------------------------------------------------------------

def _item_key(q) -> str:
    """Stable identifier shared by a pool question and all its copies."""
    return q.source_key if q.source_key else f"q{q.id}"


def _default_params(q) -> tuple[float, float, float]:
    diff = (q.difficulty or "medium").lower()
    qtype = (q.question_type or "mcq").lower()
    return DEFAULT_A, DEFAULT_B.get(diff, 0.0), DEFAULT_C.get(qtype, 0.25)


def _load_item_params(db, questions) -> dict[str, tuple[float, float, float]]:
    """Return {item_key: (a, b, c)} using DB rows where available, defaults otherwise."""
    from database import ItemParams

    keys = [_item_key(q) for q in questions]
    rows = {
        row.item_key: (row.a, row.b, row.c)
        for row in db.query(ItemParams).filter(ItemParams.item_key.in_(keys)).all()
        if isfinite(row.a) and isfinite(row.b) and isfinite(row.c)
    }
    result: dict[str, tuple[float, float, float]] = {}
    for q in questions:
        k = _item_key(q)
        result[k] = rows.get(k) or _default_params(q)
    return result


# ---------------------------------------------------------------------------
# Section scoring
# ---------------------------------------------------------------------------

def _score_one_section(
    db,
    test_id: int,
    section: str,            # "reading_writing" or "math"
    student_questions,       # list of Question objects the student saw
    student_responses,       # list of Response objects (same order)
) -> tuple[int | None, float | None, str | None, str | None]:
    """Return (score, theta, route, band) for one section.  None on failure."""
    from adaptive import SECTIONS

    cfg = SECTIONS.get(section)
    if not student_questions:
        return None, None, None, None

    # --- Determine route -------------------------------------------------------
    route = None
    for q in student_questions:
        if q.module_variant in ("easy", "hard"):
            route = q.module_variant
            break

    # --- Student EAP -----------------------------------------------------------
    params_map = _load_item_params(db, student_questions)
    s_params = [params_map[_item_key(q)] for q in student_questions]
    resp_by_qid = {r.question_id: r for r in student_responses}
    s_resp = [1 if resp_by_qid.get(q.id) and resp_by_qid[q.id].is_correct else 0
              for q in student_questions]

    theta = eap_theta(s_params, s_resp)

    # --- Anchor questions (Module 1 + Higher Module 2 for this test) ----------
    from database import Question as QuestionModel
    from sqlalchemy import or_

    if cfg:
        m1_qs = (db.query(QuestionModel)
                 .filter(QuestionModel.test_id == test_id,
                         QuestionModel.subject == cfg["module1_subject"],
                         QuestionModel.module_variant.is_(None))
                 .all())
        m2_hard_qs = (db.query(QuestionModel)
                      .filter(QuestionModel.test_id == test_id,
                              QuestionModel.subject == cfg["module2_subject"],
                              QuestionModel.module_variant == "hard")
                      .all())
        if not m2_hard_qs:
            # non-adaptive test: use all module-2 questions as anchor
            m2_hard_qs = (db.query(QuestionModel)
                          .filter(QuestionModel.test_id == test_id,
                                  QuestionModel.subject == cfg["module2_subject"])
                          .all())
    else:
        m1_qs = [q for q in student_questions if q.module_variant is None]
        m2_hard_qs = []

    anchor_qs = list(m1_qs) + list(m2_hard_qs)
    if not anchor_qs:
        anchor_qs = list(student_questions)

    anchor_params_map = _load_item_params(db, anchor_qs)
    a_params = [anchor_params_map[_item_key(q)] for q in anchor_qs]

    # Indices of hard Module-2 questions within anchor_qs
    m1_set = {q.id for q in m1_qs}
    hard_m2_indices = [
        i for i, q in enumerate(anchor_qs)
        if q.id not in m1_set and q.difficulty == "hard"
    ]
    if not hard_m2_indices:
        # no hard items found: use any non-m1 item as the one-miss
        hard_m2_indices = [i for i, q in enumerate(anchor_qs) if q.id not in m1_set]
    if not hard_m2_indices:
        hard_m2_indices = [len(anchor_qs) - 1]  # last resort

    theta_min, theta_max = _anchor_thetas(a_params, hard_m2_indices)

    # --- Scale -----------------------------------------------------------------
    is_lower = (route == "easy")
    score = _scale_score(theta, theta_min, theta_max, is_lower)
    band = score_band(score)

    return score, theta, route, band


# ---------------------------------------------------------------------------
# Main public entry points
# ---------------------------------------------------------------------------

def compute_and_store_score(user_id: int, test_id: int, session_id: str):
    """Compute IRT scores for a finished session and persist them.

    Opens its own DB session.  Idempotent — returns the existing row if one
    already exists for this session_id.  Never raises: swallows exceptions
    and logs them so a scoring failure never breaks /submit.
    """
    from database import (
        SessionLocal, Question, Response, TestScore,
        ItemParamVersion,
    )
    from adaptive import section_for_subject

    db = SessionLocal()
    try:
        existing = db.query(TestScore).filter(TestScore.session_id == session_id).first()
        if existing:
            return existing

        responses = (db.query(Response)
                     .filter(Response.user_id == user_id,
                             Response.test_id == test_id,
                             Response.session_id == session_id)
                     .all())
        if not responses:
            return None

        q_ids = [r.question_id for r in responses]
        all_qs = {q.id: q for q in db.query(Question).filter(Question.id.in_(q_ids)).all()}

        # Group by section
        by_section: dict[str, tuple[list, list]] = {}
        for r in responses:
            q = all_qs.get(r.question_id)
            if not q:
                continue
            sec = section_for_subject(q.subject)
            if not sec:
                continue
            if sec not in by_section:
                by_section[sec] = ([], [])
            by_section[sec][0].append(q)
            by_section[sec][1].append(r)

        rw_score = rw_theta = rw_route = rw_band = None
        math_score = math_theta = math_route = math_band = None

        for sec, (qs, rs) in by_section.items():
            sc, th, ro, ba = _score_one_section(db, test_id, sec, qs, rs)
            if sec == "reading_writing":
                rw_score, rw_theta, rw_route, rw_band = sc, th, ro, ba
            else:
                math_score, math_theta, math_route, math_band = sc, th, ro, ba

        total_score = None
        if rw_score is not None and math_score is not None:
            total_score = rw_score + math_score
        elif rw_score is not None:
            total_score = rw_score
        elif math_score is not None:
            total_score = math_score

        latest_ver = (db.query(ItemParamVersion)
                      .order_by(ItemParamVersion.id.desc())
                      .first())
        param_version = latest_ver.version_label if latest_ver else PARAM_VERSION_DEFAULT

        row = TestScore(
            user_id=user_id,
            test_id=test_id,
            session_id=session_id,
            rw_score=rw_score,
            math_score=math_score,
            total_score=total_score,
            rw_band=rw_band,
            math_band=math_band,
            rw_theta=rw_theta,
            math_theta=math_theta,
            rw_route=rw_route,
            math_route=math_route,
            param_version=param_version,
        )
        db.add(row)
        db.commit()
        return row

    except Exception:
        _logger.exception("IRT scoring failed for session %s", session_id)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def get_stored_score(db, user_id: int, test_id: int, session_id: str):
    """Return TestScore for this session or None if not yet computed."""
    from database import TestScore
    return (db.query(TestScore)
            .filter(TestScore.session_id == session_id,
                    TestScore.user_id == user_id,
                    TestScore.test_id == test_id)
            .first())
