from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import firebase_admin
from firebase_admin import credentials, auth
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import random
import json
import os
from openai import OpenAI
from google.cloud import firestore
from google.oauth2 import service_account
from pytz import timezone
from collections import Counter
from flask import session

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
CORS(app)

# Firebase Admin SDK
firebase_json_path = os.environ.get("FIREBASE_JSON_PATH")
cred = credentials.Certificate(firebase_json_path)
firebase_admin.initialize_app(cred)

# Firestore 初期化
firestore_credentials = service_account.Credentials.from_service_account_file(firebase_json_path)
db = firestore.Client(credentials=firestore_credentials, project="emotions-spark")

# OpenAI API設定
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Google Sheets API設定
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
gspread_json_path = os.environ.get("GSPREAD_JSON_PATH")
creds = ServiceAccountCredentials.from_json_keyfile_name(gspread_json_path, scope)
client_gs = gspread.authorize(creds)
spreadsheet = client_gs.open_by_url("https://docs.google.com/spreadsheets/d/1tZ36aRLuoGJDPjh1t1nt0SYouWOZYGnjEnDKNmOIHkI/edit?gid=0#gid=0")
sheet = spreadsheet.worksheet("ストック")  # ←シート名を明示

def get_stock_quote(emotion=None, scene=None, expand=False):
    records = sheet.get_all_records()
    filtered = []
    if scene:
        filtered = [r for r in records if r['シーン / Scene'] == scene]
    elif emotion:
        filtered = [r for r in records if r['感情 / Emotion'] == emotion]

    if not filtered:
        return [("その感情やシーンに合う名言がまだ登録されていません。", "", emotion or "", scene or "")]

    if expand:
        results = []
        for r in filtered[:5]:
            results.append((r['名言（JP）/ Quote_JP'], r['出典（JP）/ Author_JP'], r.get('感情 / Emotion', ''), r.get('シーン / Scene', '')))
        return results
    else:
        r = random.choice(filtered)
        return [(r['名言（JP）/ Quote_JP'], r['出典（JP）/ Author_JP'], r.get('感情 / Emotion', ''), r.get('シーン / Scene', ''))]

def generate_gpt_response(emotion):
    prompt = f"""
あなたは人の感情に寄り添い、やさしい名言とコメントをくれる賢者です。
以下の感情に対応する名言と寄り添い文をください：
感情：{emotion}
"""
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "あなたはメンタルケアの専門家であり、やさしく誠実な口調で答えます。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.9,
        max_tokens=300
    )
    return response.choices[0].message.content

def guess_scene_then_emotion_from_freeform(freeform_text):
    if not freeform_text.strip():
        return None, None

    prompt = f"""
以下の自由入力テキストから、まずもっとも適切な「シーン」を1つ選び、そのシーンにもっとも合う「感情」も1つ選んでください。
候補はリスト内からのみ選び、それ以外の単語を返さないでください。

**重要なルール**：
- 「やる気が出ない」「どうでもいい」「立ち上がれない」などの無力表現がある場合 →「前に進みたいのに動けない」「未来が怖いと感じたとき」などが適切。
- 「落ち込んでいる」などの語があっても、「悲しみ」ではなく「不安」「希望の欠如」も検討してください。
- 感情とシーンの整合性を大事にし、未来の動機づけに着目してください。

【シーン候補】:
["夜にひとりでいるとき", "朝が来るのが怖いとき", "眠れない夜", "誰かに傷つけられたあと",
 "自分を責めてしまうとき", "前に進みたいのに動けない", "誰にも話せないことがある",
 "なぜかわからないけど苦しい", "未来が怖いと感じたとき", "何かを失ったとき",
 "嫌なことを思い出したとき", "誰かを思い出したとき", "気持ちを整理したいとき",
 "とにかく救われたいとき", "夢が遠く感じるとき", "自分の価値がわからないとき", "泣きたくても泣けないとき"]

【感情候補】:
["悲しみ", "怒り", "喜び", "不安", "孤独", "愛情", "希望"]

テキスト：
「{freeform_text}」

# 出力形式（必ずこの形式で、他の文字列を混ぜない）:
{{
  "scene": "（上記のシーン候補から1つ）",
  "emotion": "（上記の感情候補から1つ）"
}}
    """

    res = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "あなたは感情と状況分類の専門家です。誤認を避け、文脈を大切にして厳密に分類してください。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=500
    )

    import re, json
    match = re.search(r'{.*}', res.choices[0].message.content, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            emotion = parsed.get("emotion")
            scene = parsed.get("scene")
            valid_emotions = ["悲しみ", "怒り", "喜び", "不安", "孤独", "愛情", "希望"]
            valid_scenes = [
                "夜にひとりでいるとき", "朝が来るのが怖いとき", "眠れない夜", "誰かに傷つけられたあと",
                "自分を責めてしまうとき", "前に進みたいのに動けない", "誰にも話せないことがある",
                "なぜかわからないけど苦しい", "未来が怖いと感じたとき", "何かを失ったとき",
                "嫌なことを思い出したとき", "誰かを思い出したとき", "気持ちを整理したいとき",
                "とにかく救われたいとき", "夢が遠く感じるとき", "自分の価値がわからないとき", "泣きたくても泣けないとき"
            ]
            if emotion in valid_emotions and scene in valid_scenes:
                return emotion, scene
        except:
            return None, None
    return None, None

def can_use_today():
    uid = session.get("uid")
    if not uid:
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    doc = db.collection("usage").document(uid).get()
    if doc.exists:
        return doc.to_dict().get("last_used_date") != today_str
    return True

def record_usage_today():
    uid = session.get("uid")
    if not uid:
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    db.collection("usage").document(uid).set({
        "last_used_date": today_str
    }, merge=True)

def log_usage_to_firestore(uid, email, emotion, scene, quote, author, gpt_response=None, freeform=None):
    try:
        data = {
            "uid": uid,
            "email": email,
            "emotion": emotion,
            "scene": scene,
            "quote": quote,
            "author": author,
            "timestamp": datetime.utcnow()
        }
        if gpt_response:
            data["gpt_response"] = gpt_response
        if freeform:
            data["freeform"] = freeform
        db.collection("logs").add(data)
        print("✅ Firestore 書き込み成功")
    except Exception as e:
        print("❌ Firestore 書き込み失敗:", e)

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/verify-token", methods=["POST"])
def verify_token():
    id_token = request.json.get("idToken")
    try:
        decoded_token = auth.verify_id_token(id_token)
        session["uid"] = decoded_token["uid"]
        session["email"] = decoded_token["email"]
        return jsonify({"status": "success", "uid": session["uid"]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 401

@app.route("/")
def index():
    if "uid" not in session:
        return redirect(url_for("login"))
    return render_template("index.html")

@app.route("/result", methods=["GET", "POST"])
def result():
    if "uid" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        emotion = request.form.get("emotion")
        scene = request.form.get("scene")
        freeform = request.form.get("freeform", "").strip()

        # 🚫 すでに使用済みなら、ここですぐ return（絶対にGPT呼ばせない）
        if freeform and not can_use_today():
            results = [(
                "※今日は自由入力での寄り添い名言は1回までです。\n\n"
                "でもご安心ください。\n\n"
                "感情やシーンを選べば、まだ他の名言を見ることができます🌱",
                "", "", ""
            )]
            session["selected_quotes"] = []
            return render_template(
                "result.html",
                results=results,
                emotion=None,
                scene=None,
                used_today=True,
                expand=False,
                freeform=freeform
            )

        # ✅ 使用可能な人だけ処理継続（ここで初めてGPT推定を許可）
        if freeform and can_use_today():
            record_usage_today()
            if not emotion or not scene:
                guessed_emotion, guessed_scene = guess_scene_then_emotion_from_freeform(freeform)
                if not emotion:
                    emotion = guessed_emotion
                if not scene:
                    scene = guessed_scene

        # --- 前回との違いをチェックしてリセット ---
        prev_emotion = session.get("last_emotion")
        prev_scene = session.get("last_scene")
        prev_freeform = session.get("last_freeform", "")

        if emotion != prev_emotion or scene != prev_scene or freeform != prev_freeform:
            session["expand_count"] = 0
            session.pop("first_quote", None)
            session["selected_quotes"] = []

        session["last_emotion"] = emotion
        session["last_scene"] = scene
        session["last_freeform"] = freeform

    else:
        # GETアクセス時：セッションから復元
        emotion = session.get("last_emotion")
        scene = session.get("last_scene")
        freeform = session.get("last_freeform", "")

    # --- 拡張表示クリック処理 ---
    if request.method == "GET" and request.args.get("expand", "false").lower() == "true":
        session["expand_count"] = session.get("expand_count", 0) + 1
    expand_count = session.get("expand_count", 0)
    num_additional = expand_count * 3
    total_count = 1 + num_additional

    # --- データ取得・フィルタ処理 ---
    records = sheet.get_all_records()
    if emotion:
        filtered = [r for r in records if r['感情 / Emotion'] == emotion]
    elif scene:
        filtered = [r for r in records if r['シーン / Scene'] == scene]
    else:
        filtered = []

    results = []

    if filtered:
        random.shuffle(filtered)
        selected_quotes = session.get("selected_quotes", [])
        selected_texts = [q[0] for q in selected_quotes]

        filtered = [r for r in filtered if r.get('名言（JP）/ Quote_JP', '') not in selected_texts]

        to_add = total_count - len(selected_quotes)
        new_quotes = []
        for r in filtered[:to_add]:
            quote = (
                r.get('名言（JP）/ Quote_JP', '該当なし'),
                r.get('出典（JP）/ Author_JP', ''),
                r.get('感情 / Emotion', ''),
                r.get('シーン / Scene', '')
            )
            new_quotes.append(quote)

        selected_quotes = new_quotes + selected_quotes
        session["selected_quotes"] = selected_quotes
        results = selected_quotes

        if request.method == "POST" and results:
            if freeform and not can_use_today():
                pass  # ログ記録しない
            else:
                first = results[0]
                session["first_quote"] = first
                log_usage_to_firestore(
                    uid=session["uid"],
                    email=session["email"],
                    emotion=first[2],
                    scene=first[3],
                    quote=first[0],
                    author=first[1],
                    freeform=freeform
                )

    else:
        results = [("その感情やシーンに合う名言がまだ登録されていません。", "", emotion or "", scene or "")]
        session["selected_quotes"] = []

    used_today = not can_use_today()
    return render_template(
        "result.html",
        results=results,
        emotion=emotion,
        scene=scene,
        used_today=used_today,
        expand=request.args.get("expand", "false").lower() == "true",
        freeform=freeform
    )

@app.route("/gpt")
def gpt():
    if "uid" not in session:
        return "未ログインです。ログインしてください。"
    if not can_use_today():
        return "※本日のGPT寄り添いはすでに使用済みです。"

    # 3つの入力を受け取る（emotion, scene, freeform）
    emotion = request.args.get("emotion")
    scene = request.args.get("scene")
    freeform = request.args.get("freeform")

    # emotion がない場合は scene や freeform から推定する
    if not emotion and scene:
        records = sheet.get_all_records()
        matched = [r for r in records if r["シーン / Scene"] == scene]
        if matched:
            emotion = matched[0]["感情 / Emotion"]

    if not emotion and freeform:
        guessed_emotion, _ = guess_scene_then_emotion_from_freeform(freeform)
        emotion = guessed_emotion

    # まだ emotion が特定できない場合はエラー
    if not emotion:
        return "うまく感情を特定できませんでした。"

    # GPT呼び出し
    gpt_output = generate_gpt_response(emotion)

    # Firestoreへの保存処理
    logs_ref = db.collection("logs")\
        .where("uid", "==", session["uid"])\
        .where("emotion", "==", emotion)\
        .order_by("timestamp", direction=firestore.Query.DESCENDING)\
        .limit(1)

    docs = logs_ref.stream()
    found = False
    for doc in docs:
        doc_ref = db.collection("logs").document(doc.id)
        doc_ref.update({"gpt_response": gpt_output})
        found = True
        print("✅ GPT追記に成功")

    if not found:
        log_usage_to_firestore(
            uid=session["uid"],
            email=session["email"],
            emotion=emotion,
            scene=scene or "",
            quote="",
            author="",
            gpt_response=gpt_output
        )

    record_usage_today()
    return gpt_output

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/history")
def history():
    if "uid" not in session:
        return redirect(url_for("login"))

    logs_ref = db.collection("logs").where("uid", "==", session["uid"]).order_by("timestamp", direction=firestore.Query.DESCENDING)
    logs = logs_ref.stream()

    jst = timezone('Asia/Tokyo')
    log_data = []

    for log in logs:
        item = log.to_dict()
        if "timestamp" in item and item["timestamp"]:
            timestamp = item["timestamp"].astimezone(jst).strftime("%Y-%m-%d %H:%M")
        else:
            timestamp = ""
        log_data.append({
            "emotion": item.get("emotion", ""),
            "scene": item.get("scene", ""),
            "quote": item.get("quote", ""),
            "author": item.get("author", ""),
            "gpt_response": item.get("gpt_response", ""),
            "freeform": item.get("freeform", ""),
            "timestamp": timestamp
        })

    return render_template("history.html", records=log_data)

if __name__ == "__main__":
    app.run(debug=True)