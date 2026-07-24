import io
import logging
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

logging.basicConfig(level=logging.INFO)

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
  except Exception as e:
    logging.error(f'Error handling webhook: {e}')
  return 'OK'


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
  with ApiClient(configuration) as api_client:
    messaging_api = MessagingApi(api_client)

    try:
      # LINEから画像を取得
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=event.message.id)
      image = Image.open(io.BytesIO(image_bytes))

      # 解像度を最適化（認識精度を落とさず通信を最速化）
      image.thumbnail((2048, 2048))

      prompt = (
          '送られた画像（出馬表など）を解析し、まずは【開催競馬場】を特定してください。\n\n'
          '【重要事項：ハルシネーション禁止 ＆ クレジット削減】\n'
          '・画像から読み取れる事実のみを使用し、存在しないデータを作るハルシネーションは絶対に禁止します。\n'
          '・内部的なデータ分析と考察は深く論理的に行ってください。\n'
          '・APIのクレジット（トークン）消費を抑えつつ「LINEでの視認性」を最優先にするため、無駄な挨拶は省き、指定のフォーマット通りに出力してください。箇条書きの「*」記号は文字が詰まるため使用禁止です。\n\n'
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
          '【出力フォーマット】\n'
          '※以下のレイアウト通りに、各項目の間には必ず「1行の空白行（改行）」を入れて、スマホで読みやすく出力してください。\n\n'
          '■ 1. 競馬場と展開\n'
          '（ここに簡潔な予想を記載）\n\n'
          '■ 2. 印と推奨理由\n'
          '◎ 〇番 馬名 : （理由を1文程度で。札幌の場合はルール合致点を簡潔に）\n'
          '◯ 〇番 馬名 : （理由）\n'
          '▲ 〇番 馬名 : （理由）\n'
          '☆ 〇番 馬名 : （理由）\n\n'
          '■ 3. おすすめの買い目\n'
          '（ここに買い目を記載）\n\n'
          '※馬券購入は自己責任'
      )

      # 正常に動作確認できた最新モデルを継続使用
      candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
      reply_text = None
      error_logs = []

      for model_name in candidate_models:
        try:
          response = ai_client.models.generate_content(
              model=model_name, contents=[image, prompt]
          )
          if response and response.text:
            reply_text = response.text
            break
        except Exception as m_err:
          error_logs.append(f'[{model_name}] {m_err}')
          continue

      if not reply_text:
        err_str = " / ".join(error_logs)
        reply_text = f'⚠️ AIサーバーでエラーが発生しました。\n詳細: {err_str}'

    except Exception as e:
      logging.error(f'System error: {e}')
      reply_text = f'⚠️ システムエラーが発生しました: {str(e)}'

    # LINEへ返信
    try:
      messaging_api.reply_message(
          ReplyMessageRequest(
              reply_token=event.reply_token,
              messages=[TextMessage(text=reply_text)],
          )
      )
    except Exception as line_err:
      logging.error(f'Failed to send LINE reply: {line_err}')

if __name__ == '__main__':
  port = int(os.environ.get('PORT', 5000))
  app.run(host='0.0.0.0', port=port)
