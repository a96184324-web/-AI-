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
    reply_text = None

    try:
      # 1. LINEから画像を取得
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=event.message.id)
      image = Image.open(io.BytesIO(image_bytes))

      # 2. 認識精度を一切落とさずに転送速度を最適化（長辺2048px）
      image.thumbnail((2048, 2048))

      # 3. 徹底的に精度と視認性を高めたプロンプト
      prompt = (
          '送られた画像（出馬表など）を解析し、まずは【開催競馬場】を特定してください。\n\n'
          '【絶対厳守事項：ハルシネーション禁止 ＆ 人気・オッズ完全無視 ＆ トークン削減】\n'
          '1. 画像から読み取れる事実のみを使用し、存在しないデータや架空の数値をねつ造するハルシネーションは絶対に禁止します。\n'
          '2. オッズや人気順は一切考慮せず、純粋に出馬表の条件（枠順・脚質・コース適性など）の「条件適合度」のみで評価・期待度（%およびS〜B評価）を算出してください（人気薄の好条件馬を高評価すること）。\n'
          '3. 挨拶や無駄な装飾、冗長な説明を完全に省き、指定のフォーマット通りに極めて簡潔に出力してください。\n'
          '4. 視認性を保つため、箇条書きの「*」記号は絶対に使用しないでください。\n\n'
          '【条件分岐ルール】\n'
          '◆ '
          '開催競馬場が「札幌」の場合のみ、以下の【札幌開幕週・特別ルール】を最優先して予想を組み立てること。\n'
          '　・極端な【前・内有利】の高速決着。ロスなく運べる「内枠」「逃げ・先行馬」が圧倒的有利。\n'
          '　・100%洋芝（パワー必須）、ほぼ平坦、コーナーが緩く直線短い（大外一気は不可）。\n'
          '　・芝1200m：「1枠・最内」先行馬重視。\n'
          '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
          '　・ダ1700m：最後までバテない「逃げ・先行馬」一択。\n'
          '　・盲点：函館の急坂でバテたスピード馬の巻き返しを狙い、函館で勝ったスタミナ馬のスピード負けを警戒。\n'
          '　・買い方：「1〜3枠」の「逃げ・先行馬」軸。大外枠（7〜8枠）の差し・追い込み馬は人気でも消しや割引。\n\n'
          '◆ '
          '「札幌以外」の場合は、上記ルールを無視し、一般的な適性やコース形態・展開からフラットに深く論理的に考察すること。\n\n'
          '【出力フォーマット】\n'
          '※以下のレイアウトを厳守し、各項目の間には必ず「1行の空白行」を入れてください。\n\n'
          '■ 1. 競馬場と展開予想 [レースの堅実度：A〜C]\n'
          '（ここに簡潔な展開予想を記載）\n\n'
          '■ 2. 印・期待度と推奨理由\n'
          '◎ 【本命】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
          '（理由を1文程度で）\n\n'
          '◯ 【対抗】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
          '（理由を1文程度で）\n\n'
          '▲ 【単穴】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
          '（理由を1文程度で）\n\n'
          '☆ 【穴馬】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
          '（理由を1文程度で）\n\n'
          '■ 3. おすすめの買い目\n'
          '【馬連・馬単】\n'
          '軸馬：（馬番を記載）\n'
          '相手：（馬番を記載）\n\n'
          '【3連複フォーメーション】\n'
          '1列目：（馬番を記載）\n'
          '2列目：（馬番を記載）\n'
          '3列目：（馬番を記載）\n\n'
          '※馬券購入は自己責任'
      )

      # 安定動作するモデルのフォールバック構成
      candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
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
        err_str = ' / '.join(error_logs)
        reply_text = f'⚠️ AIサーバーでエラーが発生しました。\n詳細: {err_str}'

    except Exception as e:
      logging.error(f'System error: {e}')
      reply_text = f'⚠️ システムエラーが発生しました: {str(e)}'

    # LINEへ返信（タイムアウトやトークン切れを防ぐ堅牢な処理）
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
