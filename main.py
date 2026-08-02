import datetime
import io
import json
import logging
import os
import threading
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
from PIL import Image, ImageEnhance, ImageFilter
import requests

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

GAS_WEBAPP_URL = 'https://script.google.com/macros/s/AKfycbzfjZCmbso00IgujgFfi2KoGV-9JbnEv16FaoH8FSicJtzPA5kYdhohY2Mxn268xrMRvA/exec'

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

BABA_FILE = 'baba_info.json'

processed_message_ids = set()


def get_jst_today():
  """現在の日付（JST）を取得"""
  try:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime('%Y-%m-%d')
  except Exception as e:
    logging.error(f'Date error: {e}')
    return datetime.datetime.now().strftime('%Y-%m-%d')


def process_image_for_ocr(image):
  """
  【画質補正＆最安コスト両立関数】
  解像度を1280pxに最適化してトークン費用を最安に保ちつつ、
  コントラストと輪郭を自動強調して遠目スクショの文字認識率を向上。
  ※処理失敗時も元画像で安全フォールバック
  """
  try:
    img_copy = image.copy()
    # 1. 長辺1280pxにリサイズ（API最安・最高画質のバランス）
    img_copy.thumbnail((1280, 1280), Image.Resampling.LANCZOS)

    # 2. コントラスト強調（文字をくっきり黒く、背景を白く）
    enhancer = ImageEnhance.Contrast(img_copy)
    img_copy = enhancer.enhance(1.4)

    # 3. くっきり化（ボヤけた小さな文字の輪郭補正）
    img_copy = img_copy.filter(ImageFilter.SHARPEN)

    return img_copy
  except Exception as e:
    logging.error(f'Image enhancement failed: {e}')
    return image


def send_prediction_to_gas_async(prediction_text):
  """予想結果をGASへ非同期送信（エラー完全遮断）"""

  def _send():
    if not GAS_WEBAPP_URL:
      return
    try:
      payload = {'date': get_jst_today(), 'prediction_text': prediction_text}
      response = requests.post(
          GAS_WEBAPP_URL,
          data=json.dumps(payload),
          headers={'Content-Type': 'application/json'},
          timeout=15,
      )
      logging.info(f'GAS Save Response: {response.status_code}')
    except Exception as e:
      logging.error(f'Failed to send prediction to GAS: {e}')

  thread = threading.Thread(target=_send)
  thread.start()


def fetch_accumulated_trends_from_gas():
  """GASから7カテゴリーの傾向データを安全に取得（エラー時は空文字で完全保護）"""
  if not GAS_WEBAPP_URL:
    return ''
  try:
    payload = {'action': 'get_trends'}
    response = requests.post(
        GAS_WEBAPP_URL,
        data=json.dumps(payload),
        headers={'Content-Type': 'application/json'},
        timeout=5,
    )
    if response.status_code == 200:
      res_json = response.json()
      if isinstance(res_json, dict):
        return str(res_json.get('trend_info', ''))
  except Exception as e:
    logging.error(f'Failed to fetch trends: {e}')
  return ''


def load_baba_data():
  """記憶された馬場情報を読み込み（安全処理）"""
  if os.path.exists(BABA_FILE):
    try:
      with open(BABA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        if isinstance(data, dict) and data.get('date') == get_jst_today():
          return data.get('data', {})
    except Exception as e:
      logging.error(f'Failed to load baba file: {e}')
  return {}


def save_baba_data(baba_dict):
  """馬場情報を安全に保存"""
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
  msg_id = event.message.id
  if msg_id in processed_message_ids:
    logging.info(f'Duplicate message ID detected: {msg_id}. Skipping.')
    return
  processed_message_ids.add(msg_id)

  if len(processed_message_ids) > 100:
    processed_message_ids.clear()

  with ApiClient(configuration) as api_client:
    messaging_api = MessagingApi(api_client)
    reply_text = None

    try:
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=msg_id)
      raw_image = Image.open(io.BytesIO(image_bytes))

      # ★文字認識率向上＆コスト最適化の画像補正処理
      processed_image = process_image_for_ocr(raw_image)

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
              model=model_name,
              contents=[processed_image, classify_prompt],
              config=deterministic_config,
          )
          if res and res.text:
            if 'BABA' in str(res.text).strip().upper():
              image_type = 'BABA'
            break
        except Exception as c_err:
          logging.warning(f'Classify attempt [{model_name}] error: {c_err}')
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
                model=model_name,
                contents=[processed_image, extract_prompt],
                config=deterministic_config,
            )
            if res and res.text:
              raw_text = (
                  str(res.text)
                  .replace('```json', '')
                  .replace('```', '')
                  .strip()
              )
              baba_json = json.loads(raw_text)
              break
          except Exception as b_err:
            logging.warning(f'Baba extract [{model_name}] error: {b_err}')
            continue

        if isinstance(baba_json, dict) and 'keibajo' in baba_json:
          current_data = load_baba_data()
          keibajo = str(baba_json.get('keibajo', '不明'))
          current_data[keibajo] = {
              'tenko': str(baba_json.get('tenko', '不明')),
              'shiba': str(baba_json.get('shiba', '不明')),
              'dirt': str(baba_json.get('dirt', '不明')),
          }
          save_baba_data(current_data)

          reply_text = (
              '【本日の馬場情報を更新・記憶しました】\n'
              f'📍 競馬場：【{keibajo}競馬場】\n'
              f'🌤 天候：{baba_json.get("tenko", "不明")}\n'
              f'🌿 芝：{baba_json.get("shiba", "不明")}\n'
              f'🟫 ダート：{baba_json.get("dirt", "不明")}\n\n'
              '※日付が変わると自動でリセットされます。\n'
              '※本日この競馬場の出馬表が送られた際、自動反映して深層予想を行います！'
          )
        else:
          reply_text = '⚠️ 馬場情報の読み取りに失敗しました。もう一度はっきり映った画像を送信してください。'

      else:
        baba_data = load_baba_data()
        baba_context_str = (
            '【現在システムに記憶されている本日の競馬場別リアルタイム馬場情報】\n'
        )
        if baba_data and isinstance(baba_data, dict):
          for k, v in baba_data.items():
            if isinstance(v, dict):
              baba_context_str += f'・[{k}競馬場] ➔ 天候:{v.get("tenko", "不明")} / 芝:{v.get("shiba", "不明")} / ダート:{v.get("dirt", "不明")}\n'
          baba_context_str += '※【厳格指示】必ず出馬表から解析した「競馬場名」と一致するデータのみを参照してください。ない場合は標準の「良馬場」として判定してください。\n\n'
        else:
          baba_context_str += (
              '・登録なし（馬場情報未送信のため標準の「良馬場」として判定）\n\n'
          )

        # ★GASから7カテゴリーの深層傾向データを取得
        accumulated_trends = fetch_accumulated_trends_from_gas()
        trend_context_str = ''
        if accumulated_trends:
          trend_context_str = (
              '【システムが記憶している過去の7カテゴリー学習傾向データ】\n'
              + accumulated_trends
              + '\n※【重要多重指示】：\n'
              ' 1. 「注意馬（馬名）」が出走馬に含まれる場合、前走着順に関わらず評価を大幅加点（軸・相手昇格）すること。\n'
              ' 2. 「トラックバイアス」「馬場・ペース傾向」の条件に合致する脚質・枠順の馬を加点すること。\n'
              ' 3. 「消し条件」に合致する危険な人気馬は評価を下げるか買い目から外すこと。\n\n'
          )

        prompt = (
            '送られた画像（出馬表など）を解析し、まずは【開催競馬場・何レース目か（〇R）】と【距離・馬場】を正確に特定してください。\n\n'
            + baba_context_str
            + trend_context_str
            + '【絶対厳守事項：ハルシネーション禁止 ＆ 超厳格数字読み取り】\n'
            '1. 【レース番号の絶対抽出】：冒頭のタイトル【〇〇 芝/ダ〇〇〇m】には、必ず「開催場」と「何レース目か（例: 11R）」をセットで『【新潟11R 芝1200m】』のように明確に記載してください。レース番号の省略は絶対禁止です。\n'
            '2. 【数字と文字の精密読み取り】：画像内の「馬番」「馬名」「騎手名」「着差（カッコ内数値）」「通過順（ハイフン区切り）」「上がり3F（3F 〇〇.〇）」を慎重に視認してください。\n'
            '3. 【印と買い目の完全一致ルール】：『■ 3. おすすめの買い目』に含まれるすべての馬番は、必ず『■ 2. 印・期待度と推奨理由』の中に◎・◯・▲・☆・△（連下）のいずれかの印をつけて掲載してください。買い目にしか登場しない印なしの馬番は存在禁止です。\n'
            '4. 出力の最後の行には、省略せず必ず「※馬券購入は自己責任でお願いします」と記載してください。\n\n'
            '【★超人気薄・激走穴馬（☆）を拾い上げる4重スクリーニングロジック】\n'
            '☆【穴馬】には無難な人気馬を選ぶことを禁止し、前走大敗馬の中から以下の変身要素を持つ人気薄（伏兵馬）を1頭必ず選出して掲載すること。\n'
            '①【前走の明確な敗因・条件一変】：前走「重・不良馬場」で大敗➔今回「良馬場」への一変、前走「ダート」大敗➔今回「芝」変更、前走超ハイペース逃げ潰れ・前残り不向き展開など、巻き返し理由が明確な大敗馬。\n'
            '②【クラス降級＆大幅斤量減】：格上クラスからの自己条件降級・クラス慣れ、または前走から斤量「-2kg〜-4kg」の大幅減で恵まれた伏兵。\n'
            '③【トラックバイアスの極端な追い風】：GAS傾向データ（例: 外枠有利・イン差し等）の条件に合致する人気薄。\n'
            '④【着差・上がり性能】：前走着順が悪くても着差「0.5秒以内」または過去3走以内に上がり3F「34秒台以下」を記録している隠れた能力馬。\n\n'
            '【★点数を増やすことなく回収率を最大化する「柔軟な買い目選定ルール」】\n'
            'レースの「堅実度（A〜C）」および展開に応じて、以下のルールで最高効率の買い目（総点数10点以内）を選定・構成してください。\n'
            '・【堅実度A（軸堅い / 本命◎の信頼度高）】：3連単1着固定フォーメーション（1着:◎ / 2着:◯▲ / 3着:◯▲☆△【計6点程度】）または 馬単（◎→◯▲☆【計3点程度】）を選定。\n'
            '・【堅実度B（標準）】：3連複フォーメーション（1-2-4など【計5点程度】）または 馬連流し（◎→◯▲☆△【計4点程度】）を選定。\n'
            '・【堅実度C（波乱・混戦 / 穴馬☆の妙味大）】：ワイド（◎−☆、または☆→◯▲【計2〜3点】）または 3連複穴軸（☆→◎◯→◎◯▲△【計6点程度】）を選定。\n\n'
            '【条件分岐ルール】\n'
            '◆開催競馬場が「札幌」の場合のみ、以下のルールを最優先すること。\n'
            '　・芝1200m：「1枠・最内」先行馬重視。\n'
            '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
            '　・ダ1700m：最後までバテない「逃げ・先行馬」一択。\n'
            '◆札幌以外の場合は、コース形態からフラットに考察すること。\n\n'
            '【出力フォーマット】\n'
            '※以下のレイアウトを絶対に厳守し、1文字もレイアウトを崩さず出力してください。\n\n'
            '■ 1. レース概要：【〇〇〇R 芝/ダ〇〇〇m】 [レースの堅実度：A〜C]\n'
            '（展開予想と馬場状態・コース形態の影響を記載。通過順から読み取ったペース予想もここに含めること）\n\n'
            '■ 2. 印・期待度と推奨理由\n'
            '◎ 【本命】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で。着差、3Fタイムなどの具体数値を根拠に含めること）\n\n'
            '◯ 【対抗】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で）\n\n'
            '▲ 【単穴】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で）\n\n'
            '☆ 【穴馬】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n'
            '（理由を1〜2文程度で。上記①〜④の変身・激走要素を具体的に含めること）\n\n'
            '△ 【連下】 〇番 馬名（騎手名） [期待度：〇% / 評価：B〜C]\n'
            '（理由を1文程度で。買い目に含める馬を漏れなく1〜3頭程度記載すること）\n\n'
            '■ 3. おすすめの買い目\n'
            '【選定券種（堅実度に応じて3連単・ワイド・3連複・馬連・馬単等から1〜2種選定）】\n'
            '軸馬（または1着/1列目）：〇\n'
            '相手（または2着/2列目）：〇, 〇\n'
            '相手（または3着/3列目）：〇, 〇, 〇, 〇\n\n'
            '※馬券購入は自己責任でお願いします'
        )

        error_logs = []
        for model_name in candidate_models:
          try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=[processed_image, prompt],
                config=deterministic_config,
            )
            if response and response.text:
              reply_text = str(response.text)
              send_prediction_to_gas_async(reply_text)
              break
          except Exception as m_err:
            error_logs.append(f'[{model_name}] {m_err}')
            continue

        if not reply_text:
          err_str = ' / '.join(error_logs)
          reply_text = f'⚠️ AIサーバーで一時的なエラーが発生しました。\n詳細: {err_str}'

    except Exception as e:
      logging.error(f'System error: {e}')
      reply_text = f'⚠️ システムエラーが発生しました: {str(e)}'

    # LINE送信文字数上限（5,000文字）セーフティカット
    if reply_text and len(reply_text) > 4900:
      reply_text = reply_text[:4900] + '\n...(以下省略)'

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
