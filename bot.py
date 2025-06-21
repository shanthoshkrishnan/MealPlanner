from flask import Flask, request, jsonify
import google.generativeai as genai
import os
import requests
from PIL import Image
import io
import json
import logging
import time
from typing import Dict, Any
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
WHATSAPP_TOKEN = os.getenv('WHATSAPP_TOKEN')  # Your WhatsApp Business API token
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID')  # Your WhatsApp Business phone number ID
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")
if not WHATSAPP_TOKEN:
    raise ValueError("WHATSAPP_TOKEN environment variable is required")
if not WHATSAPP_PHONE_NUMBER_ID:
    raise ValueError("WHATSAPP_PHONE_NUMBER_ID environment variable is required")
if not WEBHOOK_VERIFY_TOKEN:
    raise ValueError("WEBHOOK_VERIFY_TOKEN environment variable is required")

# Configure Gemini API
genai.configure(api_key=GEMINI_API_KEY)

class NutritionAnalyzer:
    def __init__(self):
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        
    def analyze_image(self, image: Image.Image) -> str:
        """Analyze food image and return nutrition information as formatted text"""
        
        prompt = """
        Analyze this food image and provide detailed nutritional information for one serving. 
        Please provide a clear, easy-to-read response with the following information:

        🍽️ **DISH IDENTIFICATION**
        - Name and description of the dish
        - Type of cuisine
        - Confidence level in identification

        📏 **SERVING SIZE**
        - Estimated serving size and weight

        🔥 **NUTRITION FACTS (per serving)**
        - Calories
        - Protein, Carbohydrates, Fat, Fiber, Sugar (in grams)
        - Key vitamins and minerals

        💪 **HEALTH ANALYSIS**
        - Overall health score (1-10)
        - Nutritional strengths
        - Areas of concern

        💡 **IMPROVEMENT SUGGESTIONS**
        - Ways to make it healthier
        - Foods to add or reduce
        - Better cooking methods

        🚨 **DIETARY INFORMATION**
        - Potential allergens
        - Suitable for: Vegetarian/Vegan/Gluten-free/etc.

        Please format your response in a clear, conversational way that's easy to read on a mobile device.
        If you cannot clearly identify the food, please indicate this and provide your best assessment.
        """
        
        try:
            response = self.model.generate_content([prompt, image])
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini analysis error: {e}")
            return f"❌ Sorry, I couldn't analyze this image. Please try again with a clearer photo of your food. Error: {str(e)}"

class WhatsAppBot:
    def __init__(self, token: str, phone_number_id: str):
        self.token = token
        self.phone_number_id = phone_number_id
        self.base_url = f"https://graph.facebook.com/v17.0/{phone_number_id}"
        
    def send_message(self, to: str, message: str):
        """Send text message to WhatsApp user"""
        url = f"{self.base_url}/messages"  # Fixed: Added /messages to the URL
        
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
        
        try:
            response = requests.post(url, headers=headers, json=data)
            if response.status_code == 200:
                logger.info(f"Message sent successfully to {to}")
                return True
            else:
                logger.error(f"Failed to send message: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
    
    def download_media(self, media_id: str) -> bytes:
        """Download media file from WhatsApp"""
        try:
            # Get media URL
            url = f"https://graph.facebook.com/v17.0/{media_id}"
            headers = {'Authorization': f'Bearer {self.token}'}
            
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Failed to get media URL: {response.status_code}")
            
            media_data = response.json()
            media_url = media_data.get('url')
            
            if not media_url:
                raise Exception("No media URL found")
            
            # Download the actual media file
            media_response = requests.get(media_url, headers=headers)
            if media_response.status_code != 200:
                raise Exception(f"Failed to download media: {media_response.status_code}")
            
            return media_response.content
            
        except Exception as e:
            logger.error(f"Error downloading media {media_id}: {e}")
            raise e

# Initialize components
analyzer = NutritionAnalyzer()
whatsapp_bot = WhatsAppBot(WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """Verify WhatsApp webhook"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == WEBHOOK_VERIFY_TOKEN:  # Updated variable name
        logger.info("Webhook verified successfully")
        return challenge
    else:
        logger.warning("Webhook verification failed")
        return 'Verification failed', 403

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    """Handle incoming WhatsApp messages"""
    try:
        data = request.get_json()
        logger.info(f"Received webhook data: {json.dumps(data, indent=2)}")
        
        # Check if this is a WhatsApp message
        if not data.get('object') == 'whatsapp_business_account':
            return jsonify({'status': 'ignored'}), 200
        
        entries = data.get('entry', [])
        for entry in entries:
            changes = entry.get('changes', [])
            for change in changes:
                value = change.get('value', {})
                messages = value.get('messages', [])
                
                for message in messages:
                    process_message(message)
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

def process_message(message: Dict[str, Any]):
    """Process individual WhatsApp message"""
    try:
        message_type = message.get('type')
        sender = message.get('from')
        message_id = message.get('id')
        
        logger.info(f"Processing {message_type} message from {sender}")
        
        if message_type == 'image':
            # Handle image message
            image_data = message.get('image', {})
            media_id = image_data.get('id')
            caption = image_data.get('caption', '')
            
            if not media_id:
                whatsapp_bot.send_message(sender, "❌ Sorry, I couldn't receive your image. Please try sending it again.")
                return
            
            # Send initial response
            whatsapp_bot.send_message(sender, "🔍 Analyzing your food image... This may take a few moments.")
            
            try:
                # Download the image
                image_bytes = whatsapp_bot.download_media(media_id)
                
                # Convert to PIL Image
                image = Image.open(io.BytesIO(image_bytes))
                image = image.convert('RGB')  # Ensure RGB format
                
                # Resize if too large
                max_size = (2048, 2048)
                if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
                    image.thumbnail(max_size, Image.Resampling.LANCZOS)
                
                # Analyze the image
                analysis_result = analyzer.analyze_image(image)
                
                # Send the analysis result
                whatsapp_bot.send_message(sender, analysis_result)
                
                logger.info(f"Successfully analyzed image for {sender}")
                
            except Exception as e:
                logger.error(f"Error processing image from {sender}: {e}")
                error_message = (
                    "❌ Sorry, I couldn't analyze your image. Please make sure:\n"
                    "• The image shows food clearly\n"
                    "• The image is not too dark or blurry\n"
                    "• Try taking a photo from directly above the food\n\n"
                    "Please try again with a clearer photo!"
                )
                whatsapp_bot.send_message(sender, error_message)
        
        elif message_type == 'text':
            # Handle text message
            text_content = message.get('text', {}).get('body', '').lower().strip()
            
            if any(greeting in text_content for greeting in ['hi', 'hello', 'hey', 'start']):
                welcome_message = (
                    "👋 Hello! I'm your AI Nutrition Analyzer bot!\n\n"
                    "📸 Send me a photo of any food and I'll provide:\n"
                    "• Detailed nutritional information\n"
                    "• Calorie count and macros\n"
                    "• Health analysis and tips\n"
                    "• Improvement suggestions\n\n"
                    "Just take a clear photo of your meal and send it to me! 🍽️"
                )
                whatsapp_bot.send_message(sender, welcome_message)
            
            elif 'help' in text_content:
                help_message = (
                    "🆘 **How to use this bot:**\n\n"
                    "1. Take a clear photo of your food\n"
                    "2. Send the image to me\n"
                    "3. Wait for the analysis (usually 10-30 seconds)\n"
                    "4. Get detailed nutrition information!\n\n"
                    "**Tips for best results:**\n"
                    "• Take photos in good lighting\n"
                    "• Show the food clearly from above\n"
                    "• Include the whole serving if possible\n"
                    "• One dish per photo works best\n\n"
                    "Send me a food photo to get started! 📸"
                )
                whatsapp_bot.send_message(sender, help_message)
            
            else:
                instruction_message = (
                    "📸 Please send me a photo of your food for nutrition analysis!\n\n"
                    "I can analyze any food image and provide detailed nutritional information, "
                    "including calories, macros, health tips, and improvement suggestions.\n\n"
                    "Type 'help' if you need assistance! 🤖"
                )
                whatsapp_bot.send_message(sender, instruction_message)
        
        else:
            # Handle other message types
            unsupported_message = (
                "🤖 I can only analyze food images right now.\n\n"
                "📸 Please send me a photo of your food, and I'll provide detailed nutrition analysis!\n\n"
                "Type 'help' if you need assistance."
            )
            whatsapp_bot.send_message(sender, unsupported_message)
            
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        try:
            whatsapp_bot.send_message(
                message.get('from', ''), 
                "❌ Sorry, something went wrong. Please try again later."
            )
        except:
            pass  # Ignore if we can't even send error message

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'WhatsApp Nutrition Bot',
        'timestamp': time.time()
    })

@app.route('/test-message', methods=['POST'])
def test_message():
    """Test endpoint to send a message (for development)"""
    try:
        data = request.get_json()
        phone_number = data.get('phone_number')
        message = data.get('message', 'Test message from Nutrition Bot!')
        
        if not phone_number:
            return jsonify({'error': 'phone_number is required'}), 400
        
        success = whatsapp_bot.send_message(phone_number, message)
        
        if success:
            return jsonify({'status': 'Message sent successfully'})
        else:
            return jsonify({'error': 'Failed to send message'}), 500
            
    except Exception as e:
        logger.error(f"Test message error: {e}")
        return jsonify({'error': str(e)}), 500

@app.errorhandler(500)
def internal_error(e):
    """Handle internal server errors"""
    logger.error(f"Internal server error: {e}")
    return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(404)
def not_found(e):
    """Handle not found errors"""
    return jsonify({'error': 'Endpoint not found'}), 404

if __name__ == '__main__':
    logger.info("Starting WhatsApp Nutrition Bot...")
    logger.info("Bot is ready to receive food images for nutrition analysis!")
    
    # In production, use a proper WSGI server like Gunicorn
    app.run(host='0.0.0.0', port=5000, debug=False)
