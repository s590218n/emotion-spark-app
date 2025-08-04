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
from flask import jsonify
from datetime import datetime
from flask import request

# .env 読み込み（ローカル用）
load_dotenv()

# 実行環境（local or render）
env = os.environ.get("ENV", "local")

# パスの読み分け
if env == "local":
    firebase_json_path = os.environ.get("FIREBASE_JSON_PATH_LOCAL")
    gspread_json_path = os.environ.get("GSPREAD_JSON_PATH_LOCAL")
else:
    # 🔥 Render上の環境変数（.envではなくDashboardで手動設定したもの）
    firebase_json_path = os.environ.get("FIREBASE_JSON_PATH_RENDER")
    gspread_json_path = os.environ.get("GSPREAD_JSON_PATH_RENDER")

# ファイル存在確認（ローカル開発用にだけ残してOK）
if not firebase_json_path:
    raise RuntimeError(f"Firebase JSON path is missing: {firebase_json_path}")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
CORS(app)

# Firebase Admin SDK
if not os.path.exists(firebase_json_path):
    firebase_json_path = "./firebase.json"  # 念のためローカル用に残してもOK

cred = credentials.Certificate(firebase_json_path)
firebase_admin.initialize_app(cred)

# Firestore 初期化
firestore_credentials = service_account.Credentials.from_service_account_file(firebase_json_path)
db = firestore.Client(credentials=firestore_credentials, project="emotions-spark")

# OpenAI API設定
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Google Sheets API設定
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
# Google Sheets API設定
# Renderでは存在確認せず、環境変数から受け取ったまま使用
pass  # gspread_json_path は既に設定済み

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

def get_three_random_quotes(filtered, seen_texts):
    seen_set = set(text.strip().lower() for text in seen_texts)

    # 重複しない候補を抽出
    unique_candidates = [
        (
            r.get('名言（JP）/ Quote_JP', '該当なし'),
            r.get('出典（JP）/ Author_JP', ''),
            r.get('感情 / Emotion', ''),
            r.get('シーン / Scene', '')
        )
        for r in filtered
        if r.get('名言（JP）/ Quote_JP', '').strip().lower() not in seen_set
    ]

    # 偏りを防ぐためにしっかりシャッフル
    random.shuffle(unique_candidates)

    # 最大3件を返す（なければ空リスト）
    return unique_candidates[:3]

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

def generate_gpt_response_from_prompt(prompt):
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
            valid_emotions = ["悲しみ", "不安", "怒り", "孤独", "喜び", "愛情", "希望", "罪悪感", "焦り", "混乱"]
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
    # 🔧 一時的に制限をオフ（何度でも使えるようにする）
    return True
    # 本来のコード（あとで元に戻すこと）
    """
    uid = session.get("uid")
    if not uid:
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")
    doc_ref = db.collection("usage").document(uid)
    doc = doc_ref.get()

    if doc.exists:
        data = doc.to_dict()
        if data.get("last_used_date") == today_str:
            return data.get("daily_gpt_count", 0) < 3
        else:
            return True  # 新しい日なのでOK
    return True
    """

def record_usage_today():
    uid = session.get("uid")
    if not uid:
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    doc_ref = db.collection("usage").document(uid)
    doc = doc_ref.get()

    if doc.exists:
        data = doc.to_dict()
        if data.get("last_used_date") == today_str:
            new_count = data.get("daily_gpt_count", 0) + 1
        else:
            new_count = 1
    else:
        new_count = 1

    doc_ref.set({
        "last_used_date": today_str,
        "daily_gpt_count": new_count
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

        # 🚫 自由入力があり、かつ本日すでに使用済み → APIなど一切呼ばずストップ
        if freeform and not can_use_today():
            # 🚫 今日の自由入力分はすでに使った → 名言履歴を完全にリセットして抜ける
            session.pop("first_quote", None)
            session["selected_quotes"] = []
            session["expand_count"] = 0
            session["last_emotion"] = None
            session["last_scene"] = None
            session["last_freeform"] = freeform

            results = [(
                "※今日は自由入力での寄り添い名言は1回までです。\n\n"
                "でもご安心ください。\n\n"
                "感情やシーンを選べば、まだ他の名言を見ることができます🌱",
                "", "", ""
            )]

            return render_template(
                "result.html",
                results=results,
                emotion=None,
                scene=None,
                used_today=True,
                expand=False,
                freeform=freeform
            )

        # ✅ 自由入力が初回使用OK → 使用記録＋推定
        if freeform and can_use_today():
            record_usage_today()
            if not emotion or not scene:
                guessed_emotion, guessed_scene = guess_scene_then_emotion_from_freeform(freeform)
                if not emotion:
                    emotion = guessed_emotion
                if not scene:
                    scene = guessed_scene

        # 🔁 前回との変化でセッションリセット
        prev_emotion = session.get("last_emotion")
        prev_scene = session.get("last_scene")
        prev_freeform = session.get("last_freeform", "")
        if emotion != prev_emotion or scene != prev_scene or freeform != prev_freeform:
            session["expand_count"] = 0
            session.pop("first_quote", None)
            session["selected_quotes"] = []
            session["seen_quotes"] = [] 

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
            # ←✅ filtered 定義がここに必要
            records = sheet.get_all_records()
            emotion = session.get("last_emotion")
            scene = session.get("last_scene")

            if emotion:
                filtered = [r for r in records if r['感情 / Emotion'] == emotion]
            elif scene:
                filtered = [r for r in records if r['シーン / Scene'] == scene]
            else:
                filtered = []

            seen_quotes = session.get("seen_quotes", [])
            seen_texts = [q[0] for q in seen_quotes]
            new_quotes = get_three_random_quotes(filtered, seen_texts)

    # --- データ取得・フィルタ処理 ---
    records = sheet.get_all_records()
    emotion = session.get("last_emotion")
    scene = session.get("last_scene")
    freeform = session.get("last_freeform", "")

    if emotion:
        filtered = [r for r in records if r['感情 / Emotion'] == emotion]
    elif scene:
        filtered = [r for r in records if r['シーン / Scene'] == scene]
    else:
        filtered = []

    results = []

    if filtered:
        # ✅ 表示済み名言リスト
        seen_quotes = session.get("seen_quotes", [])
        seen_texts = [q[0] for q in seen_quotes]

        # ✅ 未表示の候補だけ抽出
        new_candidates = [r for r in filtered if r.get('名言（JP）/ Quote_JP', '') not in seen_texts]

        if request.method == "GET" and request.args.get("expand", "false").lower() == "true":
            seen_quotes = session.get("seen_quotes", [])
            seen_texts = [q[0] for q in seen_quotes]
            
            # 重複しない3件の名言を取得
            new_quotes = get_three_random_quotes(filtered, seen_texts)

            if new_quotes:
                session["seen_quotes"] = seen_quotes + new_quotes
                results = new_quotes
            else:
                results = [("これ以上の名言は見つかりませんでした。", "", "", "")]

            if new_quotes:
                session["seen_quotes"] = session.get("seen_quotes", []) + new_quotes
                results = new_quotes
            else:
                results = [("これ以上の名言は見つかりませんでした。", "", "", "")]

        else:
            # ✅ 初回表示（POSTなど）
            seen_quotes = session.get("seen_quotes", [])
            seen_texts = [q[0] for q in seen_quotes]
            initial_quotes = get_three_random_quotes(filtered, seen_texts)

            session["seen_quotes"] = initial_quotes
            results = initial_quotes

            # 最初の3件をログ保存
            if request.method == "POST" and (not freeform or can_use_today()):
                for quote in initial_quotes:
                    log_usage_to_firestore(
                        uid=session["uid"],
                        email=session["email"],
                        emotion=quote[2],
                        scene=quote[3],
                        quote=quote[0],
                        author=quote[1],
                        freeform=freeform
                    )
    else:
        # ⚠️ 候補がまったくないとき
        results = [("その感情やシーンに合う名言がまだ登録されていません。", "", emotion or "", scene or "")]
        if request.method == "POST":
            if freeform and not can_use_today():
                pass
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

    # ✅ 最後に return を「if」「else」の外に1つだけ書く
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
    #if "uid" not in session:
     #   return "未ログインです。ログインしてください。"

    if not can_use_today():
        return "※本日のGPT寄り添いはすでに使用済みです。また明日🌙"

    # ✅ 安全な取得（None防止）
    emotion = request.args.get("emotion") or ""
    scene = request.args.get("scene") or ""
    freeform = request.args.get("freeform") or ""
    quote = request.args.get("quote") or ""
    author = request.args.get("author") or ""

    # ✅ ログ出力でRender側の確認
    print("▼ /gpt リクエスト")
    print("quote:", quote)
    print("author:", author)
    print("emotion:", emotion)
    print("scene:", scene)
    print("freeform:", freeform)

    # 🚫 入力が何もなければ拒否
    if not (freeform or quote or emotion or scene):
        return "自由入力または名言・感情・シーンのいずれかが必要です。"

    # 感情の推定
    if not emotion and scene:
        records = sheet.get_all_records()
        matched = [r for r in records if r["シーン / Scene"] == scene]
        if matched:
            emotion = matched[0]["感情 / Emotion"]

    if not emotion and freeform:
        guessed_emotion, _ = guess_scene_then_emotion_from_freeform(freeform)
        emotion = guessed_emotion

    if not emotion:
        return "うまく感情を特定できませんでした。"

    # GPTプロンプト生成
    if freeform:
        prompt = f"この言葉を受け取った人に、優しく寄り添う言葉をかけてください：『{freeform}』"
    elif quote:
        prompt = f"以下の名言を心に受け止めた人に、さらに寄り添うような一言を届けてください：『{quote}』（{author}）"
    elif scene:
        prompt = f"このようなシーンにいる人に、優しく寄り添う言葉をかけてください：「{scene}」"
    else:
        prompt = f"このような感情を抱える人に、優しく寄り添う言葉をかけてください：「{emotion}」"

    # ✅ GPT呼び出し with エラーハンドリング
    try:
        gpt_output = generate_gpt_response_from_prompt(prompt)
    except Exception as e:
        return f"⚠️ GPT呼び出し中にエラーが発生しました: {str(e)}"

    # Firestore保存処理
    logs_ref = db.collection("logs")\
        .where("uid", "==", session["uid"])\
        .where("emotion", "==", emotion)\
        .order_by("timestamp", direction=firestore.Query.DESCENDING)\
        .limit(1)
    docs = logs_ref.stream()
    found = False
    for doc in docs:
        db.collection("logs").document(doc.id).update({"gpt_response": gpt_output})
        found = True
    if not found:
        log_usage_to_firestore(
            uid=session["uid"],
            email=session["email"],
            emotion=emotion,
            scene=scene,
            quote=quote,
            author=author,
            gpt_response=gpt_output
        )

    record_usage_today()
    return gpt_output

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/favorite", methods=["POST"])
def favorite():
    if "uid" not in session:
        return jsonify({"status": "error", "message": "ログインしていません。"})

    data = request.json
    quote = data.get("quote")
    author = data.get("author")
    emotion = data.get("emotion")
    scene = data.get("scene")

    if not quote or not author:
        return jsonify({"status": "error", "message": "名言または著者が不足しています。"})

    try:
        db.collection("favorites").add({
            "uid": session["uid"],
            "email": session.get("email", ""),
            "quote": quote,
            "author": author,
            "emotion": emotion,
            "scene": scene,
            "timestamp": datetime.utcnow()
        })
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    
@app.route("/delete_favorite/<doc_id>", methods=["POST"])
def delete_favorite(doc_id):
    if "uid" not in session:
        return redirect(url_for("login"))

    try:
        db.collection("favorites").document(doc_id).delete()
        return redirect(url_for("favorites"))
    except Exception as e:
        return f"削除中にエラーが発生しました: {e}", 500
    
@app.route("/favorites")
def favorites():
    if "uid" not in session:
        return redirect(url_for("login"))

    uid = session["uid"]
    favorites_ref = db.collection("favorites").where("uid", "==", uid).order_by("timestamp", direction=firestore.Query.DESCENDING)
    docs = favorites_ref.stream()

    favorite_quotes = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        favorite_quotes.append({
            "id": data["id"],
            "quote": data.get("quote", ""),
            "author": data.get("author", ""),
            "emotion": data.get("emotion", ""),
            "scene": data.get("scene", ""),
            "timestamp": data.get("timestamp", "").astimezone(timezone("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
        })

    return render_template("favorites.html", favorites=favorite_quotes)


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

@app.route('/remove_favorite', methods=['POST'])
def remove_favorite():
    data = request.get_json()
    quote = data.get('quote')
    author = data.get('author')

    # 同じquote + author のドキュメントを削除
    docs = db.collection('favorites').where('quote', '==', quote).where('author', '==', author).stream()
    for doc in docs:
        db.collection('favorites').document(doc.id).delete()

    return jsonify({'status': 'removed'}), 200

@app.route('/emotion-map')
def emotion_map():
    from collections import defaultdict
    emotion_counts = defaultdict(int)

    # Firestoreからログデータ取得（uidフィルタを追加して自分の履歴だけ）
    uid = session.get("uid")
    if not uid:
        return redirect(url_for("login"))

    docs = db.collection('logs').where("uid", "==", uid).stream()
    for doc in docs:
        data = doc.to_dict()
        emotion = data.get("emotion")
        if emotion:
            emotion_counts[emotion] += 1

    # テンプレートに辞書を渡す
    return render_template("emotion_map.html", emotion_counts=dict(emotion_counts))

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)