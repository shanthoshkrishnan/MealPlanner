from flask import Flask, request, jsonify
import google.generativeai as genai
import os
import base64
from PIL import Image
import io
import json
from typing import Dict, Any, Optional
import logging
import time
from datetime import datetime, timedelta
from collections import defaultdict, deque
import threading
from dataclasses import dataclass, asdict
import sqlite3
from contextlib import contextmanager
import hashlib
import uuid
from pathlib import Path
import requests
import hmac

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# WhatsApp Cloud API Configuration
WHATSAPP_TOKEN = os.getenv('WHATSAPP_TOKEN')  # Your WhatsApp Business API token
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')  # Your WhatsApp Business phone number ID
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN', 'nutrition_bot_webhook_token')
WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
    logger.warning("WhatsApp credentials not found. WhatsApp functionality will be disabled.")

# Configure Gemini API
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")

genai.configure(api_key=GEMINI_API_KEY)

# Configuration for file storage
UPLOAD_FOLDER = 'uploads'  # Fixed folder name
ANALYSIS_FOLDER = 'analysis_results'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ANALYSIS_FOLDER, exist_ok=True)

@dataclass
class RequestMetrics:
    """Store metrics for a single request"""
    timestamp: datetime
    request_id: str
    client_ip: str
    endpoint: str
    tokens_used: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    response_time_ms: float
    success: bool
    error_message: Optional[str] = None

@dataclass
class RateLimitConfig:
    """Rate limiting configuration"""
    max_tokens_per_request: int = 100000
    min_tokens_per_request: int = 1
    tokens_per_minute: int = 60000
    requests_per_minute: int = 60
    requests_per_day: int = 1000
    burst_requests: int = 10
    burst_tokens: int = 10000

@dataclass
class AnalysisRecord:
    """Data structure for analysis records"""
    id: str
    timestamp: datetime
    client_ip: str
    image_filename: str
    image_hash: str
    analysis_filename: str
    dish_name: str
    calories: int
    confidence: str
    file_size_bytes: int
    image_dimensions: str

class WhatsAppHandler:
    """Handle WhatsApp Cloud API interactions"""
    
    def __init__(self, token: str, phone_number_id: str):
        self.token = token
        self.phone_number_id = phone_number_id
        self.api_url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
        self.media_url = "https://graph.facebook.com/v18.0"
        
    def send_message(self, to: str, message: str) -> bool:
        """Send a text message via WhatsApp"""
        try:
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            }
            
            data = {
                'messaging_product': 'whatsapp',
                'to': to,
                'type': 'text',
                'text': {'body': message}
            }
            
            response = requests.post(self.api_url, headers=headers, json=data)
            response.raise_for_status()
            
            logger.info(f"Message sent successfully to {to}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send message to {to}: {e}")
            return False
    
    def send_formatted_nutrition_info(self, to: str, nutrition_data: dict) -> bool:
        """Send formatted nutrition information"""
        try:
            dish_info = nutrition_data.get('dish_identification', {})
            nutritional_info = nutrition_data.get('nutritional_information', {})
            health_analysis = nutrition_data.get('health_analysis', {})
            
            message = f"🍽️ *{dish_info.get('name', 'Unknown Dish')}*\n\n"
            message += f"📊 *Nutritional Information (per serving):*\n"
            message += f"• Calories: {nutritional_info.get('calories', 'N/A')} kcal\n"
            
            macros = nutritional_info.get('macronutrients', {})
            if macros:
                message += f"• Protein: {macros.get('protein_g', 'N/A')}g\n"
                message += f"• Carbs: {macros.get('carbohydrates_g', 'N/A')}g\n"
                message += f"• Fat: {macros.get('fat_g', 'N/A')}g\n"
                message += f"• Fiber: {macros.get('fiber_g', 'N/A')}g\n\n"
            
            if health_analysis:
                score = health_analysis.get('overall_score', 'N/A')
                message += f"🏥 *Health Score:* {score}/10\n\n"
                
                strengths = health_analysis.get('strengths', [])
                if strengths:
                    message += "✅ *Strengths:*\n"
                    for strength in strengths[:3]:  # Limit to 3 items
                        message += f"• {strength}\n"
                    message += "\n"
                
                concerns = health_analysis.get('concerns', [])
                if concerns:
                    message += "⚠️ *Concerns:*\n"
                    for concern in concerns[:3]:  # Limit to 3 items
                        message += f"• {concern}\n"
                    message += "\n"
            
            # Add suggestions
            suggestions = nutrition_data.get('improvement_suggestions', {})
            if suggestions:
                modifications = suggestions.get('modifications', [])
                if modifications:
                    message += "💡 *Suggestions:*\n"
                    for mod in modifications[:2]:  # Limit to 2 suggestions
                        message += f"• {mod}\n"
            
            message += "\n📱 Send another food photo for analysis!"
            
            return self.send_message(to, message)
            
        except Exception as e:
            logger.error(f"Failed to send formatted nutrition info: {e}")
            return self.send_message(to, "Sorry, I encountered an error while formatting the nutrition information. Please try again.")
    
    def download_media(self, media_id: str) -> Optional[bytes]:
        """Download media from WhatsApp"""
        try:
            # First, get media URL
            headers = {'Authorization': f'Bearer {self.token}'}
            media_info_response = requests.get(f"{self.media_url}/{media_id}", headers=headers)
            media_info_response.raise_for_status()
            
            media_info = media_info_response.json()
            media_url = media_info.get('url')
            
            if not media_url:
                logger.error("No URL found in media info")
                return None
            
            # Download the actual media
            media_response = requests.get(media_url, headers=headers)
            media_response.raise_for_status()
            
            return media_response.content
            
        except Exception as e:
            logger.error(f"Failed to download media {media_id}: {e}")
            return None

class ImageAnalysisStorage:
    """Handles storage of images and analysis results"""
    
    def __init__(self, upload_folder: str = UPLOAD_FOLDER, analysis_folder: str = ANALYSIS_FOLDER):
        self.upload_folder = Path(upload_folder)
        self.analysis_folder = Path(analysis_folder)
        self.upload_folder.mkdir(exist_ok=True)
        self.analysis_folder.mkdir(exist_ok=True)
        self._init_storage_database()
    
    def _init_storage_database(self):
        """Initialize database for image and analysis storage tracking with migration support"""
        try:
            with self._get_db() as conn:
                # Create table with all columns
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS image_analyses (
                        id TEXT PRIMARY KEY,
                        timestamp DATETIME,
                        client_ip TEXT,
                        image_filename TEXT,
                        image_hash TEXT UNIQUE,
                        analysis_filename TEXT,
                        dish_name TEXT,
                        calories INTEGER,
                        confidence TEXT,
                        file_size_bytes INTEGER,
                        image_dimensions TEXT,
                        original_filename TEXT,
                        content_type TEXT,
                        analysis_json TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Check if whatsapp_number column exists and add it if it doesn't
                cursor = conn.execute("PRAGMA table_info(image_analyses)")
                columns = [row[1] for row in cursor.fetchall()]
                
                if 'whatsapp_number' not in columns:
                    logger.info("Adding whatsapp_number column to existing database")
                    conn.execute('ALTER TABLE image_analyses ADD COLUMN whatsapp_number TEXT')
                
                # Create indices
                conn.execute('CREATE INDEX IF NOT EXISTS idx_image_hash ON image_analyses(image_hash)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON image_analyses(timestamp)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_whatsapp_number ON image_analyses(whatsapp_number)')
                
                logger.info("Database initialized successfully")
                
        except Exception as e:
            logger.error(f"Storage database initialization error: {e}")
            raise e
    
    @contextmanager
    def _get_db(self):
        """Database connection context manager"""
        conn = sqlite3.connect('nutrition_storage.db', timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def calculate_image_hash(self, image_data: bytes) -> str:
        """Calculate SHA-256 hash of image data for deduplication"""
        return hashlib.sha256(image_data).hexdigest()
    
    def save_image_and_analysis(self, image: Image.Image, analysis_data: dict, 
                               client_ip: str, whatsapp_number: str = None) -> str:
        """Save image and analysis results to storage"""
        
        # Generate unique ID for this analysis
        analysis_id = str(uuid.uuid4())
        timestamp = datetime.now()
        
        # Convert image to bytes for hashing and storage
        img_buffer = io.BytesIO()
        image.save(img_buffer, format='JPEG', quality=95)
        image_data = img_buffer.getvalue()
        
        # Calculate hash for deduplication
        image_hash = self.calculate_image_hash(image_data)
        
        # Generate filenames
        timestamp_str = timestamp.strftime('%Y%m%d_%H%M%S')
        image_filename = f"{timestamp_str}_{analysis_id}.jpg"
        analysis_filename = f"{timestamp_str}_{analysis_id}_analysis.json"
        
        # Save image file
        image_path = self.upload_folder / image_filename
        with open(image_path, 'wb') as f:
            f.write(image_data)
        
        # Save analysis file
        analysis_path = self.analysis_folder / analysis_filename
        with open(analysis_path, 'w', encoding='utf-8') as f:
            json.dump(analysis_data, f, indent=2, ensure_ascii=False)
        
        # Extract key information for database
        dish_name = analysis_data.get('dish_identification', {}).get('name', 'Unknown')
        calories = analysis_data.get('nutritional_information', {}).get('calories', 0)
        confidence = analysis_data.get('dish_identification', {}).get('confidence', 'unknown')
        
        # Store record in database
        try:
            with self._get_db() as conn:
                conn.execute('''
                    INSERT INTO image_analyses 
                    (id, timestamp, client_ip, whatsapp_number, image_filename, image_hash, 
                     analysis_filename, dish_name, calories, confidence, 
                     file_size_bytes, image_dimensions, analysis_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    analysis_id, timestamp, client_ip, whatsapp_number,
                    image_filename, image_hash, analysis_filename,
                    dish_name, calories, confidence,
                    len(image_data), f"{image.size[0]}x{image.size[1]}",
                    json.dumps(analysis_data)
                ))
            
            logger.info(f"Saved analysis {analysis_id} for client {client_ip}")
            return analysis_id
            
        except Exception as e:
            # Clean up files if database save fails
            try:
                image_path.unlink(missing_ok=True)
                analysis_path.unlink(missing_ok=True)
            except:
                pass
            raise e

class RateLimitTracker:
    """Thread-safe rate limit tracking"""
    
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.lock = threading.Lock()
        self.client_requests = defaultdict(lambda: deque())
        self.client_tokens = defaultdict(lambda: deque())
        
    def estimate_tokens(self, text: str, image_size: tuple = None) -> dict:
        """Estimate token usage for input"""
        text_tokens = len(text.split()) * 1.3
        image_tokens = 0
        if image_size:
            pixels = image_size[0] * image_size[1]
            image_tokens = min(pixels // 1000, 2000)
        
        return {
            'estimated_input_tokens': int(text_tokens + image_tokens),
            'estimated_output_tokens': 500
        }
    
    def check_rate_limits(self, client_id: str, estimated_tokens: int) -> Dict[str, Any]:
        """Check if request should be allowed based on rate limits"""
        with self.lock:
            now = datetime.now()
            
            # Clean up old entries
            minute_ago = now - timedelta(minutes=1)
            self.client_requests[client_id] = deque([
                t for t in self.client_requests[client_id] if t > minute_ago
            ])
            
            # Check rate limits
            if len(self.client_requests[client_id]) >= self.config.requests_per_minute:
                return {
                    'allowed': False,
                    'reason': 'requests_per_minute',
                    'message': 'Too many requests. Please wait a moment before sending another image.'
                }
            
            return {'allowed': True}
    
    def record_request(self, client_id: str, tokens_used: int):
        """Record a successful request"""
        with self.lock:
            self.client_requests[client_id].append(datetime.now())

class NutritionAnalyzer:
    def __init__(self, rate_tracker: RateLimitTracker, storage: ImageAnalysisStorage):
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        self.rate_tracker = rate_tracker
        self.storage = storage
        
    def analyze_image(self, image: Image.Image, client_id: str, whatsapp_number: str = None) -> Dict[str, Any]:
        """Analyze food image and return nutrition information"""
        
        prompt = """
        Analyze this food image and provide detailed nutritional information for one serving. 
        Return your response as a JSON object with the following structure:
        
        {
            "dish_identification": {
                "name": "name of the dish",
                "description": "brief description",
                "cuisine_type": "type of cuisine",
                "confidence": "high/medium/low"
            },
            "serving_size": {
                "description": "estimated serving size",
                "weight_grams": estimated_weight_in_grams
            },
            "nutritional_information": {
                "calories": total_calories,
                "macronutrients": {
                    "protein_g": protein_in_grams,
                    "carbohydrates_g": carbs_in_grams,
                    "fat_g": fat_in_grams,
                    "fiber_g": fiber_in_grams,
                    "sugar_g": sugar_in_grams
                },
                "micronutrients": {
                    "sodium_mg": sodium_in_mg,
                    "potassium_mg": potassium_in_mg,
                    "calcium_mg": calcium_in_mg,
                    "iron_mg": iron_in_mg,
                    "vitamin_c_mg": vitamin_c_in_mg,
                    "vitamin_a_mcg": vitamin_a_in_mcg
                }
            },
            "health_analysis": {
                "overall_score": score_out_of_10,
                "strengths": ["list", "of", "positive", "aspects"],
                "concerns": ["list", "of", "potential", "issues"]
            },
            "improvement_suggestions": {
                "modifications": ["specific", "changes", "to", "make"],
                "additions": ["foods", "to", "add"],
                "reductions": ["elements", "to", "reduce"]
            },
            "dietary_considerations": {
                "allergens": ["potential", "allergens"],
                "dietary_restrictions": {
                    "vegetarian": true/false,
                    "vegan": true/false,
                    "gluten_free": true/false,
                    "dairy_free": true/false,
                    "low_carb": true/false,
                    "keto_friendly": true/false
                }
            }
        }
        
        Be as accurate as possible in your estimates. If you cannot clearly identify the food, 
        indicate this in the confidence level and provide your best assessment.
        """
        
        try:
            response = self.model.generate_content([prompt, image])
            response_text = response.text.strip()
            
            # Extract JSON from response
            if '```json' in response_text:
                start = response_text.find('```json') + 7
                end = response_text.find('```', start)
                response_text = response_text[start:end].strip()
            elif '```' in response_text:
                start = response_text.find('```') + 3
                end = response_text.rfind('```')
                response_text = response_text[start:end].strip()
            
            nutrition_data = json.loads(response_text)
            
            # Save to storage
            try:
                analysis_id = self.storage.save_image_and_analysis(
                    image, nutrition_data, client_id, whatsapp_number
                )
                nutrition_data['analysis_id'] = analysis_id
            except Exception as e:
                logger.error(f"Failed to save analysis: {e}")
            
            # Record successful request
            token_estimates = self.rate_tracker.estimate_tokens(prompt, image.size)
            self.rate_tracker.record_request(client_id, token_estimates['estimated_input_tokens'])
            
            return nutrition_data
            
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {e}")
            raise ValueError(f"Failed to parse AI response as JSON: {e}")
            
        except Exception as e:
            logger.error(f"Analysis error: {e}")
            raise e

# Initialize components
rate_config = RateLimitConfig()
rate_tracker = RateLimitTracker(rate_config)
storage = ImageAnalysisStorage()
analyzer = NutritionAnalyzer(rate_tracker, storage)

# Initialize WhatsApp handler if credentials are available
whatsapp_handler = None
if WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID:
    whatsapp_handler = WhatsAppHandler(WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)

def get_client_ip():
    """Get client IP address from request"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP')
    else:
        return request.remote_addr

@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Verify webhook for WhatsApp"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return challenge
    else:
        logger.warning("Webhook verification failed")
        return 'Verification failed', 403

@app.route('/webhook', methods=['POST'])
def webhook_handler():
    """Handle incoming WhatsApp messages"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'status': 'no data'}), 200
        
        # Process webhook data
        if 'entry' in data:
            for entry in data['entry']:
                if 'changes' in entry:
                    for change in entry['changes']:
                        if change.get('field') == 'messages':
                            process_whatsapp_message(change.get('value', {}))
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify({'status': 'error'}), 200

def process_whatsapp_message(message_data):
    """Process incoming WhatsApp message"""
    try:
        if not whatsapp_handler:
            logger.error("WhatsApp handler not initialized")
            return
        
        messages = message_data.get('messages', [])
        
        for message in messages:
            message_type = message.get('type')
            from_number = message.get('from')
            message_id = message.get('id')
            
            logger.info(f"Processing {message_type} message from {from_number}")
            
            if message_type == 'image':
                # Handle image message
                threading.Thread(
                    target=handle_image_message,
                    args=(message, from_number),
                    daemon=True
                ).start()
                
            elif message_type == 'text':
                # Handle text message
                text_body = message.get('text', {}).get('body', '').lower()
                
                if any(word in text_body for word in ['help', 'start', 'hello', 'hi']):
                    welcome_message = (
                        "🍽️ Welcome to Nutrition Analyzer Bot!\n\n"
                        "📸 Send me a photo of your food and I'll analyze:\n"
                        "• Calories and nutrients\n"
                        "• Health score\n"
                        "• Improvement suggestions\n"
                        "• Dietary information\n\n"
                        "Just send a clear photo of your meal to get started!"
                    )
                    whatsapp_handler.send_message(from_number, welcome_message)
                else:
                    whatsapp_handler.send_message(
                        from_number,
                        "Please send a photo of your food for nutritional analysis. 📸"
                    )
            
    except Exception as e:
        logger.error(f"Error processing WhatsApp message: {e}")

def handle_image_message(message, from_number):
    """Handle image message from WhatsApp"""
    try:
        if not whatsapp_handler:
            return
        
        # Check rate limits
        rate_check = rate_tracker.check_rate_limits(from_number, 2000)
        if not rate_check['allowed']:
            whatsapp_handler.send_message(from_number, rate_check['message'])
            return
        
        # Send processing message
        whatsapp_handler.send_message(
            from_number,
            "🔍 Analyzing your food image... This may take a moment."
        )
        
        # Get image data
        image_data = message.get('image', {})
        media_id = image_data.get('id')
        
        if not media_id:
            whatsapp_handler.send_message(
                from_number,
                "❌ Sorry, I couldn't access the image. Please try sending it again."
            )
            return
        
        # Download image from WhatsApp
        image_bytes = whatsapp_handler.download_media(media_id)
        if not image_bytes:
            whatsapp_handler.send_message(
                from_number,
                "❌ Failed to download the image. Please try again."
            )
            return
        
        # Process image
        image = Image.open(io.BytesIO(image_bytes))
        image = image.convert('RGB')
        
        # Resize if too large
        max_size = (2048, 2048)
        if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Analyze image
        nutrition_data = analyzer.analyze_image(image, from_number, from_number)
        
        # Send formatted response
        success = whatsapp_handler.send_formatted_nutrition_info(from_number, nutrition_data)
        
        if not success:
            whatsapp_handler.send_message(
                from_number,
                "✅ Analysis completed, but I had trouble formatting the response. Please try again."
            )
        
        logger.info(f"Successfully processed image from {from_number}")
        
    except Exception as e:
        logger.error(f"Error handling image from {from_number}: {e}")
        if whatsapp_handler:
            whatsapp_handler.send_message(
                from_number,
                "❌ Sorry, I encountered an error while analyzing your image. Please try again with a clear photo of your food."
            )

@app.route('/analyze', methods=['POST'])
def analyze_food():
    """Analyze food image via HTTP POST"""
    try:
        client_ip = get_client_ip()
        
        # Check if request has file upload
        if 'image' not in request.files:
            return jsonify({'error': 'No image file provided'}), 400
        
        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No image file selected'}), 400
        
        # Check rate limits
        rate_check = rate_tracker.check_rate_limits(client_ip, 2000)
        if not rate_check['allowed']:
            return jsonify({'error': rate_check['message']}), 429
        
        # Process uploaded image
        image = Image.open(file.stream)
        image = image.convert('RGB')
        
        # Resize if too large
        max_size = (2048, 2048)
        if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Analyze image
        nutrition_data = analyzer.analyze_image(image, client_ip)
        
        return jsonify({
            'success': True,
            'data': nutrition_data
        })
        
    except Exception as e:
        logger.error(f"Error in analyze_food: {e}")
        return jsonify({'error': 'Failed to analyze image'}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'whatsapp_enabled': whatsapp_handler is not None,
        'version': '2.0.0'
    })

@app.route('/send-test', methods=['POST'])
def send_test_message():
    """Test endpoint to send a message via WhatsApp"""
    if not whatsapp_handler:
        return jsonify({'error': 'WhatsApp not configured'}), 400
    
    data = request.get_json()
    to_number = data.get('to')
    message = data.get('message', 'Test message from Nutrition Bot!')
    
    if not to_number:
        return jsonify({'error': 'Phone number required'}), 400
    
    success = whatsapp_handler.send_message(to_number, message)
    
    return jsonify({
        'success': success,
        'message': 'Message sent' if success else 'Failed to send message'
    })

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get bot statistics"""
    try:
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT 
                    COUNT(*) as total_analyses,
                    COUNT(DISTINCT COALESCE(whatsapp_number, client_ip)) as unique_users,
                    AVG(CAST(calories AS FLOAT)) as avg_calories,
                    COUNT(CASE WHEN confidence = 'high' THEN 1 END) as high_confidence,
                    MIN(timestamp) as first_analysis,
                    MAX(timestamp) as latest_analysis,
                    COUNT(CASE WHEN whatsapp_number IS NOT NULL THEN 1 END) as whatsapp_analyses,
                    COUNT(CASE WHEN whatsapp_number IS NULL THEN 1 END) as web_analyses
                FROM image_analyses
            ''')
            stats = dict(cursor.fetchone())
        
        return jsonify({
            'bot_stats': stats,
            'whatsapp_enabled': whatsapp_handler is not None,
            'timestamp': datetime.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return jsonify({'error': 'Failed to retrieve statistics'}), 500

@app.route('/', methods=['GET'])
def home():
    """Home page with basic info"""
    return jsonify({
        'message': 'WhatsApp Nutrition Bot API',
        'version': '2.0.0',
        'status': 'running',
        'endpoints': {
            'webhook': '/webhook (GET/POST) - WhatsApp webhook',
            'analyze': '/analyze (POST) - Upload image for analysis',
            'health': '/health (GET) - Health check',
            'stats': '/stats (GET) - Bot statistics',
            'send-test': '/send-test (POST) - Test WhatsApp messaging'
        },
        'features': [
            'WhatsApp integration',
            'Food image analysis',
            'Nutritional information',
            'Health scoring',
            'Rate limiting',
            'Data storage',
            'Usage statistics'
        ],
        'whatsapp_enabled': whatsapp_handler is not None
    })

@app.route('/recent-analyses', methods=['GET'])
def get_recent_analyses():
    """Get recent analyses (last 50)"""
    try:
        limit = min(int(request.args.get('limit', 50)), 100)  # Max 100
        
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT 
                    id, timestamp, dish_name, calories, confidence,
                    COALESCE(whatsapp_number, 'web') as source,
                    file_size_bytes, image_dimensions
                FROM image_analyses 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (limit,))
            
            analyses = []
            for row in cursor.fetchall():
                analyses.append({
                    'id': row['id'],
                    'timestamp': row['timestamp'],
                    'dish_name': row['dish_name'],
                    'calories': row['calories'],
                    'confidence': row['confidence'],
                    'source': row['source'],
                    'file_size_kb': round(row['file_size_bytes'] / 1024, 1),
                    'dimensions': row['image_dimensions']
                })
        
        return jsonify({
            'success': True,
            'count': len(analyses),
            'analyses': analyses
        })
        
    except Exception as e:
        logger.error(f"Failed to get recent analyses: {e}")
        return jsonify({'error': 'Failed to retrieve analyses'}), 500

@app.route('/analysis/<analysis_id>', methods=['GET'])
def get_analysis_details(analysis_id):
    """Get detailed analysis by ID"""
    try:
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT * FROM image_analyses WHERE id = ?
            ''', (analysis_id,))
            
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': 'Analysis not found'}), 404
            
            # Parse the stored JSON analysis
            analysis_data = json.loads(row['analysis_json'])
            
            return jsonify({
                'success': True,
                'analysis': {
                    'id': row['id'],
                    'timestamp': row['timestamp'],
                    'source': 'whatsapp' if row['whatsapp_number'] else 'web',
                    'image_info': {
                        'filename': row['image_filename'],
                        'dimensions': row['image_dimensions'],
                        'file_size_kb': round(row['file_size_bytes'] / 1024, 1)
                    },
                    'nutrition_data': analysis_data
                }
            })
        
    except Exception as e:
        logger.error(f"Failed to get analysis {analysis_id}: {e}")
        return jsonify({'error': 'Failed to retrieve analysis'}), 500

@app.route('/popular-dishes', methods=['GET'])
def get_popular_dishes():
    """Get most analyzed dishes"""
    try:
        limit = min(int(request.args.get('limit', 20)), 50)
        
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT 
                    dish_name,
                    COUNT(*) as analysis_count,
                    AVG(CAST(calories AS FLOAT)) as avg_calories,
                    MIN(timestamp) as first_seen,
                    MAX(timestamp) as last_seen
                FROM image_analyses 
                WHERE dish_name != 'Unknown'
                GROUP BY LOWER(dish_name)
                ORDER BY analysis_count DESC
                LIMIT ?
            ''', (limit,))
            
            dishes = []
            for row in cursor.fetchall():
                dishes.append({
                    'dish_name': row['dish_name'],
                    'analysis_count': row['analysis_count'],
                    'avg_calories': round(row['avg_calories'], 1) if row['avg_calories'] else None,
                    'first_seen': row['first_seen'],
                    'last_seen': row['last_seen']
                })
        
        return jsonify({
            'success': True,
            'dishes': dishes
        })
        
    except Exception as e:
        logger.error(f"Failed to get popular dishes: {e}")
        return jsonify({'error': 'Failed to retrieve popular dishes'}), 500

@app.route('/user-stats/<user_id>', methods=['GET'])
def get_user_stats(user_id):
    """Get statistics for a specific user (WhatsApp number or IP)"""
    try:
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT 
                    COUNT(*) as total_analyses,
                    AVG(CAST(calories AS FLOAT)) as avg_calories,
                    MIN(timestamp) as first_analysis,
                    MAX(timestamp) as last_analysis,
                    COUNT(DISTINCT dish_name) as unique_dishes,
                    dish_name as most_common_dish
                FROM image_analyses 
                WHERE COALESCE(whatsapp_number, client_ip) = ?
                GROUP BY dish_name
                ORDER BY COUNT(*) DESC
                LIMIT 1
            ''', (user_id,))
            
            row = cursor.fetchone()
            if not row or row['total_analyses'] == 0:
                return jsonify({'error': 'No analyses found for this user'}), 404
            
            # Get recent analyses for this user
            cursor = conn.execute('''
                SELECT dish_name, calories, timestamp, confidence
                FROM image_analyses 
                WHERE COALESCE(whatsapp_number, client_ip) = ?
                ORDER BY timestamp DESC
                LIMIT 10
            ''', (user_id,))
            
            recent_analyses = []
            for analysis in cursor.fetchall():
                recent_analyses.append({
                    'dish_name': analysis['dish_name'],
                    'calories': analysis['calories'],
                    'timestamp': analysis['timestamp'],
                    'confidence': analysis['confidence']
                })
        
        return jsonify({
            'success': True,
            'user_stats': {
                'total_analyses': row['total_analyses'],
                'avg_calories': round(row['avg_calories'], 1) if row['avg_calories'] else None,
                'first_analysis': row['first_analysis'],
                'last_analysis': row['last_analysis'],
                'unique_dishes': row['unique_dishes'],
                'most_common_dish': row['most_common_dish']
            },
            'recent_analyses': recent_analyses
        })
        
    except Exception as e:
        logger.error(f"Failed to get user stats for {user_id}: {e}")
        return jsonify({'error': 'Failed to retrieve user statistics'}), 500

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors"""
    return jsonify({
        'error': 'Endpoint not found',
        'available_endpoints': [
            '/webhook', '/analyze', '/health', '/stats', 
            '/recent-analyses', '/popular-dishes', '/send-test'
        ]
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    logger.error(f"Internal server error: {error}")
    return jsonify({
        'error': 'Internal server error',
        'message': 'Something went wrong on our end'
    }), 500

@app.errorhandler(413)
def too_large(error):
    """Handle file too large errors"""
    return jsonify({
        'error': 'File too large',
        'message': 'Please upload an image smaller than 10MB'
    }), 413

@app.errorhandler(429)
def ratelimit_handler(error):
    """Handle rate limit errors"""
    return jsonify({
        'error': 'Rate limit exceeded',
        'message': 'Too many requests. Please try again later.'
    }), 429

def cleanup_old_files():
    """Clean up old image and analysis files (older than 30 days)"""
    try:
        cutoff_date = datetime.now() - timedelta(days=30)
        
        with storage._get_db() as conn:
            cursor = conn.execute('''
                SELECT image_filename, analysis_filename 
                FROM image_analyses 
                WHERE timestamp < ?
            ''', (cutoff_date,))
            
            old_files = cursor.fetchall()
            deleted_count = 0
            
            for row in old_files:
                try:
                    # Delete image file
                    image_path = storage.upload_folder / row['image_filename']
                    if image_path.exists():
                        image_path.unlink()
                    
                    # Delete analysis file
                    analysis_path = storage.analysis_folder / row['analysis_filename']
                    if analysis_path.exists():
                        analysis_path.unlink()
                    
                    deleted_count += 1
                    
                except Exception as e:
                    logger.error(f"Failed to delete files for {row['image_filename']}: {e}")
            
            # Clean up database records
            cursor = conn.execute('DELETE FROM image_analyses WHERE timestamp < ?', (cutoff_date,))
            db_deleted = cursor.rowcount
            
            logger.info(f"Cleanup completed: {deleted_count} files deleted, {db_deleted} database records removed")
            
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")

# Schedule cleanup to run periodically (you might want to use a proper scheduler like APScheduler)
def schedule_cleanup():
    """Schedule periodic cleanup"""
    cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
    cleanup_thread.start()
    
    # Schedule next cleanup in 24 hours
    cleanup_timer = threading.Timer(86400, schedule_cleanup)  # 24 hours = 86400 seconds
    cleanup_timer.daemon = True
    cleanup_timer.start()

if __name__ == '__main__':
    logger.info("Starting WhatsApp Nutrition Bot API")
    logger.info(f"WhatsApp integration: {'Enabled' if whatsapp_handler else 'Disabled'}")
    
    # Start cleanup scheduler
    schedule_cleanup()
    
    # Configure Flask app
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size
    
    # Run the app
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=debug,
        threaded=True
    )