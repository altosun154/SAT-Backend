"""One-command import of the client's adaptive practice tests.

    python import_practice_tests.py            # upload images, import questions, build Module 2s
    python import_practice_tests.py --dry-run  # only check the data files, touch nothing

Needs the same environment variables as the backend:
    DATABASE_URL          the production (Supabase Postgres) connection string
    SUPABASE_SERVICE_KEY  service-role key, used to upload the images

What it does
  1. Uploads every PNG in data/practice_tests/images/ to the Supabase bucket
     question-images/practice-tests/ (overwrites, so re-running is safe).
  2. Creates (or reuses) a test named "SAT Practice Test N" for each test in
     module1_questions.csv and inserts its Module 1 exactly as in the PDF.
     New tests start unpublished (hidden from students).
  3. Puts every Module 2 question into the pool table module2_pool_questions.
  4. Builds Lower/Higher Module 2 for as many tests as the pool allows without any
     question being used by two tests, and publishes those tests.

Running it again skips anything already imported.
"""
import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "practice_tests")


def read_csv(name):
    with open(os.path.join(DATA, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def blank_to_none(v):
    return v if v not in ("", None) else None


def check_data(module1, pool):
    from adaptive import SECTIONS
    problems = []
    for r in module1 + pool:
        if r["section"] not in SECTIONS:
            problems.append(f"{r['source_key']}: unknown section {r['section']}")
        if r["difficulty"] not in ("easy", "medium", "hard"):
            problems.append(f"{r['source_key']}: bad difficulty {r['difficulty']}")
        if r["image_file"] and not os.path.exists(os.path.join(DATA, "images", r["image_file"])):
            problems.append(f"{r['source_key']}: missing image {r['image_file']}")
    tests = sorted({int(r["test_number"]) for r in module1})
    for n in tests:
        for s, cfg in SECTIONS.items():
            count = sum(1 for r in module1 if int(r["test_number"]) == n and r["section"] == s)
            if count != cfg["length"]:
                problems.append(f"test {n} {s}: {count} Module 1 questions, expected {cfg['length']}")
    return tests, problems


def ensure_schema():
    """Create the new table/columns on first run (same code the backend runs at startup)."""
    from init_db import init_db
    init_db()


def upload_images(needed_files):
    import supabase_storage
    if not supabase_storage.is_configured():
        sys.exit("SUPABASE_SERVICE_KEY is not set, so images can't be uploaded. "
                 "Set it (same value as on Render) and run again.")
    urls = {}
    for i, name in enumerate(sorted(needed_files), 1):
        with open(os.path.join(DATA, "images", name), "rb") as f:
            url = supabase_storage.upload_image(f.read(), path=f"practice-tests/{name}", upsert=True)
        if not url:
            sys.exit(f"Upload failed for {name}. Check SUPABASE_SERVICE_KEY / SUPABASE_PROJECT_REF "
                     "and that the 'question-images' bucket exists and is public.")
        urls[name] = url
        print(f"  image {i}/{len(needed_files)}  {name}")
    return urls


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="check the data files only")
    args = ap.parse_args()

    module1 = read_csv("module1_questions.csv")
    pool = read_csv("module2_pool.csv")
    tests, problems = check_data(module1, pool)
    print(f"Data: {len(tests)} tests, {len(module1)} Module 1 questions, {len(pool)} pool questions, "
          f"{len({r['image_file'] for r in module1 + pool if r['image_file']})} images")
    if problems:
        print("Problems in the data files:\n  " + "\n  ".join(problems))
        sys.exit(1)
    if args.dry_run:
        print("Dry run OK - nothing was changed.")
        return

    from database import SessionLocal, Test, Question, PoolQuestion
    from adaptive import SECTIONS
    import module2_builder

    ensure_schema()
    db = SessionLocal()
    try:
        existing_keys = {k for (k,) in db.query(Question.source_key).filter(Question.source_key.isnot(None))}
        existing_pool = {k for (k,) in db.query(PoolQuestion.source_key)}
        new_m1 = [r for r in module1 if r["source_key"] not in existing_keys]
        new_pool = [r for r in pool if r["source_key"] not in existing_pool]
        print(f"New to import: {len(new_m1)} Module 1 questions, {len(new_pool)} pool questions")

        print("Uploading images...")
        urls = upload_images({r["image_file"] for r in new_m1 + new_pool if r["image_file"]})

        # Module 1, one test at a time
        for n in tests:
            rows = sorted([r for r in new_m1 if int(r["test_number"]) == n],
                          key=lambda r: (r["section"] != "reading_writing", int(r["position"])))
            if not rows:
                continue
            title = f"SAT Practice Test {n}"
            test = db.query(Test).filter(Test.title == title).order_by(Test.id).first()
            if test is not None and db.query(Question).filter(
                    Question.test_id == test.id, Question.source_key.is_(None)).first():
                # a different, older test already uses this name — don't mix questions into it
                title = f"{title} (Adaptive)"
                test = db.query(Test).filter(Test.title == title).order_by(Test.id).first()
            if test is None:
                test = Test(title=title, description="Adaptive digital SAT practice test")
                db.add(test)
                db.flush()
            if not module2_builder.has_module2(db, test.id):
                test.is_published = False
            for r in rows:
                db.add(Question(
                    test_id=test.id,
                    subject=SECTIONS[r["section"]]["module1_subject"],
                    module_variant=None,
                    text=r["text"], passage=blank_to_none(r["passage"]),
                    choice_a=r["choice_a"], choice_b=r["choice_b"],
                    choice_c=r["choice_c"], choice_d=r["choice_d"],
                    correct_answer=r["correct_answer"],
                    explanation=blank_to_none(r["explanation"]),
                    difficulty=r["difficulty"], question_type=r["question_type"],
                    skill=blank_to_none(r["skill"]),
                    image_url=urls.get(r["image_file"]) if r["image_file"] else None,
                    source_key=r["source_key"],
                ))
            print(f"  {title} (id {test.id}): {len(rows)} Module 1 questions")
        db.commit()

        # Module 2 pool
        for r in new_pool:
            db.add(PoolQuestion(
                source_key=r["source_key"], source_label=r["source_key"].split("-")[0],
                source_position=int(r["source_position"]), section=r["section"],
                difficulty=r["difficulty"], question_type=r["question_type"],
                passage=blank_to_none(r["passage"]), text=r["text"],
                choice_a=blank_to_none(r["choice_a"]), choice_b=blank_to_none(r["choice_b"]),
                choice_c=blank_to_none(r["choice_c"]), choice_d=blank_to_none(r["choice_d"]),
                correct_answer=r["correct_answer"], explanation=blank_to_none(r["explanation"]),
                skill=blank_to_none(r["skill"]),
                image_url=urls.get(r["image_file"]) if r["image_file"] else None,
            ))
        db.commit()
        print(f"  Module 2 pool: {len(new_pool)} questions added")

        print("Building Lower/Higher Module 2s...")
        for test_id, title, result in module2_builder.build_pending(db):
            print(f"  {title} (id {test_id}): {result}")

        print("\nModule 2 pool (used / unused):")
        for row in module2_builder.pool_summary(db):
            print(f"  {row['section']:16s} {row['difficulty']:6s}  {row['used']:3d} used  {row['unused']:3d} unused")
        print("\nDone. Tests marked READY are live; the rest stay hidden until the pool has enough "
              "questions (add them, then POST /admin/module2/build or run this script again).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
