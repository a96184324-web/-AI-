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
current_timer = None
latest_reply_token = None

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
            res_json = response.json()
            if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                return str(res_json.get('data', ''))
    except Exception as e:
        logging.error(f"Failed to fetch past results from GAS: {e}")
    return ""

def fetch_baba_from_gas():
    if not GAS_WEBAPP_URL:
        return {}
    try:
        payload = {
            'action': 'get_baba',
            'date': get_jst_today()
        }
        response = requests.post(
            GAS_WEBAPP_URL,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        if response.status_code == 200:
            res_json = response.json()
            if isinstance(res_json, dict) and res_json.get('status') == 'SUCCESS':
                data = res_json.get('data', {})
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logging.error(f"Failed to fetch baba info from GAS: {e}")
    return {}

def get_course_info(keibajo, kai, nichi):
    try:
        kai_str = f"{kai}回"
        nichi_num = int(nichi)
        if keibajo in COURSE_MASTER and kai_str in COURSE_MASTER[keibajo]:
            for course, day_range in COURSE_MASTER[keibajo][kai_str].items():
                if nichi_num in day_range:
                    return f"{course}コース（開幕{nichi_num}日目）"
    except Exception as e:
        logging.error(f"Course master lookup error: {e}")
    return f"開幕{nichi}日目"

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
    if len(processed_message_ids) > 200:
        processed_message_ids.clear()

    reply_text = (
        "メッセージありがとうございます！\n"
        "LINEからは【出馬表・馬場情報・レース一覧のスクショ画像】を送信してください。\n\n"
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
            processed_image = process_image_for_ocr(raw_image)

            with buffer_lock:
                image_buffer.append(processed_image)
                latest_reply_token = event.reply_token

            def process_race_prediction():
                global latest_reply_token, current_timer
                with buffer_lock:
                    imgs = list(image_buffer)
                    image_buffer.clear()
                    r_token = latest_reply_token
                    current_timer = None

                if not imgs or not r_token:
                    return

                candidate_models = ['gemini-3.1-flash-lite', 'gemini-3.5-flash']
                deterministic_config = types.GenerateContentConfig(temperature=0.0)

                image_type = 'RACE'
                if len(imgs) == 1:
                    classify_prompt = (
                        "送られた画像を判定してください。\n"
                        "・1R〜12Rなどの『全レース一覧・コース距離表』の画面であれば \"LIST\" と答えてください。\n"
                        "・『馬場情報（天候、芝・ダートの馬場状態）』の画面であれば \"BABA\" と答えてください。\n"
                        "・それ以外の『出馬表（馬名やオッズが並ぶ画面）』であれば \"RACE\" と答えてください。\n"
                        "回答は \"LIST\"、\"BABA\"、\"RACE\" の英字1単語のみにしてください。"
                    )
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=[imgs[0], classify_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                text_upper = str(res.text).strip().upper()
                                if 'LIST' in text_upper:
                                    image_type = 'LIST'
                                elif 'BABA' in text_upper:
                                    image_type = 'BABA'
                                break
                        except Exception:
                            continue

                if image_type == 'LIST':
                    extract_list_prompt = (
                        "この画像から【開催競馬場名】と【各レース(1R〜12R)のコース・距離・条件】、【開催節情報（例：1回札幌4日）】を抽出し、以下のJSON形式のみで出力してください。\n"
                        "{\"keibajo\": \"札幌\", \"kai\": \"1\", \"nichi\": \"4\", \"races\": {\"1R\": \"ダ1700m\", \"2R\": \"芝1200m\"}}\n"
                        "※JSON以外の文字列は含めないでください。"
                    )
                    list_json = None
                    for model_name in candidate_models:
                        try:
                            res = ai_client.models.generate_content(
                                model=model_name,
                                contents=[imgs[0], extract_list_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                                list_json = json.loads(raw_text)
                                break
                        except Exception:
                            continue

                    if isinstance(list_json, dict) and 'keibajo' in list_json:
                        keibajo = list_json.get('keibajo', '不明')
                        kai = list_json.get('kai', '1')
                        nichi = list_json.get('nichi', '1')
                        course_info = get_course_info(keibajo, kai, nichi)

                        current_list = load_json_file(RACE_LIST_FILE)
                        current_list[keibajo] = {
                            'races': list_json.get('races', {}),
                            'course_info': course_info
                        }
                        save_json_file(RACE_LIST_FILE, current_list)
                        send_to_gas_async('save_race_list', list_json)
                        reply_text = f"【本日の{keibajo}競馬場 全レース一覧・コース情報（{course_info}）を記憶しました】"
                    else:
                        reply_text = "⚠️ 全レース一覧の読み取りに失敗しました。もう一度送信してください。"

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

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
                                contents=[imgs[0], extract_baba_prompt],
                                config=deterministic_config
                            )
                            if res and res.text:
                                raw_text = str(res.text).replace('```json', '').replace('```', '').strip()
                                baba_json = json.loads(raw_text)
                                break
                        except Exception:
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

                    with ApiClient(configuration) as api_client_inner:
                        m_api = MessagingApi(api_client_inner)
                        m_api.reply_message(ReplyMessageRequest(reply_token=r_token, messages=[TextMessage(text=reply_text)]))
                    return

                # 【出馬表（RACE）全頭統合予想処理】
                baba_data = load_json_file(BABA_FILE)
                if not baba_data:
                    gas_baba = fetch_baba_from_gas()
                    if gas_baba:
                        baba_data = gas_baba
                        save_json_file(BABA_FILE, baba_data)

                list_data = load_json_file(RACE_LIST_FILE)

                race_info_prompt = (
                    "送られた全画像の中から【競馬場名（例: 中京、新潟）】と【レース番号（例: 1R、2R）】、【コース種別（芝またはダート）】、【距離（数字のみ）】を読み取り、\n"
                    "以下のJSON形式のみで出力してください。\n"
                    "{\"keibajo\": \"中京\", \"race_num\": \"2R\", \"track_type\": \"芝\", \"distance\": \"1400\"}\n"
                    "※JSON以外出力禁止。"
                )
                keibajo_name, race_num, track_type, distance_num = "", "", "", ""
                for m_name in candidate_models:
                    try:
                        info_res = ai_client.models.generate_content(
                            model=m_name,
                            contents=imgs + [race_info_prompt],
                            config=deterministic_config
                        )
                        if info_res and info_res.text:
                            raw_i = str(info_res.text).replace('```json', '').replace('```', '').strip()
                            info_json = json.loads(raw_i)
                            keibajo_name = str(info_json.get('keibajo', '')).strip()
                            raw_r = str(info_json.get('race_num', '')).strip()
                            if raw_r and not raw_r.endswith('R'):
                                race_num = f"{raw_r}R"
                            else:
                                race_num = raw_r
                            track_type = str(info_json.get('track_type', '')).strip()
                            distance_num = str(info_json.get('distance', '')).strip()
                            break
                    except Exception:
                        continue

                # 事前記憶された正解コース一覧（RACE_LIST_FILE）からの完全自動オーバーライド
                if keibajo_name in list_data and 'races' in list_data[keibajo_name]:
                    races_dict = list_data[keibajo_name].get('races', {})
                    if race_num in races_dict:
                        saved_condition = str(races_dict[race_num])
                        if "ダ" in saved_condition:
                            track_type = "ダート"
                        elif "芝" in saved_condition:
                            track_type = "芝"
                        
                        m = re.search(r'\d+', saved_condition)
                        if m:
                            distance_num = m.group(0)

                baba_context_str = "【記憶されている本日のリアルタイム馬場・コース情報】\n"
                if baba_data:
                    for k, v in baba_data.items():
                        c_info = list_data.get(k, {}).get('course_info', '標準') if isinstance(list_data.get(k), dict) else '標準'
                        baba_context_str += f"・[{k}競馬場] 天候:{v.get('tenko')} / 芝:{v.get('shiba')} / ダ:{v.get('dirt')} / コース区分:{c_info}\n"
                else:
                    baba_context_str += "・未設定（標準の良馬場として判定）\n"

                past_results_str = fetch_past_results_from_gas(keibajo_name, track_type, distance_num)
                past_data_context = ""
                if past_results_str:
                    past_data_context = (
                        f"\n【スプレッドシートから取得した[{keibajo_name} {track_type}{distance_num}m]の直近同条件過去データ（参照用）】\n"
                        "以下の過去同条件データから、該当コースの『勝ちタイム水準』『上がり3Fタイム限界値』『好走馬の4角通過順傾向』を抽出し、今回の出走馬の走破能力と照らし合わせて判定に反映させてください。\n"
                        "※直近（最新）の1〜2件の通過順・脚質傾向を特に強く評価してください。\n"
                        + past_results_str[:3000] + "\n"
                    )

                confirmed_condition_str = (
                    f"【絶対確定条件（※AI改変禁止）】\n"
                    f"・対象レース：{keibajo_name}{race_num}\n"
                    f"・コース種別：{track_type}\n"
                    f"・距離：{distance_num}m\n"
                    f"※画像の見た目や表記に惑わされず、上記【{keibajo_name}{race_num} {track_type}{distance_num}m】を100%正解としてタイトルおよび分析文の前提に適用してください。\n\n"
                )

                prompt = (
                    "送られた全ての出馬表画像（1枚または複数枚）を解析してください。\n"
                    "【最重要原則】\n"
                    "送られた画像全体に映っている全出走馬（1番から最後の18番など大外馬まで）を必ず1つの統合出馬表として網羅・統合し、1頭も漏らさず評価対象にしてください。\n"
                    "※画像間で重複している馬番がある場合は、馬番をキーにして自動でダブりを削除し、全頭分を完全結合してください。\n\n"
                    + confirmed_condition_str
                    + baba_context_str
                    + past_data_context + "\n"
                    "【絶対厳守ルール】\n"
                    f"1. タイトル表記：冒頭は必ず『【{keibajo_name}{race_num} {track_type}{distance_num}m】』のように【絶対確定条件】をそのまま完全に記述すること。（「ダート」と「芝」の誤表記は厳禁）。\n"
                    "2. マークダウン太字記号『**』の使用は完全禁止とする。文字強調の記号は一切含めないこと。\n"
                    "3. 全頭リストや注釈・補足テキストなどの余計な項目は一切出力しないこと。\n"
                    "4. 馬番全頭認識：画像内の1番から最後の馬番までの全頭を正確に読み取り評価すること。\n"
                    "5. 買い目整合性：『■ 3. おすすめの買い目』の馬番は、必ず『■ 2. 印・期待度と推奨理由』の印付き馬と完全一致させること。\n"
                    "6. 券種明記：『■ 3. おすすめの買い目』の1行目は、レースの堅実度や波乱度、本命馬の期待度に応じて「単勝」「馬連・ワイド」「3連複」「3連単」「馬単」等から最も期待値の高い最適な券種を選定して明記すること。\n"
                    "7. 過去データの照合：スプレッドシートの同条件過去データから勝ちタイム水準・上がり時計・直近の通過順傾向を参照し、今回の出走馬の数値と客観的に比較して根拠に組み込むこと。\n\n"
                    "【新・激走穴馬（☆）抜擢ロジック】\n"
                    "人気や前走の着順を完全に無視し、以下の数値トリガーを満たす伏兵馬を必ず☆（穴馬）または上位印に抜擢すること。\n"
                    "・前走不向きな展開（前残り馬場で後方から上がり上位を使って惨敗等）からの巻き返し\n"
                    "・今回大幅な斤量減（-2kg〜-4kg）または馬体重増減の改善\n"
                    "・前走と異なるトラックバイアス・距離変更での一変\n"
                    "・過去データで示したコース勝ちタイム水準・上がり時計に対応できる持ち時計・走破潜在力を持つ馬\n\n"
                    "【出力フォーマット（※指定以外の文字列・記号は一切追加禁止）】\n"
                    f"■ 1. レース概要：【{keibajo_name}{race_num} {track_type}{distance_num}m】 [レースの堅実度：A〜C]\n"
                    "（展開・馬場・ペース・過去データ照合に基づく分析）\n\n"
                    "■ 2. 印・期待度と推奨理由\n"
                    "◎ 【本命】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                    "◯ 【対抗】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                    "▲ 【単穴】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                    "☆ 【穴馬】 〇番 馬名（騎手名） [期待度：〇% / 評価：S〜B]\n"
                    "△ 【連下】 〇番 馬名（騎手名） [期待度：〇% / 評価：B〜C]\n\n"
                    "■ 3. おすすめの買い目\n"
                    "【選定券種：〇〇（※波乱度等に応じ「単勝・複勝」「馬連・ワイド」「3連複」「3連単」等から最適なものを記載）】\n"
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
