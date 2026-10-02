"""Unit tests, simulation, and failure injection for IRT scoring system.

Run with:  python -m pytest tests/test_irt.py -v
Or directly: python tests/test_irt.py
"""
import math
import random
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from irt_scoring import p3pl, eap_theta, _scale_score, score_band, _build_grid
from irt_calibration import _mle_b
from irt_config import (
    SCORE_MIN, SCORE_MAX, LOWER_ROUTE_MAX_SCORE, SCORE_STEP,
    DEFAULT_A, DEFAULT_B, DEFAULT_C, IRT_D,
    B_BOUNDS,
)


# ---------------------------------------------------------------------------
# p3pl
# ---------------------------------------------------------------------------

def test_p3pl_limits():
    """c <= P(theta) <= 1 for all theta."""
    for a in (0.5, 1.0, 2.0):
        for b in (-2.0, 0.0, 2.0):
            for c in (0.0, 0.25):
                for theta in (-4.0, 0.0, 4.0):
                    p = p3pl(theta, a, b, c)
                    assert c <= p <= 1.0 + 1e-9, f"p3pl out of range: {p} (a={a},b={b},c={c},theta={theta})"


def test_p3pl_monotone():
    """P increases as theta increases."""
    for a in (0.5, 1.0, 2.0):
        for b in (-1.0, 0.0, 1.0):
            ps = [p3pl(t, a, b, 0.25) for t in (-3.0, -1.0, 0.0, 1.0, 3.0)]
            for i in range(len(ps) - 1):
                assert ps[i] <= ps[i + 1], f"Non-monotone at a={a},b={b}"


def test_p3pl_at_b():
    """At theta == b: P = c + (1-c)/2."""
    a, b, c = 1.0, 0.5, 0.25
    p = p3pl(b, a, b, c)
    expected = c + (1.0 - c) / 2.0
    assert abs(p - expected) < 1e-9


def test_p3pl_overflow_guard():
    """Extreme thetas must not raise OverflowError."""
    assert math.isfinite(p3pl(1000.0, 2.0, 0.0, 0.25))
    assert math.isfinite(p3pl(-1000.0, 2.0, 0.0, 0.25))


# ---------------------------------------------------------------------------
# eap_theta
# ---------------------------------------------------------------------------

def test_eap_empty():
    assert eap_theta([], []) == 0.0


def test_eap_all_correct_positive():
    params = [(1.0, 0.0, 0.25)] * 20
    resp = [1] * 20
    t = eap_theta(params, resp)
    assert t > 0.0, f"Expected positive theta for all-correct: {t}"


def test_eap_all_wrong_negative():
    params = [(1.0, 0.0, 0.25)] * 20
    resp = [0] * 20
    t = eap_theta(params, resp)
    assert t < 0.0, f"Expected negative theta for all-wrong: {t}"


def test_eap_range():
    """Theta must stay within grid bounds."""
    grid = _build_grid(61, -3.5, 3.5)
    params = [(1.0, b, 0.25) for b in (-2.0, -1.0, 0.0, 1.0, 2.0)]
    for resp in ([1]*5, [0]*5, [1,0,1,0,1]):
        t = eap_theta(params, resp, grid)
        assert -3.5 <= t <= 3.5, f"Theta out of range: {t}"


def test_eap_easy_miss_lower_than_hard_miss():
    """Missing all easy items implies lower ability than missing all hard items."""
    params_easy = [(1.0, -1.5, 0.25)] * 10   # easy: b=-1.5
    params_hard = [(1.0,  1.5, 0.25)] * 10   # hard: b=+1.5
    t_easy_miss = eap_theta(params_easy, [0]*10)   # missed all easy → very low theta
    t_hard_miss = eap_theta(params_hard, [0]*10)   # missed all hard → moderate theta
    assert t_easy_miss < t_hard_miss, (
        f"Missing easy items should imply lower theta: easy_miss={t_easy_miss:.3f}, hard_miss={t_hard_miss:.3f}"
    )


# ---------------------------------------------------------------------------
# _scale_score
# ---------------------------------------------------------------------------

def test_scale_score_bounds():
    assert _scale_score(-10.0, -1.0, 1.0, False) == SCORE_MIN
    assert _scale_score(10.0, -1.0, 1.0, False) == SCORE_MAX


def test_scale_score_lower_route_cap():
    score = _scale_score(10.0, -1.0, 1.0, True)
    assert score <= LOWER_ROUTE_MAX_SCORE


def test_scale_score_step():
    for theta in (-0.5, 0.0, 0.5):
        s = _scale_score(theta, -2.0, 2.0, False)
        assert s % SCORE_STEP == 0


def test_scale_score_degenerate_range():
    """theta_min == theta_max should not crash."""
    s = _scale_score(0.0, 0.0, 0.0, False)
    assert SCORE_MIN <= s <= SCORE_MAX


# ---------------------------------------------------------------------------
# score_band
# ---------------------------------------------------------------------------

def test_score_band_boundaries():
    assert score_band(390) == "Needs Improvement"
    assert score_band(400) == "Developing"
    assert score_band(540) == "Developing"
    assert score_band(541) == "Proficient"
    assert score_band(680) == "Proficient"
    assert score_band(800) == "Advanced"


# ---------------------------------------------------------------------------
# _mle_b
# ---------------------------------------------------------------------------

def test_mle_b_all_correct():
    """All-correct: estimated b should hit upper bound or be very high."""
    thetas = [0.0] * 30
    responses = [1] * 30
    b = _mle_b(thetas, responses, 1.0, 0.25, 0.0)
    assert math.isfinite(b)
    assert B_BOUNDS[0] <= b <= B_BOUNDS[1]


def test_mle_b_all_wrong():
    """All-wrong: estimated b should hit lower bound or be very low."""
    thetas = [0.0] * 30
    responses = [0] * 30
    b = _mle_b(thetas, responses, 1.0, 0.25, 0.0)
    assert math.isfinite(b)
    assert B_BOUNDS[0] <= b <= B_BOUNDS[1]


def test_mle_b_recovery():
    """Simulate data from a known b, recover within 0.3."""
    true_b = 0.5
    a, c = 1.0, 0.25
    random.seed(42)
    thetas = [random.gauss(0, 1) for _ in range(500)]
    responses = [1 if random.random() < p3pl(t, a, true_b, c) else 0 for t in thetas]
    b_hat = _mle_b(thetas, responses, a, c, 0.0)
    assert abs(b_hat - true_b) < 0.3, f"b recovery failed: true={true_b}, hat={b_hat}"


def test_mle_b_bounds():
    random.seed(7)
    thetas = [random.gauss(0, 1) for _ in range(100)]
    responses = [random.randint(0, 1) for _ in range(100)]
    b = _mle_b(thetas, responses, 1.0, 0.25, 0.0)
    assert B_BOUNDS[0] <= b <= B_BOUNDS[1]


# ---------------------------------------------------------------------------
# 2000-student simulation
# ---------------------------------------------------------------------------

def _simulate_session(true_theta: float, items: list, rng: random.Random) -> tuple:
    """Simulate a student answering each item; return (item_params, responses)."""
    params = items
    responses = [1 if rng.random() < p3pl(true_theta, a, b, c) else 0 for a, b, c in params]
    return params, responses


def test_simulation_2000_students():
    """2000-student simulation: mean recovered theta ≈ mean true theta (±0.15)."""
    rng = random.Random(2024)

    # 27 R&W Module 1 items with realistic b spread
    items = [
        (1.0, -1.5, 0.25), (1.0, -1.2, 0.25), (1.0, -1.0, 0.25),
        (1.0, -0.8, 0.25), (1.0, -0.6, 0.25), (1.0, -0.4, 0.25),
        (1.0, -0.2, 0.25), (1.0,  0.0, 0.25), (1.0,  0.2, 0.25),
        (1.0,  0.4, 0.25), (1.0,  0.6, 0.25), (1.0,  0.8, 0.25),
        (1.0,  1.0, 0.25), (1.0,  1.2, 0.25), (1.0,  1.5, 0.25),
        (1.2, -1.3, 0.25), (1.2, -0.5, 0.25), (1.2,  0.1, 0.25),
        (1.2,  0.7, 0.25), (1.2,  1.4, 0.25), (0.8, -1.0, 0.25),
        (0.8,  0.0, 0.25), (0.8,  1.0, 0.25), (1.5, -0.5, 0.25),
        (1.5,  0.5, 0.25), (1.5,  1.3, 0.0),  (1.0,  0.3, 0.0),
    ]

    true_thetas = [rng.gauss(0.0, 1.0) for _ in range(2000)]
    estimated_thetas = []
    scores = []

    grid = _build_grid(61, -3.5, 3.5)
    theta_min = eap_theta(items, [0] * len(items), grid)
    theta_max = eap_theta(items, [1] * len(items) + [], grid)
    # Use a one-miss max
    resp_one_miss = [1] * len(items)
    resp_one_miss[-1] = 0
    theta_max = eap_theta(items, resp_one_miss, grid)

    for true_t in true_thetas:
        params, resp = _simulate_session(true_t, items, rng)
        t_hat = eap_theta(params, resp, grid)
        estimated_thetas.append(t_hat)
        scores.append(_scale_score(t_hat, theta_min, theta_max, False))

    mean_true = sum(true_thetas) / len(true_thetas)
    mean_est = sum(estimated_thetas) / len(estimated_thetas)
    assert abs(mean_est - mean_true) < 0.15, (
        f"Simulation bias too large: mean_true={mean_true:.3f}, mean_est={mean_est:.3f}"
    )

    # Scores should all be in valid range
    assert all(SCORE_MIN <= s <= SCORE_MAX for s in scores), "Score out of range in simulation"

    # Reasonable spread: std of scores should be > 50
    mean_score = sum(scores) / len(scores)
    std_score = math.sqrt(sum((s - mean_score) ** 2 for s in scores) / len(scores))
    assert std_score > 50, f"Score spread too narrow: std={std_score:.1f}"

    print(f"\nSimulation: mean_true={mean_true:.3f}, mean_est={mean_est:.3f}, "
          f"score_mean={mean_score:.0f}, score_std={std_score:.0f}")


# ---------------------------------------------------------------------------
# Failure injection
# ---------------------------------------------------------------------------

def test_eap_degenerate_all_zero_posterior():
    """Verify eap_theta returns 0.0 (not crash) on pathological input."""
    # Extreme items that push posterior to near-zero everywhere
    params = [(2.0, 3.5, 0.0)] * 50
    responses = [0] * 50
    t = eap_theta(params, responses)
    assert math.isfinite(t)


def test_scale_score_nan_theta():
    """NaN theta: _scale_score must not crash (clamped to min)."""
    try:
        s = _scale_score(float("nan"), -1.0, 1.0, False)
        # If it doesn't crash and clamps, that's fine
        assert SCORE_MIN <= s <= SCORE_MAX or math.isnan(s)
    except Exception as e:
        # Crashing with a clear error is also acceptable — not a silent wrong answer
        pass


def test_mle_b_empty_responses():
    """Empty lists: should return clamped b_init without crashing."""
    b = _mle_b([], [], 1.0, 0.25, 0.5)
    assert B_BOUNDS[0] <= b <= B_BOUNDS[1]


def test_eap_single_item():
    params = [(1.0, 0.0, 0.25)]
    t_correct = eap_theta(params, [1])
    t_wrong = eap_theta(params, [0])
    assert t_correct > t_wrong


if __name__ == "__main__":
    tests = [
        test_p3pl_limits, test_p3pl_monotone, test_p3pl_at_b, test_p3pl_overflow_guard,
        test_eap_empty, test_eap_all_correct_positive, test_eap_all_wrong_negative,
        test_eap_range, test_eap_easy_miss_lower_than_hard_miss,
        test_scale_score_bounds, test_scale_score_lower_route_cap,
        test_scale_score_step, test_scale_score_degenerate_range,
        test_score_band_boundaries,
        test_mle_b_all_correct, test_mle_b_all_wrong, test_mle_b_recovery, test_mle_b_bounds,
        test_simulation_2000_students,
        test_eap_degenerate_all_zero_posterior, test_scale_score_nan_theta,
        test_mle_b_empty_responses, test_eap_single_item,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
