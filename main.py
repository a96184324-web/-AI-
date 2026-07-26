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


def load_baba_data():
  """保存されている馬場情報を読み込む"""
  if os.path.exists(BABA_FILE):
    try:
      with open(BABA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)
    except Exception as e:
      logging.error(f'Failed to load baba file: {e}')
  return {}


def save_baba_data(data):
  """馬場情報をファイルに書き込む"""
  try:
    with open(BABA_FILE, 'w', encoding='utf-8') as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
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
      # ステップ1: 送られた画像が「馬場情報」か「出馬表」か自動判別
      # ----------------------------------------------------
      classify_prompt = (
          '送られた画像を判定してください。\n'
          'JRAなどの「馬場情報（天候、芝・ダートの馬場状態）」の画面であれば'
          ' "BABA" と答えてください。\n'
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
            text_clean = res.text.strip().upper()
            if 'BABA' in text_clean:
              image_type = 'BABA'
            break
        except Exception:
          continue

      # ----------------------------------------------------
      # パターンA: 画像が「馬場情報」の場合 ➔ 記憶して終了
      # ----------------------------------------------------
      if image_type == 'BABA':
        extract_prompt = (
            'この馬場情報画像から【競馬場名】、【天候】、【芝の馬場状態】、【ダートの馬場状態】を抽出し、以下のJSON形式のみで出力してください。\n'
            '{"keibajo": "新潟", "tenko": "雨", "shiba": "稍重", "dirt":'
            ' "重"}\n'
            '※JSON以外の挨拶や解説文は一切含めないでください。'
        )

        baba_json = None
        for model_name in candidate_models:
          try:
            res = ai_client.models.generate_content(
                model=model_name, contents=[image, extract_prompt]
            )
            if res and res.text:
              raw_text = (
                  res.text.replace('```json', '').replace('```', '').strip()
              )
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
              '【馬場情報を更新・記憶しました】\n'
              f'📍 競馬場：{keibajo}競馬場\n'
              f'🌤 天候：{baba_json.get("tenko", "不明")}\n'
              f'🌿 芝：{baba_json.get("shiba", "不明")}\n'
              f'🟫 ダート：{baba_json.get("dirt", "不明")}\n\n'
              '※本日この競馬場の出馬表が送られた際、上記条件を自動で反映して予想します！'
          )
        else:
          reply_text = '⚠️ 馬場情報の読み取りに失敗しました。もう一度はっきり映った画像を送信してください。'

      # ----------------------------------------------------
      # パターンB: 画像が「出馬表」の場合 ➔ 記憶された馬場情報を使って予想
      # ----------------------------------------------------
      else:
        baba_data = load_baba_data()
        baba_context_str = '【現在システムに登録されているリアルタイム馬場情報】\n'
        if baba_data:
          for k, v in baba_data.items():
            baba_context_str += (
                f'・{k}競馬場 ➔ 天候:{v.get("tenko")} / 芝:{v.get("shiba")} /'
                f' ダート:{v.get("dirt")}\n'
            )
          baba_context_str += '※出馬表の競馬場に対応する馬場情報があれば、道悪（重・稍重）適性や馬場差・天候による展開変化（泥んこパワー型、外差し傾向、高速ダートの前残り等）を最優先して予想に反映すること。\n\n'
        else:
          baba_context_str += (
              '・登録なし（馬場情報画像未送信のため標準の「良馬場」として判定）\n\n'
          )

        prompt = (
            '送られた画像（出馬表など）を解析し、まずは【開催競馬場】と【距離・馬場】を特定してください。\n\n'
            + baba_context_str
            + '【絶対厳守事項：ハルシネーション禁止 ＆ 人気・オッズ完全無視 ＆ 視認性最優先】\n'
            '1. 架空のデータや数値をねつ造するハルシネーションは絶対に禁止します。\n'
            '2. オッズや人気順は一切考慮せず、出馬表の事実（枠順、脚質、持ちタイム、騎手・斤量、継続騎乗、厩舎所属、過去成績）に基づく【条件合致度（100点満点）】を期待度（%）として算出してください。\n'
            '3. 期待度はレースの勝率（合計100%）ではなく、各馬の絶対評価として算出してください。\n'
            '4. 【評価基準】S: 90〜100%（条件完全合致）、A: 80〜89%（ほぼ合致）、B: 70〜79%（一部合致）としてください。\n'
            '5. 挨拶や無駄な装飾は省き、箇条書きの「*」記号は絶対に使用しないでください。\n'
            '6. 出力の最後の行には、省略せず必ず「※馬券購入は自己責任でお願いします」と記載してください。\n\n'
            '【全競馬場共通・論理的チェックリスト（騎手・厩舎・展開・馬場状態の深掘り）】\n'
            '・馬場状態と天候の影響：登録されている馬場状態（稍重・重・不良など）がある場合、その馬場・血統・過去成績における道悪適性を強く考慮すること。\n'
            '・騎手と斤量（減量恩恵）：騎手名横の記号（▲、★等）や負担重量（52.0kg等）を確認し、軽量による有利さを評価に加味すること。\n'
            '・継続騎乗の確認：前走の騎手と今回の騎手を比較し、継続騎乗や好走騎手への手が戻る場合は評価すること。\n'
            '・厩舎所属（美浦/栗東）：関西馬（栗東）の遠征や同一厩舎2頭出しなどの気配を加味すること。\n'
            '・着差と内容：過去の着順だけでなく「1着とのタイム差」や「上がり3F」を見て実力を評価すること。\n'
            '・展開の競合リスク：過去成績から「逃げ・先行馬」が多すぎる場合はハイペースによる共倒れリスクを考慮し、差し馬の評価を相対的に上げること。\n\n'
            '【条件分岐ルール】\n'
            '◆ 開催競馬場が「札幌」の場合のみ、以下の【札幌開幕週・特別ルール】を最優先して予想を組み立てること。（ただし雨や重馬場が登録されている場合は、外差しやパワー馬優先に補正すること）\n'
            '　・基本は【前・内有利】の高速決着。ロスなく運べる「内枠」「逃げ・先行馬」が圧倒的有利。\n'
            '　・100%洋芝（パワー必須）、ほぼ平坦、コーナーが緩く直線短い。\n'
            '　・芝1200m：「1枠・最内」先行馬重視。\n'
            '　・芝2000m：インで脚を溜める「イン差しの馬」。\n'
            '　・ダ1700m：最後までバテない「逃げ・先行馬」一択。\n'
            '　・買い方：「1〜3枠」の「逃げ・先行馬」軸。大外枠（7〜8枠）の差し馬は人気でも消しや割引。\n\n'
            '◆ 「札幌以外」の場合は、上記ルールを無視し、一般的な適性やコース形態・登録馬場状態からフラットに考察すること。\n\n'
            '【買い目の自動選定およびフォーマットルール】\n'
            '堅実度（A〜C）に応じて選んだ券種を、必ず以下の「改行・縦並びフォーマット」に従って出力すること。\n'
            '・堅実度A（本命戦） ➔ 【馬単】および【3連単フォーメーション】\n'
            '・堅実度B（標準）   ➔ 【馬連・馬単】および【3連複フォーメーション】\n'
            '・堅実度C（混戦）   ➔ 【ワイド】および【3連複フォーメーション】\n\n'
            '【出力フォーマット】\n'
            '※以下のレイアウトを厳守し、各項目の間には必ず「1行の空白行」を入れてください。\n\n'
            '■ 1. 競馬場と展開予想 [レースの堅実度：A〜C]\n'
            '（ここに簡潔な展開予想と馬場状態の影響を記載）\n\n'
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
