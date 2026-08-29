import datetime
import io
import json
import logging
import os
import re
import threading
import unicodedata
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
TREND_FILE = 'trend_info.json'

processed_message_ids = set()

image_buffer = []
buffer_lock = threading.Lock()
current_timer = None
latest_reply_token = None

JRA_KEIBAJO_LIST = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"]

COURSE_MASTER = {
    "札幌": {"1回": {"A": range(1, 7)}, "2回": {"B": range(1, 7)}},
    "函館": {"1回": {"A": range(1, 7), "B": range(7, 13)}},
    "福島": {"1回": {"A": range(1, 7)}, "2回": {"A": range(1, 7)}, "3回": {"A": range(1, 7)}},
    "新潟": {"1回": {"A": range(1, 7)}, "2回": {"A": range(1, 7), "B": range(7, 13)}, "3回": {"A": range(1, 9)}},
    "東京": {"1回": {"A": range(1, 5), "B": range(5, 9)}, "2回": {"A": range(1, 5), "B": range(5, 9), "C": range(9, 13)}, "3回": {"A": range(1, 9)}},
    "中山": {"1回": {"A": range(1, 9)}, "2回": {"A": range(1, 5), "B": range(5, 9)}, "3回": {"A": range(1, 9)}},
    "中京": {"1回": {"A": range(1, 7)}, "2回": {"A": range(1, 7)}, "3回": {"A": range(1, 7)}},
    "京都": {"1回": {"A": range(1, 9)}, "2回": {"A": range(1, 5), "B": range(5, 9)}, "3回": {"A": range(1, 9)}},
    "阪神": {"1回": {"A": range(1, 9)}, "2回": {"A": range(1, 5), "B": range(5, 9)}, "3回": {"A": range(1, 9)}},
    "小倉": {"1回": {"A": range(1, 7)}, "2回": {"A": range(1, 7)}, "3回": {"A": range(1, 9)}}
}

def clean_text(text):
    if not text:
        return ""
    text = unicodedata.normalize('NFKC', str(text))
    return re.sub(r'\s+', '', text).strip().lower()

def force_parse_json(data):
    if not isinstance(data, str):
        return data
    try:
        val = data.strip()
        while (val.startswith('{') and val.endswith('}')) or (val.startswith('[') and val.endswith(']')):
            parsed = json.loads(val)
            if isinstance(parsed, str):
                val = parsed.strip()
            else:
                return parsed
        return val
    except Exception:
        return data

def get_jst_today():
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        return datetime.datetime.now(jst).strftime('%Y-%m-%d')
    except Exception as e:
        logging.error(f"Date error: {e}")
        return datetime.datetime.now().strftime('%Y-%m-%d')

def process_image_for_ocr(image):
    try:
        w, h = image.size
        crop_top = int(h * 0.18)
        crop_bottom = int(h * 0.85)
        
        if h > 800:
            img_cropped = image.crop((0, crop_top, w, crop_bottom))
        else:
            img_cropped = image.copy()

        img_copy = img_cropped.copy()
        img_copy.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        enhancer = ImageEnhance.Contrast(img_copy)
        img_copy = enhancer.enhance(1.5)
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
                timeout=20
            )
            logging.info(f"GAS [{action}] Response: {response.status_code}")
        except Exception as e:
            logging.error(f"Failed to send [{action}] to GAS: {e}")

    thread = threading.Thread(target=_send)
    thread.start()

def fetch_past_results_from_gas(keibajo="", track_type="", distance=""):
    if not GAS_WEBAPP_URL:
        return ""
    try:
        payload = {
            'action': 'get_past_results',
            'keibajo': keibajo,
            'track_type': track_type,
            'distance': str(distance)
        }
        response = requests.post(
            GAS_WEBAPP_URL,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        if response.status_code == 200:
            res_json = force_parse_json(response.json())
            if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                return str(res_json.get('data', ''))
    except Exception as e:
        logging.error(f"Failed to fetch past results from GAS: {e}")
    return ""

def fetch_baba_from_gas():
    if not GAS_WEBAPP_URL:
        return {}
    try:
        payload = {'action': 'get_baba', 'date': ''}
        response = requests.post(
            GAS_WEBAPP_URL,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        if response.status_code == 200:
            res_json = force_parse_json(response.json())
            if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                data = force_parse_json(res_json.get('data', {}))
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logging.error(f"Failed to fetch baba info from GAS: {e}")
    return {}

def fetch_trend_from_gas():
    if not GAS_WEBAPP_URL:
        return {}
    try:
        payload = {'action': 'get_trend', 'date': ''}
        response = requests.post(
            GAS_WEBAPP_URL,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        if response.status_code == 200:
            res_json = force_parse_json(response.json())
            if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                data = force_parse_json(res_json.get('data', {}))
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logging.error(f"Failed to fetch trend info from GAS: {e}")
    return {}

def fetch_race_list_from_gas():
    if not GAS_WEBAPP_URL:
        return {}
    for attempt in range(3):
        try:
            payload = {'action': 'get_race_list', 'date': ''}
            response = requests.post(
                GAS_WEBAPP_URL,
                data=json.dumps(payload),
                headers={'Content-Type': 'application/json'},
                timeout=20
            )
            if response.status_code == 200:
                res_json = force_parse_json(response.json())
                if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                    raw_data = res_json.get('data', [])
                    processed_dict = {}

                    def extract_race_objects(obj):
                        if isinstance(obj, str):
                            parsed = force_parse_json(obj)
                            if isinstance(parsed, (dict, list)) and parsed != obj:
                                extract_race_objects(parsed)
                            return

                        if isinstance(obj, dict):
                            if 'keibajo' in obj and 'races' in obj:
                                kj = clean_text(str(obj.get('keibajo')))
                                kai = clean_text(str(obj.get('kai', '')))
                                nichi = clean_text(str(obj.get('nichi', '')))
                                races = force_parse_json(obj.get('races', {}))
                                
                                if isinstance(races, dict):
                                    cleaned_races = {clean_text(k): clean_text(v) for k, v in races.items()}
                                    course_info = get_course_info(kj, kai, nichi) if kai and nichi else "開催区分"
                                    if kj in processed_dict:
                                        processed_dict[kj]['races'].update(cleaned_races)
                                        processed_dict[kj]['course_info'] = course_info
                                    else:
                                        processed_dict[kj] = {
                                            'races': cleaned_races,
                                            'course_info': course_info
                                        }
                            else:
                                for v in obj.values():
                                    extract_race_objects(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                extract_race_objects(item)

                    extract_race_objects(raw_data)
                    if processed_dict:
                        return processed_dict

        except Exception as e:
            logging.error(f"Failed to fetch race_list info from GAS (Attempt {attempt+1}): {e}")
    return {}

def resolve_course_condition(keibajo_name, race_num_only, list_data, raw_imgs):
    def find_in_races(races_input):
        races_dict = force_parse_json(races_input)
        if isinstance(races_dict, dict):
            for r_key, cond_str in races_dict.items():
                clean_r_key = clean_text(r_key)
                m_key = re.search(r'(\d+)', clean_r_key)
                if m_key and m_key.group(1) == str(race_num_only):
                    cond = str(cond_str)
                    tt = ""
                    if "ダ" in cond or "だ" in cond:
                        tt = "ダート"
                    elif "芝" in cond or "し" in cond:
                        tt = "芝"
                    m_dist = re.search(r'(\d+)', cond)
                    dist = m_dist.group(1) if m_dist else ""
                    if tt and dist:
                        return tt, dist
        return "", ""

    # Tier 1: ローカル検索
    if isinstance(list_data, dict):
        for k, v in list_data.items():
            clean_k = clean_text(k)
            if clean_k and (clean_k == keibajo_name or clean_k in keibajo_name or keibajo_name in clean_k):
                if isinstance(v, dict) and 'races' in v:
                    tt, dist = find_in_races(v['races'])
                    if tt and dist:
                        return tt, dist

    # Tier 2: GASから全件取得して検索
    gas_list = fetch_race_list_from_gas()
    if isinstance(gas_list, dict) and gas_list:
        if isinstance(list_data, dict):
            list_data.update(gas_list)
        else:
            list_data = gas_list
        save_json_file(RACE_LIST_FILE, list_data)

        for k, v in list_data.items():
            clean_k = clean_text(k)
            if clean_k and (clean_k == keibajo_name or clean_k in keibajo_name or keibajo_name in clean_k):
                if isinstance(v, dict) and 'races' in v:
                    tt, dist = find_in_races(v['races'])
                    if tt and dist:
                        return tt, dist

    # Tier 3: 出馬表画像からの直接OCRフォールバック
    try:
        ocr_prompt = (
            f"送られた出馬表画像（馬の過去走成績欄など）から、対象レースのコース条件を読み取ってください。\n"
            "以下のJSON形式のみで出力してください:\n"
            "{\"track_type\": \"芝\" または \"ダート\", \"distance\": \"数字のみ\"}"
        )
        res = ai_client.models.generate_content(
            model='gemini-3.5-flash',
            contents=raw_imgs + [ocr_prompt],
            config=types.GenerateContentConfig(temperature=0.0)
        )
        if res and res.text:
            raw_t = str(res.text).replace('```json', '').replace('```', '').strip()
            extracted = force_parse_json(raw_t)
            if isinstance(extracted, dict):
                tt_ocr = str(extracted.get('track_type', ''))
                dist_ocr = str(extracted.get('distance', ''))
                if "ダ" in tt_ocr: tt_ocr = "ダート"
                elif "芝" in tt_ocr: tt_ocr = "芝"
                m_d = re.search(r'(\d+)', dist_ocr)
                dist_ocr = m_d.group(1) if m_d else ""
                if tt_ocr and dist_ocr:
                    logging.info(f"Fallback OCR success: {tt_ocr}{dist_ocr}m")
                    return tt_ocr, dist_ocr
    except Exception as e:
        logging.error(f"Fallback OCR error: {e}")

    return "", ""

def get_course_info(keibajo, kai, nichi):
    try:
        kai_str = f"{kai}回"
        nichi_num = int(nichi)
        for master_kj, data in COURSE_MASTER.items():
            if clean_text(master_kj) == keibajo or master_kj in keibajo:
                if kai_str in data:
                    for course, day_range in data[kai_str].items():
                        if nichi_num in day_range:
                            return f"{course}コース（開幕{nichi_num}日目）"
    except Exception as e:
        logging.error(f"Course master lookup error: {e}")
    return "開催区分"

def load_json_file(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data = force_parse_json(data)
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
    if len(processed_message_ids) > 200:
        processed_message_ids.clear()

    reply_text = (
        "メッセージありがとうございます！\n"
        "LINEからは【出馬表・馬場情報・レース一覧・本日の傾向】のスクショ画像を送信してください。\n\n"
        "※レース結果テキストの一括保存・解析は、専用のWebフォームから行ってください。"
    )

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
    global current_timer, latest_reply_token
    msg_id = event.message.id

    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)
    if len(processed_message_ids) > 200:
        processed_message_ids.clear()

    with ApiClient(configuration) as api_client:
        try:
            blob_api = MessagingApiBlob(api_client)
            image_bytes = blob_api.get_message_content(message_id=msg_id)
            
            raw_image = Image.open(io.BytesIO(image_bytes))
            raw_for_header = raw_image.copy()
            raw_for_header.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            
            processed_image = process_image_for_ocr(raw_image)

            with buffer_lock:
                image_buffer.append((raw_for_header, processed_image))
                latest_reply_token = event.reply_token

            def process_race_prediction():
                global latest_reply_token, current_timer
                with buffer_lock:
                    imgs_data = list(image_buffer)
                    image_buffer.clear()
                    r_token = latest_reply_token
                    current_timer = None

                if not imgs_data or not r_token:
                    return

                raw_imgs = [item[0] for item in imgs_data]
                proc_imgs = [item[1] for item in imgs_data]

                candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
                deterministic_config = types.GenerateContentConfig(temperature=0.0)

                image_type = 'RACE'
                classify_prompt = (
                    "送られた画像を判定してください。\n"
                    "・1R〜12Rなどの『全レース一覧・コース距離表』の画面であれば \"LIST\" と答えてください。\n"
                    "・『馬場情報（天候、芝・ダートの馬場状態）』の画面であれば \"BABA\" と答えてください。\n"
                    "・JRA-VANなどの『本日の傾向（馬番、騎手、脚質ごとの着順一覧）』画面であれば \"TREND\" と答えてください。\n"
                    "・それ以外の『出馬表（馬名やオッズが並ぶ画面）』であれば \"RACE\" と答えてください。\n"
                    "回答は \"LIST\"、\"BABA\"、\"TREND\"、\"RACE\" の英字1単語のみにしてください。"
                )
                
                for model_name in candidate_models:
                    try:
                        res = ai_client.models.generate_content(
                            model=model_name,
                            contents=raw_imgs + [classify_prompt],
                            config=deterministic_config
                        )
                        if res and res.text:
                            text_upper = clean_text(res.text).upper()
                            if 'LIST' in text_upper:
                                image_type = 'LIST'
                            elif 'BABA' in text_upper:
                                image_type = 'BABA'
                            elif 'TREND' in text_upper:
                                image_type = 'TREND'
                            break
                    except Exception:
                        continue

                # LIST
                if image_type == 'LIST':
                    extract_list_prompt = (
                        "この画像から【開催競馬場名】と【各レース(1R〜12R)のコース・距離・条件】、【開催節情報（例：1回〇〇4日）】を抽出し、以下のJSON形式のみで出力してください。\n"
                        "{\"keibajo\": \"競馬場名\", \"kai\": \"数字\", \"nichi\": \"数字\", \"races\": {\"1R\": \"ダ1700m\", \"2R\": \"芝1200m\"}}\n"
                        "※不明な項目は空文字 \"\" にし、無理に推測しないでください。JSON以外の文字列は出力禁止。"
                    )
                    list_json = None
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=[raw_imgs[0], extract_list_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                                list_json = force_parse_json(raw_text)
                                break
                        except Exception:
                            continue

                    if isinstance(list_json, dict) and list_json.get('keibajo'):
                        keibajo = clean_text(str(list_json.get('keibajo')))
                        kai = clean_text(str(list_json.get('kai', '')))
                        nichi = clean_text(str(list_json.get('nichi', '')))
                        
                        races_input = force_parse_json(list_json.get('races', {}))
                        cleaned_races_input = {}
                        if isinstance(races_input, dict):
                            cleaned_races_input = {clean_text(k): clean_text(v) for k, v in races_input.items()}

                        original_kj = str(list_json.get('keibajo'))
                        course_info = get_course_info(original_kj, kai, nichi) if kai and nichi else "開催区分"

                        current_list = load_json_file(RACE_LIST_FILE)
                        current_list[keibajo] = {
                            'races': cleaned_races_input,
                            'course_info': course_info
                        }
                        save_json_file(RACE_LIST_FILE, current_list)
                        send_to_gas_async('save_race_list', list_json)
                        reply_text = f"【本日の{original_kj}競馬場 全レース一覧・コース情報（{course_info}）を記憶しました】"
                    else:
                        reply_text = "⚠️ 全レース一覧の読み取りに失敗しました。画像を明るく撮影し直して再送してください。"

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # BABA
                elif image_type == 'BABA':
                    extract_baba_prompt = (
                        "この馬場情報画像から【競馬場名】、【天候】、【芝の馬場状態】、【ダートの馬場状態】を抽出し、以下のJSON形式のみで出力してください。\n"
                        "{\"keibajo\": \"競馬場名\", \"tenko\": \"天候\", \"shiba\": \"馬場\", \"dirt\": \"馬場\"}\n"
                        "※不明な項目は \"不明\" とし、JSON以外の文字列は含めないでください。"
                    )
                    baba_json = None
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=[raw_imgs[0], extract_baba_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                                baba_json = force_parse_json(raw_text)
                                break
                        except Exception:
                            continue

                    if isinstance(baba_json, dict) and baba_json.get('keibajo') and baba_json.get('keibajo') != "不明":
                        keibajo = clean_text(str(baba_json.get('keibajo')))
                        original_kj = str(baba_json.get('keibajo'))
                        current_baba = load_json_file(BABA_FILE)
                        current_baba[keibajo] = {
                            'tenko': clean_text(str(baba_json.get('tenko', '不明'))),
                            'shiba': clean_text(str(baba_json.get('shiba', '不明'))),
                            'dirt': clean_text(str(baba_json.get('dirt', '不明')))
                        }
                        save_json_file(BABA_FILE, current_baba)
                        send_to_gas_async('save_baba', baba_json)

                        reply_text = (
                            f"【本日の馬場情報を更新・記憶しました】\n"
                            f"📍 競馬場：【{original_kj}競馬場】\n"
                            f"🌤 天候：{current_baba[keibajo]['tenko']}\n"
                            f"🌿 芝：{current_baba[keibajo]['shiba']}\n"
                            f"🟫 ダート：{current_baba[keibajo]['dirt']}"
                        )
                    else:
                        reply_text = "⚠️ 馬場情報の読み取りに失敗しました。もう一度送信してください。"

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # TREND
                elif image_type == 'TREND':
                    extract_trend_prompt = (
                        "送られた画像（本日の傾向：馬番・騎手・脚質画面）から【競馬場名】と【各レースの好走馬（脚質傾向・馬番傾向・好調騎手）】を抽出し、以下のJSON形式のみで出力してください。\n"
                        "{\"keibajo\": \"競馬場名\", \"summary\": \"傾向の要約分析\"}\n"
                        "※不明な項目は \"不明\" とし、JSON以外の文字列は含めないでください。"
                    )
                    trend_json = None
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=raw_imgs + [extract_trend_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                                trend_json = force_parse_json(raw_text)
                                break
                        except Exception:
                            continue

                    if isinstance(trend_json, dict) and trend_json.get('keibajo') and trend_json.get('keibajo') != "不明":
                        keibajo = clean_text(str(trend_json.get('keibajo')))
                        original_kj = str(trend_json.get('keibajo'))
                        summary_str = str(trend_json.get('summary', ''))
                        current_trend = load_json_file(TREND_FILE)
                        current_trend[keibajo] = {
                            'summary': summary_str
                        }
                        save_json_file(TREND_FILE, current_trend)
                        send_to_gas_async('save_trend', trend_json)

                        reply_text = (
                            f"【本日の{original_kj}競馬場 リアルタイム傾向（バイアス）を記憶しました】\n"
                            f"📊 傾向分析：{summary_str}\n\n"
                            f"※後半レースの予想作成時にこのバイアスを組み込んで自動分析します。"
                        )
                    else:
                        reply_text = "⚠️ 本日の傾向画像の読み取りに失敗しました。もう一度送信してください。"

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # RACE (メイン予想ロジック)
                baba_data = load_json_file(BABA_FILE)
                if not baba_data:
                    gas_baba = fetch_baba_from_gas()
                    if gas_baba:
                        baba_data = gas_baba
                        save_json_file(BABA_FILE, baba_data)

                trend_data = load_json_file(TREND_FILE)
                if not trend_data:
                    gas_trend = fetch_trend_from_gas()
                    if gas_trend:
                        trend_data = gas_trend
                        save_json_file(TREND_FILE, trend_data)

                list_data = load_json_file(RACE_LIST_FILE)

                header_ocr_prompt = (
                    "画像最上部の緑色ヘッダーバーに印字されている文字（例: 『1回札幌8日 12R』）を、見たままそのまま文字として書き出してください。\n"
                    "回答はヘッダーの文字列のみとし、他の補足文は一切含めないでください。"
                )
                
                raw_header_text = ""
                for m_name in candidate_models:
                    try:
                        info_res = ai_client.models.generate_content(
                            model=m_name,
                            contents=[raw_imgs[0], header_ocr_prompt],
                            config=deterministic_config
                        )
                        if info_res and info_res.text:
                            raw_header_text = str(info_res.text).strip()
                            break
                    except Exception:
                        continue

                original_keibajo_name = "不明"
                for kj_candidate in JRA_KEIBAJO_LIST:
                    if kj_candidate in raw_header_text:
                        original_keibajo_name = kj_candidate
                        break

                keibajo_name = clean_text(original_keibajo_name)
                race_num_match = re.search(r'(\d+)\s*[Rr]', raw_header_text)
                race_num_only = race_num_match.group(1) if race_num_match else ""

                if not keibajo_name or keibajo_name == "不明" or not race_num_only:
                    reply_text = "⚠️ 競馬場名またはレース番号が正しく読み取れませんでした。\nJRAの緑色のヘッダー（〇回〇〇〇日 △R）が映っているスクリーンショットを送信してください。"
                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # 3段階解決ロジック
                track_type, distance_num = resolve_course_condition(keibajo_name, race_num_only, list_data, raw_imgs)

                if not track_type or not distance_num:
                    reply_text = (
                        f"⚠️ [{original_keibajo_name}{race_num_only}R] のコース条件の特定に失敗しました。\n"
                        f"お手数ですが、もう一度画像を送信してください。"
                    )
                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # コンテキスト構築
                baba_context_str = "【記憶されている本日のリアルタイム馬場・コース情報】\n"
                if baba_data:
                    for k, v in baba_data.items():
                        c_info = list_data.get(k, {}).get('course_info', '標準') if isinstance(list_data.get(k), dict) else '標準'
                        baba_context_str += f"・[{k}競馬場] 天候:{v.get('tenko')} / 芝:{v.get('shiba')} / ダ:{v.get('dirt')} / コース区分:{c_info}\n"
                else:
                    baba_context_str += "・未設定（標準の良馬場として判定）\n"

                trend_context_str = "【記憶されている本日のリアルタイム傾向（バイアス）情報】\n"
                if trend_data:
                    for k, v in trend_data.items():
                        trend_context_str += f"・[{k}競馬場] {v.get('summary')}\n"
                else:
                    trend_context_str += "・未設定（標準傾向として判定）\n"

                past_results_str = fetch_past_results_from_gas(original_keibajo_name, track_type, distance_num)
                past_data_context = ""
                if past_results_str:
                    past_data_context = (
                        f"\n【スプレッドシートから取得した[{original_keibajo_name} {track_type}{distance_num}m]の直近同条件過去データ（参照用）】\n"
                        "以下の過去同条件データから、該当コースの『勝ちタイム水準』『上がり3Fタイム限界値』『好走馬の4角通過順傾向』を抽出し、今回の出走馬の走破能力と照らし合わせて判定に反映させてください。\n"
                        "※直近（最新）の1〜2件の通過順・脚質傾向を特に強く評価してください。\n"
                        + past_results_str[:3000] + "\n"
                    )

                confirmed_condition_str = (
                    f"【絶対確定条件（※AI改変・推測禁止）】\n"
                    f"・対象レース：{original_keibajo_name}{race_num_only}R\n"
                    f"・コース種別：{track_type}\n"
                    f"・距離：{distance_num}m\n"
                    f"※画像の見た目や誤認識に惑わされず、上記【{original_keibajo_name}{race_num_only}R {track_type}{distance_num}m】を100%正解としてタイトルおよび分析文の前提に適用すること。\n\n"
                )

                prompt = (
                    "送られた全ての出馬表画像（1枚または複数枚）を解析してください。\n"
                    "【最重要原則】\n"
                    "送られた画像全体に映っている全出走馬（1番から最後の18番など大外馬まで）を必ず1つの統合出馬表として網羅・統合し、1頭も漏らさず評価対象にしてください。\n"
                    "※画像間で重複している馬番がある場合は、馬番をキーにして自動でダブりを削除し、全頭分を完全結合してください。\n\n"
                    + confirmed_condition_str
                    + baba_context_str
                    + trend_context_str
                    + past_data_context + "\n"
                    "【絶対厳守ルール（画像認識およびデータの誤読防止）】\n"
                    "1. タイトル表記：冒頭は必ず『【" + original_keibajo_name + race_num_only + "R " + track_type + distance_num + "m】』のように【絶対確定条件】をそのまま完全に記述すること。（「ダート」と「芝」の誤表記は厳禁）。\n"
                    "2. 枠番・馬番・馬名・騎手名の絶対同期と位置限定ルール：\n"
                    "   ・【枠番と馬番の識別】：カラー背景の数字は『枠番』です。馬番は白地列の数字（1, 2, 3...）です。印や買い目には必ず『馬番』のみを記述すること。\n"
                    "   ・【馬名と馬番の同一行（水平ライン）厳守】：各行はグレーの罫線で区切られています。馬番（白地の数字）の【真右の同一行にある馬名】のみを正としてセットにせよ。上下の行の馬名や馬番を跨いで交雑することを固く禁止する。\n"
                    "   ・【騎手名の抽出位置限定（最重要）】：今回該当レースに乗る『騎手名』は、勝負服（服のカラーイラスト）の直下にある文字（青文字等）のみを抽出せよ！右側の過去走（「前走」「前々走」等）の枠内に記載されている過去の騎手名を今回の騎手として読み取ることを固く禁止する！\n"
                    "3. 表の上下・行の誤読防止：同じ行の「馬番」「馬名」「今回騎手名」を正しくセットで読み取ること。\n"
                    "4. マークダウン太字記号『**』の使用は完全禁止とする。文字強調の記号は一切含めないこと。\n"
                    "5. 全頭リストや注釈・補足テキストなどの余計な項目は一切出力しないこと。\n"
                    "6. 【全領域ハルシネーション（誤認識・数値捏造）完全禁止ルール】：\n"
                    "   ・出馬表画像内に文字として直接印字されているデータのみを参照・記述すること。画像に表示されていない3走前以前の数値や記憶からの推測・補完・数値捏造を固く禁止する。\n"
                    "   ・過去走データは画像内の【前走】および【前々走】（最大2走分）の枠内に直接印字されている数値（着順・距離・コース・タイム・上がり3F・着差）のみを取り出すこと。\n"
                    "7. 【照合手順の固定化 ＆ 人気非依存・大穴救済ルール（最重要・絶対厳守）】：\n"
                    "   ・ステップ1【今回確定条件の確認】：今回の確定条件（" + track_type + distance_num + "m）を照合の絶対基準とする。\n"
                    "   ・ステップ2【前走・前々走データの条件照合】：各馬の『前走』『前々走』枠内からコース（芝/ダ）と距離（〇〇m）を読み取り、今回確定条件に対し『同コースかつ同距離（±200m以内）』に合致する走りを特定せよ。\n"
                    "   ・ステップ3【タイム・着差・一変条件の評価】：\n"
                    "     ① 人気順やオッズの数字を判断材料にすることを完全に禁止する（1番人気自動固定の徹底排除）。\n"
                    "     ② 近走の見た目の着順（二桁着順など）のみで一律除外することを固く禁止する（10番人気以下の大穴馬を無条件切り捨て禁止）。\n"
                    "     ③ 『前走が大敗でも前々走で同距離帯（±200m）で1着や高タイム・上がり上位実績がある馬』や『前走着順は二桁でも勝ち馬と0.5秒以内の僅小着差だった馬』、『前走不利・不適性距離から今回の得意条件への好転（距離変更・馬場変化等）』がある馬は、人気に関わらず◎（本命）・☆（穴馬）・△（連下）へ積極的に抜擢・評価せよ。\n"
                    "8. 【◎（本命）・☆（穴馬）・相手（◯〜△）の選定軸明確化ルール】：\n"
                    "   ・『◎本命』(1頭)：人気に関わらず（1番人気でも大穴でも）、1着で勝ち切る能力・展開バイアス適合・同距離帯タイム実績が最も高い馬を必ず1頭選定せよ。\n"
                    "   ・『☆穴馬』(1頭)：10番人気以下などを含め、前々走の同距離実績や前走着差・条件好転により激走一変する要素を持つ伏兵を1頭選定せよ。\n"
                    "   ・『◯対抗・▲単穴・△連下』：1〜3着（複勝圏内）に走破する確率（3着内率）が客観的に高い順に配置せよ。\n"
                    "   ・評価表記は『[3着内期待度：〇%]』に完全統一すること（☆穴馬は『[激走期待度：〇%] ※伏兵』）。\n"
                    "   ・△（連下）は出走頭数や混戦度に応じて2〜3頭選定し、全体で6〜7頭の印付き馬を選出してヒモ抜けを防止せよ。\n"
                    "9. レース波乱度の判定と可変評価：\n"
                    "   ・レース概要の波乱度は『順当』『混戦』『波乱』の3段階で判定すること。\n"
                    "     - 『順当』：前走・前々走の同距離帯での地力・タイムがダントツな馬を◎に固定。\n"
                    "     - 『混戦』：上位馬のタイム実績が拮抗し、本日バイアスと合致する馬を評価。\n"
                    "     - 『波乱』：ペース想定や前走不利・前々走好実績からの激走条件が揃った穴馬を積極的に◎・◯・☆に抜擢せよ。\n"
                    "10. 【波乱度に応じた券種固定 ＆ 買い目点数上限（絶対10点以内）ルール】：\n"
                    "   ・買い目の馬番は、必ず「■ 2. 印・推奨理由」で選定した◎◯▲☆△の馬番のみと100%完全一致・同期させよ！印に存在しない馬番の買い目追加や、◎を買い目から外すことを固く禁止する。\n"
                    "   ・波乱度の判定に応じて、以下の券種スタイルを自動選定せよ（特定例に引きずられずルールに従うこと）：\n"
                    "     - 『順当』判定時 ➔ 【馬単 1着固定流し】（軸◎ ➔ 相手◯▲☆△：計4〜5点）\n"
                    "     - 『混戦』判定時 ➔ 【3連複 1頭軸流し】（軸◎ ➔ 相手◯▲☆△：計6〜8点）\n"
                    "     - 『波乱』判定時 ➔ 【ワイド 流し】（軸◎または☆ ➔ 相手◯▲△：計3〜5点）または【馬連 流し】\n"
                    "   ・買い目の合計点数はどのような場合でも『最大10点以内』に絶対収めること。\n"
                    "   ・買い目構成（軸・相手など）は【必ず改行した縦並び】で視認性良く出力すること。\n"
                    "11. 過去データの照合：スプレッドシートの同条件過去データから勝ちタイム水準・上がり時計・直近の通過順傾向を参照し、今回の出走馬の数値と客観的に比較して根拠に組み込むこと。\n\n"
                    "【出力フォーマット（※指定以外の文字列・記号は一切追加禁止）】\n"
                    f"■ 1. レース概要：【{original_keibajo_name}{race_num_only}R {track_type}{distance_num}m】 [レース波乱度：順当／混戦／波乱から選定]\n"
                    "（展開・ペース想定・馬場・本日バイアス・過去データ照合に基づく分析）\n\n"
                    "■ 2. 印・推奨理由と3着内期待度\n"
                    "◎ 【本命】 〇番 馬名（今回騎手名） [3着内期待度：〇%]\n"
                    "（※推奨理由には1着で勝ち切る根拠、同距離帯タイム面、およびバイアス・展開適合を記述すること）\n"
                    "◯ 【対抗】 〇番 馬名（今回騎手名） [3着内期待度：〇%]\n"
                    "▲ 【単穴】 〇番 馬名（今回騎手名） [3着内期待度：〇%]\n"
                    "☆ 【穴馬】 〇番 馬名（今回騎手名） [激走期待度：〇%] ※伏兵\n"
                    "△ 【連下】 〇番 馬名（今回騎手名） [3着内期待度：〇%]\n"
                    "△ 【連下】 〇番 馬名（今回騎手名） [3着内期待度：〇%]\n"
                    "△ 【連下】 〇番 馬名（今回騎手名） [3着内期待度：〇%] ※混戦時のみ3頭目出力可\n\n"
                    "■ 3. おすすめの買い目\n"
                    "【選定した券種名をここに記述】\n"
                    "（※以下のように縦並び・10点以内で出力）\n"
                    "軸：〇\n"
                    "相手：〇, 〇, 〇, 〇\n"
                    "（計〇点）\n\n"
                    "※馬券購入は自己責任でお願いします"
                )

                content_list = proc_imgs + [prompt]
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
                            reply_text = reply_text.replace('**', '')
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
                        ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)])
                    )

            if current_timer is not None:
                current_timer.cancel()

            current_timer = threading.Timer(2.5, process_race_prediction)
            current_timer.start()

        except Exception as e:
            logging.error(f"System error: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
