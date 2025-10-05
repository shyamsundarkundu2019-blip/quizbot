#!/usr/bin/env python3
# coding: utf-8
"""
Telegram Quiz Bot - single-file updated
Features:
 - Subject Quiz (select subject from CSVs in quiz_data/)
 - Random Quiz (5/10/15/20)
 - Full Length Exam (50/85/100)
 - Per-chat Settings: negative marking ON/OFF, question timeout (seconds), summary delay
 - Results per subject / random / full
 - Leaderboard (total aggregated score across subjects)
 - Admin commands: /export_scores (CSV), /reset_scores
 - Thread-safe tracking of polls and answers
Requirements:
 - pyTelegramBotAPI (pip install pyTelegramBotAPI)
 - quiz_data/ directory with CSV files (headers: Question, Option A, Option B, Option C, Option D, Answer)
"""
import os
import csv
import time
import threading
import telebot
from telebot import types
from datetime import datetime
import random
import io

# ====== CONFIGURE THESE ======
TOKEN = "8440856766:AAE6IuMV5q3inJ8eCSot9sde-RxeIkHa2FU"   # <-- change to your bot token
DEFAULT_CHAT_ID = -922627571  # <-- default group chat id (change)
QUIZ_DIR = "quiz_data"
DELAY_BETWEEN_POLLS = 1.0
SUMMARY_WAIT_AFTER_LAST_POLL = 8
DEFAULT_QUESTION_TIMEOUT = 0  # Not used for polls but kept for future
ADMIN_IDS = [123456789]  # <-- fill with your Telegram user IDs who are admins
# =============================

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# ---------- Storage (in-memory) ----------
poll_info = {}       # poll_id -> {subject, q_index, correct_index, chat_id}
user_scores = {}     # (user_id, subject) -> {name, attempted, correct, wrong}
user_answers = {}    # (user_id, poll_id) -> selected_option_index
user_last_time = {}  # (user_id, subject) -> time string
lock = threading.Lock()

# chat settings: chat_id -> {negative_marking:bool, timeout:int, summary_delay:int}
chat_settings = {}

# ---------- Utilities ----------
def list_subjects():
    subjects = []
    if not os.path.exists(QUIZ_DIR):
        os.makedirs(QUIZ_DIR)
    for root, _, files in os.walk(QUIZ_DIR):
        for f in files:
            if f.lower().endswith(".csv"):
                rel = os.path.relpath(os.path.join(root, f), QUIZ_DIR)
                subjects.append(os.path.splitext(rel)[0].replace("\\", "/"))
    return sorted(subjects)

def load_questions(subject):
    path = os.path.join(QUIZ_DIR, f"{subject}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)

def all_questions_flat():
    out = []
    for s in list_subjects():
        qs = load_questions(s)
        for r in qs:
            out.append((s, r))
    return out

def safe_send_message(chat_id, text, markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e:
        print("Failed to send message:", e)
        return None

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_chat_settings(chat_id):
    defaults = {"negative_marking": True, "timeout": DEFAULT_QUESTION_TIMEOUT, "summary_delay": SUMMARY_WAIT_AFTER_LAST_POLL}
    s = chat_settings.setdefault(chat_id, {})
    for k, v in defaults.items():
        s.setdefault(k, v)
    return s

# ---------- Scoring & summaries ----------
def calc_score(correct, wrong, chat_id):
    neg_on = get_chat_settings(chat_id).get("negative_marking", True)
    return correct - (wrong * 0.25 if neg_on else 0)

def build_subject_summary_text(subject, chat_id):
    rows = []
    with lock:
        for (uid, sub), rec in user_scores.items():
            if sub != subject: continue
            attempted, correct, wrong = rec["attempted"], rec["correct"], rec["wrong"]
            score = calc_score(correct, wrong, chat_id)
            t = user_last_time.get((uid, sub), "--:--:--")
            rows.append((rec["name"], attempted, correct, wrong, score, t))
    if not rows:
        return f"📊 *{subject}* — কেউ অংশ নেয়নি।"
    rows.sort(key=lambda x: x[4], reverse=True)
    text = f"📊 *{subject}* RESULT\n"
    for name, attempted, correct, wrong, score, t in rows:
        text += f"\n👤 *{name}*\n✔️ {correct} | ❌ {wrong} | 📌 {attempted} | 🏆 {score:.2f} | 🕒 {t}\n"
    return text

def build_leaderboard_text(chat_id, top_n=10):
    totals = {}
    with lock:
        for (uid, sub), rec in user_scores.items():
            total_key = (uid, rec["name"])
            totals.setdefault(total_key, {"attempted":0,"correct":0,"wrong":0})
            totals[total_key]["attempted"] += rec["attempted"]
            totals[total_key]["correct"] += rec["correct"]
            totals[total_key]["wrong"] += rec["wrong"]
    if not totals:
        return "🏆 Leaderboard — no data yet."
    rows = []
    for (uid, name), rec in totals.items():
        score = calc_score(rec["correct"], rec["wrong"], chat_id)
        rows.append((name, rec["attempted"], rec["correct"], rec["wrong"], score))
    rows.sort(key=lambda x: x[4], reverse=True)
    text = "🏆 *Leaderboard*\n"
    for i, (name, attempted, correct, wrong, score) in enumerate(rows[:top_n], start=1):
        text += f"\n{i}. *{name}* — 🏆 {score:.2f} | ✔️{correct} | ❌{wrong} | 📌{attempted}\n"
    return text

# ---------- Poll sending helpers ----------
def record_poll(poll_obj, subject, q_index, correct_index, chat_id):
    poll_id = poll_obj.poll.id
    with lock:
        poll_info[poll_id] = {
            "subject": subject,
            "q_index": q_index,
            "correct_index": correct_index,
            "chat_id": chat_id
        }

def send_poll_for_row(chat_id, prefix, row, qnum, subject):
    qtext = row.get("Question") or ""
    options = [row.get("Option A") or "", row.get("Option B") or "",
               row.get("Option C") or "", row.get("Option D") or ""]
    answer_letter = (row.get("Answer") or "").strip().upper()
    correct_index = ord(answer_letter) - ord("A") if answer_letter in ["A","B","C","D"] else 0
    try:
        sent = bot.send_poll(
            chat_id,
            f"{prefix}{qnum}. {qtext}",
            options,
            type="quiz",
            correct_option_id=correct_index,
            is_anonymous=False
        )
        record_poll(sent, subject, qnum-1, correct_index, chat_id)
        return True
    except Exception as e:
        print("send_poll failed:", e)
        return False

# ---------- Quiz Routines ----------
def send_quiz_for_subject(subject, chat_id=DEFAULT_CHAT_ID):
    questions = load_questions(subject)
    if not questions:
        safe_send_message(chat_id, f"⚠️ Subject *{subject}* পাওয়া যায়নি।")
        return
    safe_send_message(chat_id, f"📖 *{subject.upper()}* Quiz শুরু হচ্ছে!\nমোট প্রশ্ন: {len(questions)}")
    for i, row in enumerate(questions, start=1):
        send_poll_for_row(chat_id, "Q", row, i, subject)
        time.sleep(DELAY_BETWEEN_POLLS)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 Result দেখুন", callback_data=f"result_subject:{subject}"))
    safe_send_message(chat_id, f"✅ Subject Quiz শেষ — *{subject}*", markup)

def send_random_quiz(count, chat_id=DEFAULT_CHAT_ID):
    all_qs = all_questions_flat()
    if not all_qs:
        safe_send_message(chat_id, "⚠️ কোনো প্রশ্ন পাওয়া যায়নি।")
        return
    count = max(1, min(count, len(all_qs)))
    sample = random.sample(all_qs, count)
    safe_send_message(chat_id, f"🎲 *Random Quiz* শুরু হচ্ছে! মোট: {count}")
    for idx, (subject, row) in enumerate(sample, start=1):
        # include subject in question heading for traceability
        send_poll_for_row(chat_id, f"Q{idx} _(subject:{subject})_ ", row, idx, subject)
        time.sleep(DELAY_BETWEEN_POLLS)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 Random Result দেখুন", callback_data="result_random"))
    safe_send_message(chat_id, "✅ Random Quiz Complete.", markup)

def send_full_length_exam(chat_id=DEFAULT_CHAT_ID, count=None):
    all_questions = []
    for subject in list_subjects():
        qs = load_questions(subject)
        # annotate rows with subject by copying is fine (we store subject in record_poll)
        for q in qs:
            all_questions.append((subject, q))
    if not all_questions:
        safe_send_message(chat_id, "⚠️ কোনো প্রশ্ন পাওয়া যায়নি।")
        return
    if count is not None:
        all_questions = all_questions[:count]
    safe_send_message(chat_id, f"📝 Full Length Exam শুরু হচ্ছে!\nমোট প্রশ্ন: {len(all_questions)}")
    for i, (subject, row) in enumerate(all_questions, start=1):
        send_poll_for_row(chat_id, "Q", row, i, "FullExam")
        time.sleep(DELAY_BETWEEN_POLLS)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📊 Full Exam Result দেখুন", callback_data="result_full"))
    safe_send_message(chat_id, "✅ Full Exam Complete.", markup)

# ---------- Poll Answer Handler ----------
@bot.poll_answer_handler()
def handle_poll_answer(poll):
    poll_id = poll.poll_id
    user = poll.user
    if not user:
        return
    user_id = user.id
    name = (user.first_name or "") + (" " + (user.last_name or ""))
    name = name.strip() or str(user_id)
    selected_list = poll.option_ids
    if not selected_list:
        return
    selected = selected_list[0]
    with lock:
        info = poll_info.get(poll_id)
        if not info:
            # Unknown poll (maybe older or from elsewhere)
            return
        subject = info["subject"]
        correct = info["correct_index"]
        key = (user_id, subject)
        rec = user_scores.setdefault(key, {"name": name, "attempted":0, "correct":0, "wrong":0})
        prev = user_answers.get((user_id, poll_id))
        if prev is None:
            user_answers[(user_id, poll_id)] = selected
            rec["attempted"] += 1
            if selected == correct:
                rec["correct"] += 1
            else:
                rec["wrong"] += 1
        # update last seen time for that subject
        user_last_time[key] = datetime.now().strftime("%H:%M:%S")

# ---------- Main Menu & Callbacks ----------
def show_main_menu(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📖 Subject Quiz", callback_data="menu_subjects"))
    markup.add(types.InlineKeyboardButton("🎲 Random Quiz", callback_data="menu_random"))
    markup.add(types.InlineKeyboardButton("📝 Full Length Exam", callback_data="menu_full_exam"))
    markup.add(types.InlineKeyboardButton("📊 Results", callback_data="menu_results"))
    markup.add(types.InlineKeyboardButton("🏆 Leaderboard", callback_data="menu_leaderboard"))
    markup.add(types.InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"))
    safe_send_message(chat_id, "👇 Main Menu:", markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    try:
        chat_id = call.message.chat.id
        data = call.data
        # answer callback to remove "loading" state
        bot.answer_callback_query(call.id, show_alert=False)
        # ---------- Subjects menu ----------
        if data == "menu_subjects":
            subjects = list_subjects()
            if not subjects:
                safe_send_message(chat_id, "⚠️ কোনো subject CSV পাওয়া যায়নি। `quiz_data/` ফোল্ডারে CSV যোগ করো।")
                return
            markup = types.InlineKeyboardMarkup(row_width=1)
            for s in subjects:
                markup.add(types.InlineKeyboardButton(s, callback_data=f"subject_run:{s}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send_message(chat_id, "📚 Subjects:", markup)
            return

        if data.startswith("subject_run:"):
            subject = data.split(":", 1)[1]
            send_quiz_for_subject(subject, chat_id)
            return

        # ---------- Random menu ----------
        if data == "menu_random":
            markup = types.InlineKeyboardMarkup()
            for n in [5,10,15,20]:
                markup.add(types.InlineKeyboardButton(f"🎲 {n} Questions", callback_data=f"random_{n}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send_message(chat_id, "🎲 Random Quiz কতগুলো প্রশ্ন চাইছো?", markup)
            return

        if data.startswith("random_"):
            count = int(data.split("_")[1])
            send_random_quiz(count, chat_id)
            return

        # ---------- Full exam ----------
        if data == "menu_full_exam":
            markup = types.InlineKeyboardMarkup()
            for n in [50,85,100]:
                markup.add(types.InlineKeyboardButton(f"📝 {n} Questions", callback_data=f"full_{n}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send_message(chat_id, "📝 Full Exam কতগুলো প্রশ্ন চাইছো?", markup)
            return

        if data.startswith("full_"):
            count = int(data.split("_")[1])
            send_full_length_exam(chat_id, count)
            return

        # ---------- Results ----------
        if data == "menu_results":
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📚 Per Subject Results", callback_data="results_subjects"))
            markup.add(types.InlineKeyboardButton("🎲 Random Results (All Subjects)", callback_data="result_random"))
            markup.add(types.InlineKeyboardButton("📝 Full Exam Result", callback_data="result_full"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send_message(chat_id, "📊 Results Menu:", markup)
            return

        if data == "results_subjects":
            subjects = list_subjects()
            if not subjects:
                safe_send_message(chat_id, "⚠️ কোনো subject CSV পাওয়া যায়নি।")
                return
            markup = types.InlineKeyboardMarkup()
            for s in subjects:
                markup.add(types.InlineKeyboardButton(s, callback_data=f"result_subject:{s}"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="menu_results"))
            safe_send_message(chat_id, "Select subject to view result:", markup)
            return

        if data.startswith("result_subject:"):
            subject = data.split(":",1)[1]
            safe_send_message(chat_id, build_subject_summary_text(subject, chat_id))
            return

        if data == "result_random":
            outputs = [build_subject_summary_text(s, chat_id) for s in list_subjects()]
            if outputs:
                safe_send_message(chat_id, "\n\n".join(outputs))
            else:
                safe_send_message(chat_id, "⚠️ কোনো প্রশ্ন পাওয়া যায়নি।")
            return

        if data == "result_full":
            safe_send_message(chat_id, build_subject_summary_text("FullExam", chat_id))
            return

        # ---------- Leaderboard ----------
        if data == "menu_leaderboard":
            safe_send_message(chat_id, build_leaderboard_text(chat_id))
            return

        # ---------- Settings ----------
        if data == "menu_settings":
            s = get_chat_settings(chat_id)
            markup = types.InlineKeyboardMarkup(row_width=1)
            neg_text = "ON" if s.get("negative_marking", True) else "OFF"
            markup.add(types.InlineKeyboardButton(f"❗ Negative Marking: {neg_text}", callback_data="toggle_negative"))
            markup.add(types.InlineKeyboardButton(f"⏱️ Summary Delay: {s.get('summary_delay', SUMMARY_WAIT_AFTER_LAST_POLL)}s", callback_data="set_summary_delay"))
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
            safe_send_message(chat_id, "⚙️ Settings:", markup)
            return

        if data == "toggle_negative":
            s = get_chat_settings(chat_id)
            s["negative_marking"] = not s.get("negative_marking", True)
            neg_text = "ON" if s["negative_marking"] else "OFF"
            safe_send_message(chat_id, f"✅ Negative marking now *{neg_text}*")
            # refresh settings menu
            handle_callbacks(types.SimpleNamespace(message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id)), data="menu_settings", id=call.id))
            return

        if data == "set_summary_delay":
            # For simplicity: cycle through some preset values
            s = get_chat_settings(chat_id)
            current = s.get("summary_delay", SUMMARY_WAIT_AFTER_LAST_POLL)
            choices = [5,8,12,20]
            nxt = choices[(choices.index(current) + 1) % len(choices)] if current in choices else choices[0]
            s["summary_delay"] = nxt
            safe_send_message(chat_id, f"✅ Summary delay updated to *{nxt} seconds*")
            handle_callbacks(types.SimpleNamespace(message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id)), data="menu_settings", id=call.id))
            return

        # ---------- Navigation ----------
        if data == "back_main":
            show_main_menu(chat_id)
            return

    except Exception as e:
        print("Error in callback handler:", e)

# ---------- Bot Commands ----------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    show_main_menu(message.chat.id)

@bot.message_handler(commands=["help"])
def cmd_help(message):
    txt = (
        "Usage:\n"
        "/start - Main menu\n"
        "/help - this help\n\n"
        "Admins:\n"
        "/export_scores - export scores CSV (admins only)\n"
        "/reset_scores - reset all stored scores (admins only)\n"
    )
    safe_send_message(message.chat.id, txt)

@bot.message_handler(commands=["export_scores"])
def cmd_export_scores(message):
    if not is_admin(message.from_user.id):
        safe_send_message(message.chat.id, "❌ এটি admin-only কমান্ড।")
        return
    # build CSV in-memory
    with lock:
        rows = []
        for (uid, subject), rec in user_scores.items():
            rows.append({
                "user_id": uid,
                "name": rec.get("name",""),
                "subject": subject,
                "attempted": rec.get("attempted",0),
                "correct": rec.get("correct",0),
                "wrong": rec.get("wrong",0)
            })
    if not rows:
        safe_send_message(message.chat.id, "⚠️ কোনো স্কোর পাওয়া যায়নি।")
        return
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["user_id","name","subject","attempted","correct","wrong"])
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    output.seek(0)
    try:
        bot.send_document(message.chat.id, ("scores_export.csv", output.read()))
    except Exception as e:
        safe_send_message(message.chat.id, "Failed to send export: " + str(e))

@bot.message_handler(commands=["reset_scores"])
def cmd_reset_scores(message):
    if not is_admin(message.from_user.id):
        safe_send_message(message.chat.id, "❌ এটি admin-only কমান্ড।")
        return
    with lock:
        user_scores.clear()
        user_answers.clear()
        user_last_time.clear()
    safe_send_message(message.chat.id, "✅ All scores reset.")

# ---------- Startup check & run ----------
if __name__ == "__main__":
    print("✅ Quiz Bot started...")
    print(" - Quiz directory:", QUIZ_DIR)
    print(" - Subjects found:", list_subjects())
    bot.infinity_polling()
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()
