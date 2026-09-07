from database import SessionLocal, Question

# Your Supabase project ref (found in Supabase → Project Settings → General)
PROJECT_REF = "rgtpylhsewekyepcgxde"  # replace with your actual ref e.g. "abcdefghijklmnop"
BASE_STORAGE_URL = f"https://{PROJECT_REF}.supabase.co/storage/v1/object/public/question-images/"

def sync_image_urls():
    db = SessionLocal()
    try:
        # Get all questions from your DB
        questions = db.query(Question).all()
        print(f"Found {len(questions)} questions. Starting sync...")
        
        for q in questions:
            image_filename = f"question_image_{q.id}.png"
            full_url = f"{BASE_STORAGE_URL}{image_filename}"
            q.image_url = full_url
            print(f"Linked Question #{q.id} -> {image_filename}")
        
        db.commit()
        print("\n✅ Database updated! Check your Supabase Table Editor.")
        
    except Exception as e:
        print(f"❌ Error during sync: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    sync_image_urls()