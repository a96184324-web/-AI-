import os
import io
from flask import Flask, request, abort
from PIL import Image
from google import genai
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, MessagingApiBlob, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message_cls=ImageMessageContent)
def handle_image(event):
    with ApiClient(configuration) as api_client:
        blob_api = MessagingApiBlob(api_client)
        image_bytes = blob_api.get_message_content(message_id=event.message.id)
        image = Image.open(io.BytesIO(image_bytes))

        prompt = (
            "送られた画像（出馬表や競馬新聞）を解析し、以下の形式で競馬予想を出力してください。\n"
            "1. 展開予想（ハイペース/ミドル/スローなどの見立て）\n"
            "2. 本命（◎）、対抗（◯）、単穴（▲）、穴馬（☆）の推奨馬とその理由\n"
            "3. おすすめの買い目（馬連・3連複など）\n"
            "語尾は丁寧かつ分かりやすく伝えてください。"
        )
        
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[image, prompt]
        )

        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=response.text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
