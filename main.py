import io
import os
from flask import Flask, abort, request
from google import genai
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import ImageMessageContent, MessageEvent
from PIL import Image

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


@app.route('/callback', methods=['POST'])
def callback():
  signature = request.headers.get('X-Line-Signature', '')
  body = request.get_data(as_text=True)
  try:
    handler.handle(body, signature)
  except InvalidSignatureError:
    abort(400)
  return 'OK'


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
  with ApiClient(configuration) as api_client:
    messaging_api = MessagingApi(api_client)

    try:
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=event.message.id)
      image = Image.open(io.BytesIO(image_bytes))

      prompt = (
          '送られた画像（出馬表など）を解析し、まずは【開催競馬場】を特定してください。\n\n'
          '【重要事項：ハルシネーション禁止 ＆ クレジット削減】\n'
          '・画像から読み取れる事実のみを使用し、存在しないデータを作るハルシネーションは絶対に禁止します。\n'
          '・内部的なデータ分析と考察は深く論理的に行ってください。\n'
          '・APIのクレジット（トークン）消費を抑えるため、挨拶や無駄な装飾、冗長な説明を完全に省き、要点のみを極めて簡潔に表示してください。\n\n'
          '【条件分岐ルール】\n'
          '◆ '
          '開催競馬場が「札幌」の場合のみ、以下の【札幌開幕週・特別ルール】を最優先して予想を組み立てること。\n'
          '　・極端な「前・内有利」の高速決着。ロスなく運べる「内枠」「逃げ・先行馬」が圧倒的有利。\n'
          '　・100%洋芝（パワー必須）、ほぼ平坦、コーナーが緩く直線短い（大外一気は不可）。\n'
          '　・芝1200m：「1枠・最内」先行馬重視。\n'
          '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
          '　・ダ1700m：最後までバテない「逃げ・先行馬」一択。\n'
          '　・盲点：函館の急坂でバテたスピード馬の巻き返しを狙い、函館で勝ったスタミナ馬のスピード負けを警戒。\n'
          '　・買い方：「1〜3枠」の「逃げ・先行馬」軸。大外枠（7〜8枠）の差し・追い込み馬は人気でも消しや割引。\n\n'
          '◆ '
          '「札幌以外」の場合は、上記ルールを無視し、一般的な適性や展開からフラットに考察すること。\n\n'
          '【出力形式】（※各項目は短文・箇条書きで出力）\n'
          '1. 競馬場名と展開予想\n'
          '2. '
          '印（◎◯▲☆）と推奨理由（※各馬1文程度。札幌の場合は特別ルール合致点も簡潔に）\n'
          '3. おすすめの買い目\n'
          '※馬券購入は自己責任'
      )

      # 有効な現行モデルのみを指定（旧モデルgemini-2.0を排除）
      candidate_models = [
          'gemini-3.5-flash',
          'gemini-2.5-flash',
          'gemini-2.5-pro',
      ]
      reply_text = None
      last_error = None

      for model_name in candidate_models:
        try:
          response = ai_client.models.generate_content(
              model=model_name, contents=[image, prompt]
          )
          if response and response.text:
            reply_text = response.text
            break
        except Exception as model_err:
          last_error = model_err
          continue

      if not reply_text:
        reply_text = f'⚠️ AIモデルの呼び出しに失敗しました: {str(last_error)}'

    except Exception as e:
      reply_text = f'⚠️ 処理エラーが発生しました: {str(e)}'

    messaging_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)],
        )
    )


if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)
