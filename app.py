# app.py

import time
import os
import logging
from datetime import datetime
from functools import wraps
from typing import Tuple, List, Dict, Any
from chatbot.twilio.handlers import handle_incoming_message, initialize_conversation
from flask import Flask, request, jsonify, redirect, url_for, Response, send_from_directory
from calendar_module.auth import authenticate, oauth2callback
from calendar_module.calendar_service import CalendarService
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
import pytz
from werkzeug.middleware.proxy_fix import ProxyFix
import threading
from pymongo import MongoClient
from calendar_module.auth import load_credentials
import uuid  # Added for UUID generation
from flask_cors import CORS
from flask import Flask, send_from_directory
from chatbot.conversation import scheduler
from chatbot.constants import ConversationState
from werkzeug.utils import secure_filename
import pandas as pd
import csv
import io

# Load environment variables from .env
load_dotenv()

# Configure logging with more detailed format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Environment variables with defaults
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
API_KEY = os.getenv('API_KEY')
ENVIRONMENT = os.getenv('ENVIRONMENT', 'development')
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))

if not TWILIO_AUTH_TOKEN:
    logger.error("TWILIO_AUTH_TOKEN environment variable is missing!")
    raise ValueError("TWILIO_AUTH_TOKEN is required")

validator = RequestValidator(TWILIO_AUTH_TOKEN)

# Initialize Flask application
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False  # Preserve JSON response order
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max-limit

# Initialize CORS
CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:3000", "http://localhost:8080"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "x-api-key", "Authorization"],
        "expose_headers": ["Content-Type", "x-api-key"],
        "supports_credentials": False,
        "send_wildcard": False
    }
})
# Register the oauth2callback route
app.add_url_rule('/oauth2callback', 'oauth2callback', oauth2callback, methods=['GET'])

# Configure ProxyFix based on environment
if ENVIRONMENT == 'production':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

# Custom error handler for 404
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Resource not found'}), 404

# Custom error handler for 500
@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal Server Error: {str(error)}")
    return jsonify({'error': 'Internal server error'}), 500

# Decorator for API key authentication
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('x-api-key')
        if not api_key or api_key != API_KEY:
            logger.warning("Unauthorized API access attempt")
            return jsonify({"error": "Unauthorized access"}), 401
        return f(*args, **kwargs)
    return decorated_function

def validate_csv_headers(headers: List[str]) -> Tuple[bool, str]:
    """Validate that all required headers are present in the CSV."""
    required_headers = {
        'interviewer_name', 'interviewer_number', 'interviewer_email',
        'interviewee_name', 'interviewee_number', 'interviewee_email',
        'jd_title', 'meeting_duration', 'superior_flag',
        'role_to_contact_name', 'role_to_contact_number', 'role_to_contact_email',
        'company_details'
    }
    
    missing_headers = required_headers - set(headers)
    if missing_headers:
        return False, f"Missing required headers: {', '.join(missing_headers)}"
    return True, ""

def process_csv_data(csv_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Process CSV data into the format required by the initialize endpoint.
    Groups interviewees by interviewer.
    """
    conversations = {}
    
    for row in csv_data:
        interviewer_key = (
            row['interviewer_name'],
            row['interviewer_number'],
            row['interviewer_email']
        )
        
        # Create interviewee data
        interviewee = {
            'name': row['interviewee_name'],
            'number': row['interviewee_number'],
            'email': row['interviewee_email'],
            'jd_title': row['jd_title']
        }
        
        # Initialize conversation if new interviewer
        if interviewer_key not in conversations:
            conversations[interviewer_key] = {
                'interviewer_name': row['interviewer_name'],
                'interviewer_number': row['interviewer_number'],
                'interviewer_email': row['interviewer_email'],
                'superior_flag': row['superior_flag'],
                'meeting_duration': int(row['meeting_duration']),
                'role_to_contact_name': row['role_to_contact_name'],
                'role_to_contact_number': row['role_to_contact_number'],
                'role_to_contact_email': row['role_to_contact_email'],
                'company_details': row['company_details'],
                'interviewees': []
            }
        
        conversations[interviewer_key]['interviewees'].append(interviewee)
    
    return list(conversations.values())

# Helper function to validate timezone
def validate_timezone(timezone: str) -> bool:
    try:
        pytz.timezone(timezone)
        return True
    except pytz.UnknownTimeZoneError:
        return False

# Authenticate with Google Calendar on a separate background thread
def authenticate_google_calendar_background():
    def authenticate_task():
        while True:
            try:
                creds = load_credentials()
                if not creds.valid:
                    raise ValueError("Invalid credentials, re-authentication required.")
                logger.info("Google Calendar is already authenticated.")
                break  # Exit loop once authenticated
            except Exception as e:
                logger.warning(f"Google Calendar authentication required: {str(e)}")
                auth_url = authenticate()  # Generate the authentication URL
                logger.info(f"Please complete the Google Calendar authentication: {auth_url}")
                # Sleep and retry after some time if authentication is needed
                time.sleep(10)  # Retry every 10 seconds if not authenticated

    # Start the background thread
    auth_thread = threading.Thread(target=authenticate_task)
    auth_thread.daemon = True
    auth_thread.start()

# Initialize background thread for authentication
authenticate_google_calendar_background()

### API Endpoints ###

# Serve frontend in production
@app.route('/')
def serve_frontend():
    return send_from_directory('../frontend/dist', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('../frontend/dist', path)


@app.route('/api/test', methods=['GET'])
def test_endpoint() -> Tuple[Response, int]:
    """Simple test endpoint to verify that the API is running."""
    logger.info("Test endpoint was called")
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "environment": ENVIRONMENT
    }), 200

@app.route('/api/twilio-webhook', methods=['POST'])
def twilio_webhook() -> Response:
    """Handles incoming Twilio messages with improved signature validation."""
    # Get the original Twilio signature from the request
    twilio_signature = request.headers.get('X-TWILIO-SIGNATURE', '')

    # Get the raw form data
    post_data = request.form.to_dict()
    message_sid = post_data.get('MessageSid')

    # Check if the request has been processed before using MessageSid
    if not message_sid:
        logger.error("MessageSid missing in request.")
        return Response("Missing MessageSid", status=400)

    if message_sid in app.config.get('PROCESSED_SIDS', set()):
        logger.info(f"Duplicate request detected for MessageSid: {message_sid}")
        return Response("Duplicate request", status=200)

    # Add the MessageSid to processed set
    if 'PROCESSED_SIDS' not in app.config:
        app.config['PROCESSED_SIDS'] = set()
    app.config['PROCESSED_SIDS'].add(message_sid)

    # Construct the canonical URL that Twilio used for signing
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.headers.get('X-Forwarded-Host', request.host)

    # Construct the full URL keeping only the components Twilio uses for signing
    url = f"{proto}://{host}{request.path}"

    # Validate the signature
    is_valid = validator.validate(
        url,
        post_data,
        twilio_signature
    )

    if not is_valid:
        logger.warning(
            "Invalid Twilio signature\n"
            f"URL: {url}\n"
            f"Signature: {twilio_signature}\n"
            f"Post Data: {post_data}"
        )
        return Response("Invalid signature", status=403)

    # If signature is valid, process the message
    return handle_incoming_message(request)

@app.route('/api/upload-csv', methods=['POST'])
@require_api_key
def upload_csv():
    """Handle CSV file upload and initialize conversations."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.endswith('.csv'):
        return jsonify({'error': 'File must be a CSV'}), 400
    
    try:
        # Read CSV content
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_data = list(csv.DictReader(stream))
        
        # Validate headers
        if not csv_data:
            return jsonify({'error': 'CSV file is empty'}), 400
        
        headers_valid, error_message = validate_csv_headers(csv_data[0].keys())
        if not headers_valid:
            return jsonify({'error': error_message}), 400
        
        # Process CSV data into conversations
        conversations = process_csv_data(csv_data)
        
        # Initialize conversations using existing endpoint logic
        results = []
        for conv_data in conversations:
            try:
                conversation_id = initialize_conversation(
                    interviewer_name=conv_data['interviewer_name'],
                    interviewer_number=conv_data['interviewer_number'],
                    interviewer_email=conv_data['interviewer_email'],
                    interviewees_data=conv_data['interviewees'],
                    superior_flag=conv_data['superior_flag'],
                    meeting_duration=conv_data['meeting_duration'],
                    role_to_contact_name=conv_data['role_to_contact_name'],
                    role_to_contact_number=conv_data['role_to_contact_number'],
                    role_to_contact_email=conv_data['role_to_contact_email'],
                    company_details=conv_data['company_details']
                )
                results.append({
                    'interviewer': conv_data['interviewer_name'],
                    'status': 'success',
                    'conversation_id': conversation_id,
                    'interviewee_count': len(conv_data['interviewees'])
                })
            except Exception as e:
                results.append({
                    'interviewer': conv_data['interviewer_name'],
                    'status': 'failed',
                    'error': str(e)
                })
        
        return jsonify({
            'message': 'CSV processed successfully',
            'conversations_created': len([r for r in results if r['status'] == 'success']),
            'conversations_failed': len([r for r in results if r['status'] == 'failed']),
            'results': results
        }), 200
        
    except Exception as e:
        logger.error(f"Error processing CSV: {str(e)}")
        return jsonify({'error': f'Error processing CSV: {str(e)}'}), 500

@app.route('/api/initialize', methods=['POST'])
@require_api_key
def initialize() -> Tuple[Response, int]:
    """Initializes multiple new interview scheduling conversations with validation."""
    data = request.json

    if not data or 'conversations' not in data:
        return jsonify({"error": "Invalid request format. 'conversations' field is required."}), 400

    conversations = data['conversations']
    if not isinstance(conversations, list) or not conversations:
        return jsonify({"error": "'conversations' must be a non-empty list."}), 400

    results = []
    for idx, convo in enumerate(conversations):
        # Define required fields for the conversation
        required_fields = {
            'interviewer_name': str,
            'interviewer_number': str,
            'interviewer_email': str,
            'interviewees': list,
            'superior_flag': str,
            'meeting_duration': int,
            'role_to_contact_name': str,
            'role_to_contact_number': str,
            'role_to_contact_email': str,
            'company_details': str
        }

        # Check for missing required fields in the conversation
        missing_fields = [field for field in required_fields if field not in convo]
        if missing_fields:
            results.append({
                "index": idx,
                "status": "failed",
                "error": f"Missing required fields: {', '.join(missing_fields)}"
            })
            continue

        # Validate interviewees list
        interviewees = convo['interviewees']
        if not isinstance(interviewees, list) or not interviewees:
            results.append({
                "index": idx,
                "status": "failed",
                "error": "interviewees must be a non-empty list."
            })
            continue

        # Define required fields for each interviewee, including 'jd_title'
        required_interviewee_fields = ['name', 'number', 'email', 'jd_title']

        # Check for missing required fields in each interviewee
        invalid_interviewees = [
            ie for ie in interviewees
            if not all(k in ie for k in required_interviewee_fields)
        ]
        if invalid_interviewees:
            results.append({
                "index": idx,
                "status": "failed",
                "error": f"Each interviewee must have {', '.join(required_interviewee_fields)}."
            })
            continue

        try:
            # Initialize each conversation
            conversation_id = initialize_conversation(
                interviewer_name=convo['interviewer_name'],
                interviewer_number=convo['interviewer_number'],
                interviewer_email=convo['interviewer_email'],
                interviewees_data=convo['interviewees'],
                superior_flag=convo['superior_flag'],
                meeting_duration=convo['meeting_duration'],
                role_to_contact_name=convo['role_to_contact_name'],
                role_to_contact_number=convo['role_to_contact_number'],
                role_to_contact_email=convo['role_to_contact_email'],
                company_details=convo['company_details']
            )

            logger.info(f"Initialized conversation {conversation_id} with {len(convo['interviewees'])} interviewees.")

            results.append({
                "index": idx,
                "status": "success",
                "conversation_id": conversation_id
            })

        except Exception as e:
            logger.error(f"Error initializing conversation at index {idx}: {str(e)}")
            results.append({
                "index": idx,
                "status": "failed",
                "error": str(e)
            })

    return jsonify({"results": results}), 200

@app.route('/api/create_event/<conversation_id>', methods=['POST'])
@require_api_key
def api_create_event_endpoint(conversation_id: str) -> Tuple[Response, int]:
    """Creates a Google Calendar event for a specific interviewee."""
    logger.info(f"Creating calendar event for conversation {conversation_id}")

    data = request.get_json()
    interviewee_number = data.get('interviewee_number')
    if not interviewee_number:
        return jsonify({"error": "Missing interviewee_number in request body"}), 400

    try:
        # Instantiate CalendarService
        calendar_service = CalendarService()
        
        # Call the instance method with both arguments
        event_response, error = calendar_service.create_event(conversation_id, interviewee_number)
        
        if error:
            if error in ["No tokens found for the given conversation ID. Please authenticate.", "invalid_grant"]:
                auth_url = url_for('auth_schedule', conversation_id=conversation_id, _external=True)
                return redirect(auth_url)
            return jsonify({"error": error}), 400

        # Extract 'event_id' from event_response
        event_id = event_response.get('event_id')

        if not event_id:
            logger.error(f"Failed to retrieve event_id for conversation {conversation_id} and interviewee {interviewee_number}.")
            return jsonify({"error": "Failed to retrieve event ID"}), 500

        return jsonify({
            "message": "Event created successfully",
            "event": event_response.get('event', {}),
            "event_id": event_id,
            "created_at": datetime.now().isoformat(),
        }), 200

    except Exception as e:
        logger.error(f"Error creating calendar event: {str(e)}")
        return jsonify({"error": "Failed to create calendar event"}), 500

@app.route('/api/authenticate/<conversation_id>', methods=['GET'])
def auth_schedule(conversation_id: str) -> Response:
    """Initiates OAuth 2.0 flow with error handling."""
    try:
        authorization_url = authenticate(conversation_id)
        logger.info(f"Redirecting to OAuth2 consent screen for conversation {conversation_id}")
        return redirect(authorization_url)
    except Exception as e:
        logger.error(f"OAuth2 flow initialization error: {str(e)}")
        return jsonify({"error": "Authentication initialization failed"}), 500
    
@app.route('/api/conversations/active', methods=['GET'])
@require_api_key
def get_active_conversations():
    """Get all active conversations."""
    print("Received request for active conversations")  # Debug log
    print("Headers:", request.headers)  # Debug log
    try:
        active_conversations = []
        for conversation in scheduler.conversations.values():
            if any(ie['state'] != ConversationState.SCHEDULED.value 
                  and ie['state'] != ConversationState.CANCELLED.value 
                  for ie in conversation['interviewees']):
                active_conversations.append({
                    'id': conversation['conversation_id'],
                    'interviewer_name': conversation['interviewer']['name'],
                    'interviewer_email': conversation['interviewer']['email'],
                    'interviewer_number': conversation['interviewer']['number'],
                    'interviewees': [{
                        'id': str(idx),
                        'name': ie['name'],
                        'email': ie['email'],
                        'number': ie['number'],
                        'status': ie['state']
                    } for idx, ie in enumerate(conversation['interviewees'])],
                    'status': 'active',
                    'last_activity': conversation.get('last_response_times', {}).get(
                        conversation['interviewer']['number'],
                        datetime.now().isoformat()
                    )
                })
        return jsonify(active_conversations), 200
    except Exception as e:
        logger.error(f"Error fetching active conversations: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/interviews/scheduled', methods=['GET'])
@require_api_key
def get_scheduled_interviews():
    """Get all scheduled interviews."""
    try:
        scheduled_interviews = []
        for conversation in scheduler.conversations.values():
            for interviewee in conversation['interviewees']:
                if (interviewee['state'] == ConversationState.SCHEDULED.value 
                    and interviewee.get('scheduled_slot')):
                    scheduled_interviews.append({
                        'id': str(uuid.uuid4()),
                        'title': f"Interview with {interviewee['name']}",
                        'interviewer_name': conversation['interviewer']['name'],
                        'interviewer_email': conversation['interviewer']['email'],
                        'interviewer_number': conversation['interviewer']['number'],
                        'interviewee_name': interviewee['name'],
                        'interviewee_email': interviewee['email'],
                        'interviewee_number': interviewee['number'],
                        'scheduled_time': interviewee['scheduled_slot']['start_time'],
                        'status': 'scheduled'
                    })
        return jsonify(scheduled_interviews), 200
    except Exception as e:
        logger.error(f"Error fetching scheduled interviews: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/attention-flags', methods=['GET'])
@require_api_key
def get_attention_flags():
    """Get all attention flags."""
    try:
        flags = []
        current_time = datetime.now(pytz.UTC)
        for conversation in scheduler.conversations.values():
            flags_dict = scheduler.evaluator.evaluate_conversation_flags(
                conversation, current_time
            )
            for participant_id, participant_flags in flags_dict.items():
                for flag in participant_flags:
                    flags.append({
                        'id': str(uuid.uuid4()),
                        'conversation_id': conversation['conversation_id'],
                        'message': f"Attention required for {participant_id}: {flag.value}",
                        'severity': 'high',
                        'created_at': datetime.now().isoformat(),
                        'resolved': False
                    })
        return jsonify(flags), 200
    except Exception as e:
        logger.error(f"Error fetching attention flags: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/attention-flags/<flag_id>/resolve', methods=['POST'])
@require_api_key
def resolve_attention_flag(flag_id):
    """Mark an attention flag as resolved."""
    try:
        # In a real implementation, you would update the flag status in your database
        return jsonify({'message': 'Flag resolved successfully'}), 200
    except Exception as e:
        logger.error(f"Error resolving attention flag: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

# Health check endpoint
@app.route('/api/health', methods=['GET'])
def health_check() -> Tuple[Response, int]:
    """Comprehensive health check endpoint."""
    try:
        # Check MongoDB connection
        MONGODB_URI = os.getenv("MONGODB_URI")
        client = MongoClient(MONGODB_URI)
        client.admin.command('ping')  # Check connection
        client.close()

        return jsonify({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "environment": ENVIRONMENT,
            "database": "connected",
            "version": "1.0.0"
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            "status": "unhealthy",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e)
        }), 500

if __name__ == "__main__":
    port = int(os.getenv('PORT', 5000))
    debug = ENVIRONMENT == 'development'

    # Additional security headers middleware
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    app.run(host="0.0.0.0", port=port, debug=debug)
