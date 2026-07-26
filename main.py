import datetime
import io
import json
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

BABA_FILE = 'baba_info.json'


def get_jst_today():
  """日本時間の現在日付（YYYY-MM-DD）を取得"""
  jst = datetime.timezone(datetime.timedelta(hours=9))
  return datetime.datetime.now(jst).strftime('%Y-%m-%d')


def load_baba_data():
  """保存されている馬場情報を読み込み、日付が古ければリセットする"""
  if os.path.exists(BABA_FILE):
    try:
      with open(BABA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        # 保存日付が今日と異なる場合はリセット（空を返す）
        if data.get('date') != get_jst_today():
          return {}
        return data.get('data', {})
    except Exception as e:
      logging.error(f'Failed to load baba file: {e}')
  return {}


def save_baba_data(baba_dict):
  """馬場情報を現在日付とともにファイルに書き込む"""
  try:
    data_to_save = {
        'date': get_jst_today(),
        'data': baba_dict
    }
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
      # LINEから画像を取得
      blob_api = MessagingApiBlob(api_client)
      image_bytes = blob_api.get_message_content(message_id=event.message.id)
      image = Image.open(io.BytesIO(image_bytes))
      image.thumbnail((2048, 2048))

      candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']

      # ----------------------------------------------------
      # ステップ1: 画像の自動判別（馬場情報か出馬表か）
      # ----------------------------------------------------
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
              model=model_name, contents=[image, classify_prompt]
          )
          if res and res.text:
            if 'BABA' in res.text.strip().upper():
              image_type = 'BABA'
            break
        except Exception:
          continue

      # ----------------------------------------------------
      # パターンA: 画像が「馬場情報」の場合
      # ----------------------------------------------------
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
                model=model_name, contents=[image, extract_prompt]
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
              f'📍 競馬場：{keibajo}競馬場\n'
              f'🌤 天候：{baba_json.get("tenko", "不明")}\n'
              f'🌿 芝：{baba_json.get("shiba", "不明")}\n'
              f'🟫 ダート：{baba_json.get("dirt", "不明")}\n\n'
              '※日付が変わると自動でリセットされます。\n'
              '※本日この競馬場の出馬表が送られた際、血統適性や有利な脚質に自動反映します！'
          )
        else:
          reply_text = '⚠️ 馬場情報の読み取りに失敗しました。もう一度はっきり映った画像を送信してください。'

      # ----------------------------------------------------
      # パターンB: 画像が「出馬表」の場合（究極ロジック適用）
      # ----------------------------------------------------
      else:
        baba_data = load_baba_data()
        baba_context_str = '【現在システムに登録されている本日のリアルタイム馬場情報】\n'
        if baba_data:
          for k, v in baba_data.items():
            baba_context_str += f'・{k}競馬場 ➔ 天候:{v.get("tenko")} / 芝:{v.get("shiba")} / ダート:{v.get("dirt")}\n'
          baba_context_str += '※対応する馬場情報がある場合、道悪適性（血統）や天候による展開変化を最優先して予想に反映すること。\n\n'
        else:
          baba_context_str += '・登録なし（馬場情報画像未送信のため標準の「良馬場」として判定）\n\n'

        prompt = (
            '送られた画像（出馬表など）を解析し、まずは【開催競馬場】と【距離・馬場】を特定してください。\n\n'
            + baba_context_str +
            '【絶対厳守事項：ハルシネーション禁止 ＆ 人気・オッズ完全無視】\n'
            '1. 架空のデータや数値をねつ造するハルシネーションは絶対に禁止します。\n'
            '2. 馬番と馬名のズレ防止：表組みを読み取る際、視覚的なズレが生じやすいため、必ず「同じ横の行」にある【馬番】と【馬名】が正確に一致しているか、出力前に必ずクロスチェック（指差し確認）してください。\n'
            '3. オッズや人気順は一切考慮せず、出馬表の事実のみに基づく【条件合致度（100点満点）】を期待度（%）として絶対評価で算出してください。\n'
            '4. 【評価基準】S: 90〜100%（条件完全合致）、A: 80〜89%（ほぼ合致）、B: 70〜79%（一部合致）としてください。\n'
            '5. 挨拶や無駄な装飾は省き、箇条書きの「*」記号は絶対に使用しないでください。\n'
            '6. 出力の最後の行には、省略せず必ず「※馬券購入は自己責任でお願いします」と記載してください。\n\n'
            '【全競馬場共通・究極の論理的チェックリスト（多角的な深掘り）】\n'
            '・コース形態バイアス：直線が短いコース（札幌・函館・中山・小倉等）は「前・内有利」、直線が長いコース（東京・新潟外・阪神外等）は末脚の生きる「差し・外有利」を基本とする。\n'
            '・ダート戦×枠順×テンの速さ：ダート戦で「内枠（1〜3枠）」を高評価にするのは、過去成績から「テンが速い（前に行ける）逃げ・先行馬」に限定する。出足が遅い内枠の差し・追い込み馬は、砂被り・包まれリスク大として大幅割引（消し評価）とする。\n'
            '・前走からの変動要素：過去成績欄と比較し、「斤量の大幅減（例:56kg→52kg）」はスピードUPの加点材料、「極端な距離延長・短縮」は折り合いリスクとして慎重に評価する。\n'
            '・血統と馬場適性：馬場状態が「稍重・重・不良」の場合、画像記載の「父・母父」から論理的に推測できる範囲で道悪適性（パワー型等）を加味する。\n'
            '・騎手と厩舎：減量記号（▲、★等）の恩恵、継続騎乗、好走騎手への手戻り、厩舎の遠征気配などを加点する。\n\n'
            '【条件分岐ルール】\n'
            '◆ 開催競馬場が「札幌」の場合のみ、以下の【札幌開幕週ルール】を最優先すること。\n'
            '　・芝1200m：「1枠・最内」先行馬重視。\n'
            '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
            '　・ダ1700m：最後までバテない「逃げ・先行馬」一択（※出足の遅い内枠馬は消し）。\n'
            '◆ 札幌以外の場合は、上記ルールを無視し、コース形態からフラットに考察すること。\n\n'
            '【買い目の自動選定およびフォーマットルール】\n'
            '堅実度（A〜C）に応じて選定した券種を、必ず以下の「改行・縦並びフォーマット」に従って出力すること。\n'
            '・堅実度A（本命戦） ➔ 【馬単】および【3連単フォーメーション】\n'
            '・堅実度B（標準）   ➔ 【馬連・馬単】および【3連複フォーメーション】\n'
            '・堅実度C（混戦）   ➔ 【ワイド】および【3連複フォーメーション】\n\n'
            '【出力フォーマット】\n'
            '※以下のレイアウトを厳守し、各項目の間には必ず「1行の空白行」を入れてください。\n\n'
            '■ 1. 競馬場と展開予想 [レースの堅実度：A〜C]\n'
            '（ここに簡潔な展開予想と馬場状態・コース形態の影響を記載）\n\n'
            '■ 2. 印・期待度と推奨理由\n'
            '◎ 【本命】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
            '（理由を1文程度で。血統や斤量変動、ダートならテンの速さなども交えて）\n\n'
            '◯ 【対抗】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
            '（理由を1文程度で）\n\n'
            '▲ 【単穴】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
            '（理由を1文程度で）\n\n'
            '☆ 【穴馬】 〇番 馬名 [期待度：〇% / 評価：S〜B]\n'
            '（理由を1文程度で）\n\n'
            '■ 3. おすすめの買い目\n'
            '【選定券種（例：馬連・馬単 など）】\n'
            '軸馬：〇\n'
            '相手：〇, 〇, 〇\n\n'
            '【選定券種（例：3連複フォーメーション など）】\n'
            '1列目（または1着）：〇\n'
            '2列目（または2着）：〇, 〇\n'
            '3列目（または3着）：〇, 〇, 〇, 〇\n\n'
            '※馬券購入は自己責任でお願いします'
        )

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
