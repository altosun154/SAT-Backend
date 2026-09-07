from database import SessionLocal, Response


# ===== FORMATTING FUNCTIONS =====

def format_response_for_frontend(response_dict):
    """
    Format a single response object for frontend consumption.

    Args:
        response_dict: Dictionary containing response data

    Returns:
        Formatted response dictionary
    """
    return {
        "id": response_dict.get("id"),
        "question_id": response_dict.get("question_id"),
        "selected_answer": response_dict.get("selected_answer"),
        "is_correct": response_dict.get("is_correct"),
        "time_spent_seconds": response_dict.get("time_spent_seconds"),
        "answered_at": response_dict.get("answered_at")
    }
