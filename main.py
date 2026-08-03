import datetime
import io
import json
import logging
import os
import re
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
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, TextMessageContent
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
RACE_LIST_FILE = 'race_list_info.json'

processed_message_ids = set()

image_buffer = []
buffer_lock = threading.Lock()

def get_jst_today():
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        return datetime.datetime.now(jst).strftime('%Y-%m-%d')
    except Exception as e:
        logging.error(f"Date error: {e}")
        return datetime.datetime.now().strftime('%Y-%m-%d')

def process_image_for_ocr(image):
    try:
        img_copy = image.copy()
        img_copy.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        enhancer = ImageEnhance.Contrast(img_copy)
        img_copy = enhancer.enhance(1.4)
        img_copy = img_copy.filter(ImageFilter.SHARPEN)
        return img_copy
    except Exception as e:
        logging.error(f"Image enhancement failed: {e}")
        return image

def send_to_gas_async(action, payload_data):
    def _send():
        if not GAS_WEBAPP_URL:
            return
        try:
            payload = {
                'action': action,
                'date': get_jst_today(),
                'data': payload_data
            }
            response = requests.post(
                GAS_WEBAPP_URL,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                timeout=15
            )
            logging.info(f"GAS [{action}] Response: {response.status_code}")
        except Exception as e:
            logging.error(f"Failed to send [{action}] to GAS: {e}")

    thread = threading.Thread(target=_send)
    thread.start()

def load_json_file(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get('date') == get_jst_today():
                    return data.get('data', {})
        except Exception as e:
            logging.error(f"Failed to load {filepath}: {e}")
    return {}

def save_json_file(filepath, dict_data):
    try:
        data_to_save = {
            'date': get_jst_today(),
            'data': dict_data
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Failed to save {filepath}: {e}")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logging.error(f"Error handling webhook: {e}")
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    msg_id = event.message.id
    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)
    if len(processed_message_ids) > 100:
        processed_message_ids.clear()

    user_text = event.message.text
    
    if any(k in user_text for k in ["着順", "ハロンタイム", "単勝", "複勝", "コーナー通過順位"]):
        cleaned_lines = []
        skip_keywords = ["JRAプラス10", "特払い", "勝馬の紹介", "印刷用ページ", "レース映像", "全周パトロール"]
        for line in user_text.splitlines():
            if not any(sk in line for sk in skip_keywords):
                cleaned_lines.append(line)
        cleaned_text = "\n".join(cleaned_lines)

        send_to_gas_async('save_race_results', cleaned_text)
        reply_text = (
            "【結果テキストを一括取り込みました】\n"
            "雑文を自動カットし、GASデータベースへ数値を保存しました。\n"
            "※次週の予想時に、この一次データ（前残り・展開不向き）が自動参照されます！"
        )
    else:
        reply_text = "メッセージありがとうございます。出馬表・馬場・レース一覧のスクショ画像、またはレース結果テキストを送信してください。"

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    msg_id = event.message.id
    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)
    if len(processed_message_ids) > 100:
        processed_message_ids.clear()

    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)

        try:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(message_id=msg_id)
            raw_image = Image.open(io.BytesIO(image_bytes))
            processed_image = process_image_for_ocr(raw_image)

            candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
            deterministic_config = types.GenerateContentConfig(temperature=0.0)

            classify_prompt = (
                "送られた画像を判定してください。\n"
                "・1R〜12Rなどの『全レース一覧・コース距離表』の画面であれば \"LIST\" と答えてください。\n"
                "・『馬場情報（天候、芝・ダートの馬場状態）』の画面であれば \"BABA\" と答えてください。\n"
                "・それ以外の『出馬表（馬名やオッズが並ぶ画面）』であれば \"RACE\" と答えてください。\n"
                "回答は \"LIST\"、\"BABA\"、\"RACE\" の英字1単語のみにしてください。"
            )

            image_type = 'RACE'
            for model_name in candidate_models:
                try:
                    res = ai_client.models.generate_content(
                        model=model_name,
                        contents=[processed_image, classify_prompt],
                        config=deterministic_config
                    )
                    if res and res.text:
                        text_upper = str(res.text).strip().upper()
                        if 'LIST' in text_upper:
                            image_type = 'LIST'
                        elif 'BABA' in text_upper:
                            image_type = 'BABA'
                        break
                except Exception as c_err:
                    logging.warning(f"Classify attempt [{model_name}] error: {c_err}")
                    continue

            if image_type == 'LIST':
                extract_list_prompt = (
                    "この画像から【開催競馬場名】と【各レース(1R〜12R)のコース・距離・条件】を抽出し、以下のJSON形式のみで出力してください。\n"
                    "{\"keibajo\": \"札幌\", \"races\": {\"1R\": \"ダ1700m\", \"2R\": \"芝1200m\"}}\n"
                    "※JSON以外の文字列は含めないでください。"
                )
                list_json = None
                for model_name in candidate_models:
                    try:
                        res = ai_client.models.generate_content(
                            model=model_name,
                            contents=[processed_image, extract_list_prompt],
                            config=deterministic_config
                        )
                        if res and res.text:
                            raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                            list_json = json.loads(raw_text)
                            break
                    except Exception as l_err:
                        logging.warning(f"List extract [{model_name}] error: {l_err}")
                        continue

                if isinstance(list_json, dict) and 'keibajo' in list_json:
                    keibajo = list_json.get('keibajo', '不明')
                    current_list = load_json_file(RACE_LIST_FILE)
                    current_list[keibajo] = list_json.get('races', {})
                    save_json_file(RACE_LIST_FILE, current_list)
                    send_to_gas_async('save_race_list', list_json)

                    reply_text = f"【本日の{keibajo}競馬場 全レース一覧・距離情報を記憶しました】"
                else:
                    reply_text = "⚠️ 全レース一覧の読み取りに失敗しました。もう一度送信してください。"

                messaging_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
                )

            elif image_type == 'BABA':
                extract_baba_prompt = (
                    "この馬場情報画像から【競馬場名】、【天候】、【芝の馬場状態】、【ダートの馬場状態】を抽出し、以下のJSON形式のみで出力してください。\n"
                    "{\"keibajo\": \"札幌\", \"tenko\": \"晴\", \"shiba\": \"良\", \"dirt\": \"良\"}\n"
                    "※JSON以外の文字列は含めないでください。"
                )
                baba_json = None
                for model_name in candidate_models:
                    try:
                        res = ai_client.models.generate_content(
                            model=model_name,
                            contents=[processed_image, extract_baba_prompt],
                            config=deterministic_config
                        )
                        if res and res.text:
                            raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                            baba_json = json.loads(raw_text)
                            break
                    except Exception as b_err:
                        logging.warning(f"Baba extract [{model_name}] error: {b_err}")
                        continue

                if isinstance(baba_json, dict) and 'keibajo' in baba_json:
                    keibajo = str(baba_json.get('keibajo', '不明'))
                    current_baba = load_json_file(BABA_FILE)
                    current_baba[keibajo] = {
                        'tenko': str(baba_json.get('tenko', '不明')),
                        'shiba': str(baba_json.get('shiba', '不明')),
                        'dirt': str(baba_json.get('dirt', '不明'))
                    }
                    save_json_file(BABA_FILE, current_baba)
                    send_to_gas_async('save_baba', baba_json)

                    reply_text = (
                        f"【本日の馬場情報を更新・記憶しました】\n"
                        f"📍 競馬場：【{keibajo}競馬場】\n"
                        f"🌤 天候：{baba_json.get('tenko', '不明')}\n"
                        f"🌿 芝：{baba_json.get('shiba', '不明')}\n"
                        f"🟫 ダート：{baba_json.get('dirt', '不明')}"
                    )
                else:
                    reply_text = "⚠️ 馬場情報の読み取りに失敗しました。もう一度送信してください。"

                messaging_api.reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
                )

            else:
                with buffer_lock:
                    image_buffer.append(processed_image)

                def process_race_prediction(reply_token, imgs):
                    baba_data = load_json_file(BABA_FILE)
                    
                    baba_context_str = "【記憶されている本日のリアルタイム馬場情報】\n"
                    if baba_data:
                        for k, v in baba_data.items():
                            baba_context_str += f"・[{k}競馬場] 天候:{v.get('tenko')} / 芝:{v.get('shiba')} / ダ:{v.get('dirt')}\n"
                    else:
                        baba_context_str += "・未設定（標準の良馬場として判定）\n"

                    prompt = (
                        "送られた1枚または2枚の出馬表画像を解析してください。\n"
                        "※2枚の画像で中央付近の馬（馬番）が重複して映っている場合は、馬番を基準にしてダブりを自動削除し、全頭1つに結合して解析してください。\n\n"
                        + baba_context_str + "\n"
                        "【絶対厳守ルール】\n"
                        "1. タイトル表記：冒頭は必ず『【札幌1R ダ1700m】』のように競馬場・レース番号・距離条件を完全に明記すること。\n"
                        "2. 馬番認識：画像内の馬番・馬名・負担重量・騎手・前走着順・通過順を正確に読み取ること。\n"
                        "3. 買い目整合性：『■ 3. おすすめの買い目』の馬番は、必ず『■ 2. 印・期待度と推奨理由』の印付き馬と完全一致させること。\n"
                        "4. 過去の抽象傾向データの参照は禁止。出馬表の純粋数値のみで判定すること。\n\n"
                        "【新・激走穴馬（☆）抜擢ロジック】\n"
                        "人気や前走の着順を完全に無視し、以下の数値トリガーを満たす伏兵馬を必ず☆（穴馬）または上位印に抜擢すること。\n"
                        "・前走不向きな展開（前残り馬場で後方から上がり上位を使って惨敗等）からの巻き返し\n"
                        "・今回大幅な斤量減（-2kg〜-4kg）または馬体重増減の改善\n"
                        "・前走と異なるトラックバイアス・距離変更での一変\n\n"
                        "【出力フォーマット】\n"
                        "■ 1. レース概要：【〇〇〇R 芝/ダ〇〇〇m】 [レースの堅実度：A〜C]\n"
                        "（展開・馬場・ペースの分析）\n\n"
                        "■ 2. 印・期待度と推奨理由\n"
                        "◎ 【本命】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                        "◯ 【対抗】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                        "▲ 【単穴】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                        "☆ 【穴馬】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                        "△ 【連下】 〇番 馬名（騎手名） [期待度：〇% / 評価：B〜C]\n\n"
                        "■ 3. おすすめの買い目\n"
                        "【選定券種】\n"
                        "軸馬：〇\n"
                        "相手：〇, 〇, 〇\n\n"
                        "※馬券購入は自己責任でお願いします"
                    )

                    content_list = imgs + [prompt]
                    reply_text = None
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=content_list,
                                config=deterministic_config
                            )
                            if res and res.text:
                                reply_text = str(res.text)
                                send_to_gas_async('save_prediction', reply_text)
                                break
                        except Exception as p_err:
                            logging.warning(f"Prediction attempt [{model_name}] error: {p_err}")
                            continue

                    if not reply_text:
                        reply_text = "⚠️ 出馬表の読み取り・予想作成に失敗しました。もう一度送信してください。"

                    if len(reply_text) > 4900:
                        reply_text = reply_text[:4900] + "\n...(以下省略)"

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(
                            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=reply_text)])
                        )

                def timer_callback():
                    with buffer_lock:
                        imgs_to_process = list(image_buffer)
                        image_buffer.clear()
                    if imgs_to_process:
                        process_race_prediction(event.reply_token, imgs_to_process)

                timer = threading.Timer(0.8, timer_callback)
                timer.start()

        except Exception as e:
            logging.error(f"System error: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
