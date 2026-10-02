"""IRT scoring configuration. All tuneable values live here."""

# --- 3PL default item parameters -----------------------------------------------
# Applied when a question has no calibrated row in item_params.
DEFAULT_B = {"easy": -1.0, "medium": 0.0, "hard": 1.0}
DEFAULT_A = 1.0
DEFAULT_C = {"mcq": 0.25, "free_response": 0.0}
IRT_D = 1.7  # conventional logistic scaling constant

# --- Score scale ---------------------------------------------------------------
SCORE_MIN = 200
SCORE_MAX = 800
SCORE_STEP = 10          # round to nearest 10
LOWER_ROUTE_MAX_SCORE = 650   # cap when student took Lower Module 2

# --- EAP grid ------------------------------------------------------------------
# 61 equally-spaced theta nodes from -3.5 to 3.5 (more than sufficient precision)
THETA_GRID_N = 61
THETA_GRID_LO = -3.5
THETA_GRID_HI = 3.5

# --- Score bands ---------------------------------------------------------------
# (upper_bound_inclusive, label)
SCORE_BANDS = [
    (390, "Needs Improvement"),
    (540, "Developing"),
    (680, "Proficient"),
    (800, "Advanced"),
]

# --- Auto-calibration ---------------------------------------------------------
MIN_SESSIONS_FOR_CALIBRATION = 50      # minimum new TestCompletion rows since last run
MIN_DAYS_BETWEEN_CALIBRATIONS = 7
MIN_RESPONSES_FOR_ITEM_FIT = 200       # skip items with fewer observed responses
CALIBRATION_LOCK_TIMEOUT_MINUTES = 30
MAX_B_CHANGE_PER_RUN = 0.5             # b shift limit per run (prevents runaway drift)
A_BOUNDS = (0.5, 2.5)
B_BOUNDS = (-3.0, 3.0)

# --- Versioning ----------------------------------------------------------------
PARAM_VERSION_DEFAULT = "v1-label-defaults"
