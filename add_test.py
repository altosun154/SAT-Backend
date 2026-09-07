import sys
from database import SessionLocal, Test, Question
from test_file_parser import parse_test_file

def main():
    if len(sys.argv) != 2:
        print("Usage: python add_test.py <path_to_test_file.pdf|.docx>")
        sys.exit(1)

    path = sys.argv[1]
    data = parse_test_file(path)

    test_name = data.get("test_name")
    questions = data.get("questions", [])

    if not test_name:
        print("Error: JSON must have a 'test_name' field.")
        sys.exit(1)

    db = SessionLocal()
    try:
        test = Test(title=test_name)
        db.add(test)
        db.flush()  # assigns test.id before committing
        print(f"Created test '{test_name}' (id={test.id})")

        for i, q in enumerate(questions, start=1):
            db.add(Question(
                test_id=test.id,
                text=q.get("text", ""),
                choice_a=q.get("choice_a", ""),
                choice_b=q.get("choice_b", ""),
                choice_c=q.get("choice_c", ""),
                choice_d=q.get("choice_d", ""),
                correct_answer=q.get("correct_answer", ""),
                subject=q.get("subject"),
                difficulty=q.get("difficulty"),
                passage=q.get("passage"),
                image_url=q.get("image_url"),
                skill=q.get("skill"),
                explanation=q.get("explanation"),
                module_variant=q.get("module_variant"),
            ))
            print(f"  [{i}/{len(questions)}] Added: {q.get('text', '')[:60]}")

        db.commit()
        print(f"\nDone. {len(questions)} question(s) added to test id={test.id}.")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    main()
