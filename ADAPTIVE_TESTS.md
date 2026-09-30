# Adaptive practice tests

## How a test works

1. **Module 1 (the first half) is fixed.** Every student gets the same Module 1 questions, exactly as in the client's PDF.
2. **The backend grades Module 1.** When a student submits Module 1, the frontend calls `POST /tests/<id>/module2`. The backend grades the answers and picks a Module 2:
   - **Reading & Writing:** 19 or more of 27 correct gets the **Higher** Module 2 (`module_variant = "hard"`). Otherwise the student gets the **Lower** Module 2 (`"easy"`).
   - **Math:** 16 or more of 22 correct gets **Higher**. Otherwise **Lower**.
3. **Difficulty mix of each Module 2:**
   - **Lower:** 45% easy, 40% medium, 15% hard (R&W 12/11/4, Math 10/9/3).
   - **Higher:** 15% easy, 40% medium, 45% hard (R&W 4/11/12, Math 3/9/10).
4. **Answers are saved and graded by `/submit` as before.** Typed math answers accept equivalent forms: `3/4`, `.75` and `0.75` all count.

The rules live in `adaptive.py` and the answer checking in `grading.py`. Both are plain Python and don't depend on the database.

## Where the questions live

| Table | What's in it |
|---|---|
| `questions` | Every question a student can get: each test's Module 1, plus the Lower and Higher Module 2 built for that test. |
| `module2_pool_questions` | Every Module 2 question from the client's PDFs, stored once. `used_in_test_id` shows which test it went into; empty means unused. |

**How a Module 2 gets built:** `module2_builder.build_module2()` takes **unused** pool questions in the ratios above, copies them into `questions` for one test, and marks them used. It prefers questions from the test's own PDF first, then spreads its picks across the other PDFs.

**No question goes into two tests.** The Lower and Higher versions of the same test share their medium questions and some easy and hard ones. That's fine because a student only ever sees one of the two versions.

**Tests that aren't complete are hidden.** A test whose Module 2 can't be built yet stays `is_published = False`. Students don't see it, and admins can't assign it.

## Importing the client's 10 tests

The data is already in `data/practice_tests/`:

- `module1_questions.csv`
- `module2_pool.csv`
- `images/`, with 105 PNGs

1. **Check the data.** This doesn't touch the database:
   ```bash
   python import_practice_tests.py --dry-run
   ```
2. **Test the whole flow locally.** This uses a throwaway SQLite file, not your real database:
   ```bash
   python smoke_test_adaptive.py
   ```
3. **Run the real import.** Use the same values Render has:
   ```bash
   DATABASE_URL='postgresql://...' SUPABASE_SERVICE_KEY='...' python import_practice_tests.py
   ```

The import does four things:

- uploads the images to `question-images/practice-tests/` in Supabase Storage;
- creates "SAT Practice Test 1" to "SAT Practice Test 10";
- imports each test's Module 1 and fills the pool;
- builds Module 2 for as many tests as the pool allows.

With the current PDFs that's **3 tests**. The pool has only 38 hard Math questions, and each test needs 10. Tests 4–10 keep their Module 1 and stay hidden until the pool has enough questions. Running the import again is safe, because it skips anything already imported.

## Adding questions later

- **New Module 2 questions:** add them to `module2_pool_questions` with `used_in_test_id` empty. Then call `POST /admin/module2/build` (admin token) or re-run the import. Every hidden test that can now be completed gets built and published.
- **Pool status:** `GET /admin/module2-pool` shows used and unused counts by difficulty, plus which tests are still waiting.
- **What one more test needs:** 12 easy, 11 medium and 12 hard R&W questions, and 10 easy, 9 medium and 10 hard Math questions. Test 4 needs only **2 more hard Math** questions.

## Images and passages

- **Stored on each question:** each question row has `passage` (text) and `image_url`, a full link to the PNG in Supabase Storage.
- **Sent to the page:** the API returns both fields, and `practice-tests.js` shows the passage above the question and the image under the question text.

---

## Scoring — how section scores are calculated

### The short version (non-technical)

Every time a student finishes a test, the backend automatically calculates their **Reading & Writing score** (200–800) and **Math score** (200–800) using a statistical method called **IRT** (Item Response Theory) — the same method that College Board uses for the real SAT.

A student who took the **Lower Module 2** (the easier path) can score a maximum of **650** in that section. The Higher Module 2 path can reach **800**. This is intentional: the lower path is for students still building foundational skills, and the scoring cap reflects that.

The **total score** (400–1600) is the sum of both section scores.

### What "band" labels mean

Each section score also comes with a band label:

| Score range | Band |
|---|---|
| 200–390 | Needs Improvement |
| 400–540 | Developing |
| 550–680 | Proficient |
| 690–800 | Advanced |

These show up on the student's results page next to each section score and in the admin dashboard.

### What IRT is and why it matters

The old scoring formula (`200 + correct/total × 600`) treats every question as equal. A student who got a lucky guess on a hard question and a student who genuinely answered it correctly would get the same credit. IRT fixes this.

Under IRT:
- Each question has a **difficulty** parameter. Getting a hard question right counts for more than getting an easy one right.
- Getting a question wrong when you had a high ability estimate counts against you more than getting one wrong when you were clearly struggling.
- The estimate accounts for the fact that students guess on multiple-choice questions (a lucky guess doesn't move the needle much).

The score still scales to 200–800 in the same way. The IRT model just makes the ability estimate more accurate before scaling it.

### How calibration works (technical background)

The difficulty parameters start from the question's difficulty tag (`easy` → −1.0, `medium` → 0.0, `hard` → +1.0). Over time, as students actually take the tests, the backend can refine these values based on observed response patterns — a question tagged "easy" that 40% of students get wrong is probably harder than its label suggests.

Calibration runs automatically in a background thread after test completions, subject to three gates:
- It's been at least **7 days** since the last successful run.
- At least **50 new test completions** have happened since then.
- Each question needs at least **200 responses** before its parameter is updated — items with fewer responses keep their label-based defaults.

Each run saves a **versioned snapshot** of all parameters. If a calibration ever produces unexpected results, an admin can roll back to any previous version from the admin dashboard.

### Admin controls

| Action | How |
|---|---|
| View calibration status | `GET /admin/irt/status` |
| Trigger a calibration immediately | `POST /admin/irt/calibrate` |
| Roll back to a previous parameter version | `POST /admin/irt/rollback/<version_id>` |
| Disable/enable auto-calibration | `POST /admin/irt/auto-calibrate` with `{"enabled": true/false}` |

All of these require an admin JWT token in the `Authorization: Bearer <token>` header.

---

## Moving to a different database or host

- **Database:** everything here goes through SQLAlchemy, so any PostgreSQL works. Copy the data with `pg_dump`/`pg_restore` and change `DATABASE_URL`.
- **Images:** they're the only part tied to Supabase. If you move storage, copy the `question-images` bucket and update the links:
  ```sql
  update questions set image_url = replace(image_url, 'old-host', 'new-host');
  ```
  Run the same update on `module2_pool_questions`.
