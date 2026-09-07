import json
import os

JSON_FILE = "sat_questions.json"
IMAGES_DIR = "static/images"

MODULE_SLUGS = {
    "Reading Module 1": "reading_module1",
    "Reading Module 2": "reading_module2",
    "Math Module 1":    "math_module1",
    "Math Module 2":    "math_module2",
}


def rename_images():
    with open(JSON_FILE, "r") as f:
        data = json.load(f)

    renamed = 0
    skipped = 0
    errors = 0

    for q in data["questions"]:
        if not q.get("image_url"):
            continue

        module_slug = MODULE_SLUGS.get(q["module"])
        if not module_slug:
            print(f"  [WARN] Unknown module '{q['module']}' — skipping")
            skipped += 1
            continue

        current_filename = os.path.basename(q["image_url"])
        ext = os.path.splitext(current_filename)[1]  # e.g. .png or .jpg
        new_filename = f"q{q['question_number']}_{module_slug}{ext}"

        src = os.path.join(IMAGES_DIR, current_filename)
        dst = os.path.join(IMAGES_DIR, new_filename)

        if not os.path.exists(src):
            print(f"  [MISSING] {src}")
            errors += 1
            continue

        if src == dst:
            print(f"  [SKIP] Already named correctly: {new_filename}")
            skipped += 1
            continue

        if os.path.exists(dst):
            print(f"  [SKIP] Destination already exists: {dst}")
            skipped += 1
            continue

        os.rename(src, dst)
        print(f"  [OK] {current_filename} -> {new_filename}")
        renamed += 1

    print(f"\nDone. Renamed: {renamed} | Skipped: {skipped} | Missing: {errors}")


if __name__ == "__main__":
    rename_images()
