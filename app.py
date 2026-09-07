from flask import Flask
from flask_cors import CORS
from routes_questions import questions_bp
from routes_responses import responses_bp
from routes_results import results_bp
from routes_activity import activity_data_bp
from routes_assignments import assignments_bp
from routes_parent import parent_bp
from routes_parse import parse_bp
from routes_imports import imports_bp
from auth import auth_bp
from admin import admin_bp
from activity import activity_bp
from init_db import init_db

app = Flask(__name__)
CORS(app, origins=[
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "https://digital-sat-testing-analytics-platform-hzo2.onrender.com"
])

app.register_blueprint(questions_bp)
app.register_blueprint(responses_bp)
app.register_blueprint(results_bp)
app.register_blueprint(activity_data_bp)
app.register_blueprint(assignments_bp)
app.register_blueprint(parent_bp)
app.register_blueprint(parse_bp)
app.register_blueprint(imports_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(activity_bp)

init_db()

if __name__ == "__main__":
    app.run(debug=True)
