import datetime
import io
import json
import logging
import os
from flask import Flask, abort, request
from google import genai
from google.genai import types
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
import requests

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# GASのウェブアプリURL（埋め込み済み）
GAS_WEBAPP_URL = 'https://script.google.com/macros/s/AKfycbwsDCHtrbNNFTVbuOPlbTiSwqyNx5YHhiAfgWpcYjGbk1S26NsjL3J4-oheMRs5MWl4/exec'

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

BABA_FILE = 'baba_info.json'


def get_jst_today():
  """日本時間の現在日付（YYYY-MM-DD）を取得"""
  jst = datetime.timezone(datetime.timedelta(hours=9))
  return datetime.datetime.now(jst).strftime('%Y-%m-%d')


def send_prediction_to_gas(prediction_text):
  """予想データを裏でGAS（スプレッドシート）へ自動送信する関数"""
  if not GAS_WEBAPP_URL:
    logging.warning('GAS_WEBAPP_URLが設定されていないため自動保存をスキップします。')
    return

  try:
    payload = {'date': get_jst_today(), 'prediction_text': prediction_text}
    response = requests.post(
        GAS_WEBAPP_URL,
        data=json.dumps(payload),
        headers={'Content-Type': 'application/json'},
        timeout=10,
    )
    logging.info(f'GAS Save Response: {response.status_code}')
  except Exception as e:
    logging.error(f'Failed to send prediction to GAS: {e}')


def load_baba_data():
  """保存されている馬場情報を読み込み、日付が古ければリセットする"""
  if os.path.exists(BABA_FILE):
    try:
      with open(BABA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        if data.get('date') != get_jst_today():
          return {}
        return data.get('data', {})
    except Exception as e:
      logging.error(f'Failed to load baba file: {e}')
  return {}


def save_baba_data(baba_dict):
  """馬場情報を現在日付とともにファイルに書き込む"""
  try:
    data_to_save = {'date': get_jst_today(), 'data': baba_dict}
    with open(BABA_FILE, 'w', encoding='utf-8') as f:
      json.dump(data_to_save, f, ensure_ascii=False, indent=2)
  except Exception as e:
    logging.error(f'Failed to save baba file: {e}')


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
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=event.message.id)
      image = Image.open(io.BytesIO(image_bytes))
      image.thumbnail((2048, 2048))

      candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
      deterministic_config = types.GenerateContentConfig(temperature=0.0)

      classify_prompt = (
          '送られた画像を判定してください。\n'
          'JRAなどの「馬場情報（天候、芝・ダートの馬場状態）」の画面であれば "BABA" と答えてください。\n'
          'それ以外の「出馬表」や「レース情報」であれば "RACE" と答えてください。\n'
          '回答は "BABA" または "RACE" の英字1単語のみにしてください。'
      )

      image_type = 'RACE'
      for model_name in candidate_models:
        try:
          res = ai_client.models.generate_content(
              model=model_name, contents=[image, classify_prompt], config=deterministic_config
          )
          if res and res.text:
            if 'BABA' in res.text.strip().upper():
              image_type = 'BABA'
            break
        except Exception:
          continue

      if image_type == 'BABA':
        extract_prompt = (
            'この馬場情報画像から【競馬場名】、【天候】、【芝の馬場状態】、【ダートの馬場状態】を抽出し、以下のJSON形式のみで出力してください。\n'
            '{"keibajo": "新潟", "tenko": "雨", "shiba": "稍重", "dirt": "重"}\n'
            '※JSON以外の挨拶や解説文は一切含めないでください。'
        )

        baba_json = None
        for model_name in candidate_models:
          try:
            res = ai_client.models.generate_content(
                model=model_name, contents=[image, extract_prompt], config=deterministic_config
            )
            if res and res.text:
              raw_text = res.text.replace('```json', '').replace('```', '').strip()
              baba_json = json.loads(raw_text)
              break
          except Exception:
            continue

        if baba_json and 'keibajo' in baba_json:
          current_data = load_baba_data()
          keibajo = baba_json['keibajo']
          current_data[keibajo] = {
              'tenko': baba_json.get('tenko', '不明'),
              'shiba': baba_json.get('shiba', '不明'),
              'dirt': baba_json.get('dirt', '不明'),
          }
          save_baba_data(current_data)

          reply_text = (
              '【本日の馬場情報を更新・記憶しました】\n'
              f'📍 競馬場：【{keibajo}競馬場】\n'
              f'🌤 天候：{baba_json.get("tenko", "不明")}\n'
              f'🌿 芝：{baba_json.get("shiba", "不明")}\n'
              f'🟫 ダート：{baba_json.get("dirt", "不明")}\n\n'
              '※日付が変わると自動でリセットされます。\n'
              '※本日この競馬場の出馬表が送られた際、血統適性や有利な脚質に自動反映します！'
          )
        else:
          reply_text = '⚠️ 馬場情報の読み取りに失敗しました。もう一度はっきり映った画像を送信してください。'

      else:
        baba_data = load_baba_data()
        baba_context_str = '【現在システムに記憶されている本日の競馬場別リアルタイム馬場情報】\n'
        if baba_data:
          for k, v in baba_data.items():
            baba_context_str += f'・[{k}競馬場] ➔ 天候:{v.get("tenko")} / 芝:{v.get("shiba")} / ダート:{v.get("dirt")}\n'
          baba_context_str += '※【超厳格指示】必ず出馬表画像から解析した「開催競馬場名」と100%名称が合致するデータ『のみ』を参照してください。合致するデータがない場合は標準の「良馬場」として判定してください。\n\n'
        else:
          baba_context_str += '・登録なし（馬場情報画像未送信のため標準の「良馬場」として判定）\n\n'

        prompt = (
            '送られた画像（出馬表など）を解析し、まずは【開催競馬場】と【距離・馬場】を正確に特定してください。\n\n'
            + baba_context_str +
            '【絶対厳守事項：ハルシネーション禁止 ＆ 人気・オッズ完全無視】\n'
            '1. 【数字の超厳格読み取り】：文字認識の精度を上げるため、予想を始める前に必ず画像内の過去成績にある「着差（カッコ内の数字）」「通過順（ハイフン区切りの数字）」「上がり3F（3F 〇〇.〇）」を慎重に視認し、見間違いがないか確認してから評価に移行してください。\n'
            '2. 【馬番・馬名・騎手名の紐付け】：画像から「馬番」「馬名」「騎手名」を正しく読み取り、「〇番 馬名（騎手名）」の形式で必ず出力してください。読めない騎手名の捏造は厳禁です。\n'
            '3. 【印と買い目の完全一致ルール】：『■ 3. おすすめの買い目』に含まれるすべての馬番は、必ず『■ 2. 印・期待度と推奨理由』の中に◎・◯・▲・☆・△（連下）のいずれかの印をつけて掲載してください。買い目にしか登場しない印なしの謎の馬番が存在することは絶対に禁止します。\n'
            '4. オッズや人気順は一切考慮せず、出馬表の事実のみに基づく【条件合致度（100点満点）】を期待度（%）として絶対評価で算出してください。\n'
            '5. 出力の最後の行には、省略せず必ず「※馬券購入は自己責任でお願いします」と記載してください。\n\n'
            '【★的中精度を上げる3つの追加ロジック（数字データの徹底活用）】\n'
            '①【着差による実力評価】：前走の着順が4着以下と悪くても、1着馬との着差（カッコ内の数字）が「0.5秒以内」であれば、実力拮抗とみなし大幅な減点はせず、展開次第で巻き返し可能として評価を上げること。\n'
            '②【上がり3F×コース形態】：過去3走以内で上がり3F「34秒台以下（3F 34.X）」を記録している馬は末脚が優秀である。東京・新潟外・阪神外など直線が長いコースではこれを大加点せよ。逆に札幌・函館など直線が短いコースでは、末脚不発のリスクを考慮し過信しないこと。\n'
            '③【通過順によるペース判定】：過去成績の「通過順（例: 2-2-2-2）」の最初の数字を確認せよ。出走馬の中で「1」や「2」が少なく、特定の馬だけが常に前に行けている場合、「単騎逃げ・先行有利なスローペース展開」と判定し、その先行馬を高く評価すること。\n\n'
            '【全競馬場共通・基本チェックリスト】\n'
            '・ダート戦×枠順：ダート戦で内枠（1〜3枠）を高評価にするのは「逃げ・先行馬」限定。出足が遅い内枠の馬は砂被りリスクとして割引。\n'
            '・前走からの変動要素：斤量の大幅減は加点、極端な距離延長・短縮は慎重評価。\n'
            '・騎手と厩舎：減量記号（▲等）の恩恵、継続騎乗などを加点。\n\n'
            '【条件分岐ルール】\n'
            '◆開催競馬場が「札幌」の場合のみ、以下のルールを最優先すること。\n'
            '　・芝1200m：「1枠・最内」先行馬重視。\n'
            '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
            '　・ダ1700m：最後までバテない「逃げ・先行馬」一択。\n'
            '◆札幌以外の場合は、コース形態からフラットに考察すること。\n\n'
            '【出力フォーマット】\n'
            '※以下のレイアウトを厳守し、冒頭のレース情報には必ず【開催場・コース種別・距離】を明確に記載してください。\n\n'
            '■ 1. レース概要：【〇〇 芝/ダ〇〇〇m】 [レースの堅実度：A〜C]\n'
            '（展開予想と馬場状態・コース形態の影響を記載。※追加ロジック③の通過順から読み取ったペース予想もここに含めること）\n\n'
            '■ 2. 印・期待度と推奨理由\n'
            '◎ 【本命】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で。※追加ロジック①の着差、②の3Fタイムなどを具体的に根拠として含めること）\n\n'
            '◯ 【対抗】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で）\n\n'
            '▲ 【単穴】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で）\n\n'
            '☆ 【穴馬】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で）\n\n'
            '△ 【連下】 〇番 馬名（騎手名） [期待度：〇% / 評価：B〜C]\n'
            '（理由を1文程度で。※買い目（相手や3列目など）に含める馬を漏れなく1〜3頭程度記載すること）\n\n'
            '■ 3. おすすめの買い目\n'
            '【選定券種（例：馬連・馬単 など）】\n'
            '軸馬：〇\n'
            '相手：〇, 〇, 〇\n\n'
            '【選定券種（例：3连複フォーメーション など）】\n'
            '1列目（または1着）：〇\n'
            '2列目（または2着）：〇, 〇\n'
            '3列目（または3着）：〇, 〇, 〇, 〇\n\n'
            '※馬券購入は自己責任でお願いします'
        )

        error_logs = []
        for model_name in candidate_models:
          try:
            response = ai_client.models.generate_content(
                model=model_name, contents=[image, prompt], config=deterministic_config
            )
            if response and response.text:
              reply_text = response.text
              # 予想成功時にGAS（スプレッドシート）へ自動送信
              send_prediction_to_gas(reply_text)
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
