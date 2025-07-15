import os
import re
import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request
import gspread
from telegram import (
    Bot, Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Dispatcher, CommandHandler, MessageHandler, Filters, ConversationHandler
)
from werkzeug.utils import secure_filename
from oauth2client.service_account import ServiceAccountCredentials
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# === Telegram Setup ===
BOT_TOKEN = "8045705611:AAEB8j3V_uyJbb_2uTmNE438xO1Y7G01yZM"
NGROK_URL = "https://check-bot-h94y.onrender.com"
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
bot = Bot(token=BOT_TOKEN)
app = Flask(__name__)
dispatcher = Dispatcher(bot, None, workers=1)

# === States ===
ASK_CONTACT, ASK_SLOT, ASK_QUESTION, ASK_IMAGE = range(4)

# === Google Setup ===
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_FILE = "service_account.json"
SHEET_NAME = "AOD Master App"
TAB_EMP = "EmployeeRegister"
TAB_CHECKLIST = "ChecklistQuestions"
TAB_RESPONSES = "Checklist Responses - Jatin"
TAB_ROSTER = "Roster"

# === Google Drive Upload ===
IMAGE_FOLDER = "checklist"
DRIVE_FOLDER_ID = "16lSRoFrCovjd9SgoIwZ8rwYwp3SyNW0Z"
os.makedirs(IMAGE_FOLDER, exist_ok=True)

def setup_drive():
    gauth = GoogleAuth()
    gauth.credentials = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE)
    return GoogleDrive(gauth)

drive = setup_drive()

# === Helpers ===
def normalize_number(number):
    return re.sub(r"\D", "", number)[-10:]

def sanitize_filename(name):
    return re.sub(r"[^a-zA-Z0-9_]", "", name.replace(" ", "_"))

def get_employee_info(phone):
    phone = normalize_number(phone)
    client = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE))
    emp_sheet = client.open(SHEET_NAME).worksheet(TAB_EMP)
    emp_records = emp_sheet.get_all_records()
    today = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d/%m/%Y")

    for row in emp_records:
        row_phone = normalize_number(str(row.get("Phone Number", "")))
        if row_phone == phone:
            emp_name = sanitize_filename(str(row.get("Full Name", "Unknown")))
            emp_id = str(row.get("Employee ID", ""))

            roster_sheet = client.open(SHEET_NAME).worksheet(TAB_ROSTER)
            roster_records = roster_sheet.get_all_records()

            for record in roster_records:
                if record.get("Employee ID") == emp_id and record.get("Date") == today:
                    return emp_name, record.get("Outlet")

    return "Unknown", ""

def get_filtered_questions(outlet, slot):
    client = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE))
    sheet = client.open(SHEET_NAME).worksheet(TAB_CHECKLIST)
    records = sheet.get_all_records()
    return [
        {
            "question": row.get("Question_Text", ""),
            "image_required": str(row.get("Image Required", "")).strip().lower() == "yes"
        }
        for row in records
        if str(row.get("Applicable Checklist", "")).strip().lower() == outlet.strip().lower()
        and str(row.get("Time_Slot", "")).strip().lower() == slot.strip().lower()
    ]

# === Bot Flow ===
def start(update: Update, context):
    contact_btn = KeyboardButton("📱 Send Phone Number", request_contact=True)
    update.message.reply_text("Please verify your phone number to continue:",
        reply_markup=ReplyKeyboardMarkup([[contact_btn]], resize_keyboard=True, one_time_keyboard=True))
    return ASK_CONTACT

def handle_contact(update: Update, context):
    if not update.message.contact:
        update.message.reply_text("❌ Please use the button to send your contact.")
        return ASK_CONTACT

    phone = normalize_number(update.message.contact.phone_number)
    emp_name, outlet = get_employee_info(phone)

    if emp_name == "Unknown" or not outlet:
        update.message.reply_text("❌ You're not rostered today or not registered in the system.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    context.user_data.update({"emp_name": emp_name, "outlet": outlet})
    update.message.reply_text("⏰ Select time slot:",
        reply_markup=ReplyKeyboardMarkup([["Morning", "Mid Day", "Closing"]], one_time_keyboard=True))
    return ASK_SLOT

def load_questions(update: Update, context):
    context.user_data["slot"] = update.message.text
    questions = get_filtered_questions(context.user_data["outlet"], context.user_data["slot"])
    if not questions:
        update.message.reply_text("❌ No checklist questions found.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    context.user_data.update({"questions": questions, "answers": [], "current_q": 0})
    return ask_next_question(update, context)

def ask_next_question(update: Update, context):
    idx = context.user_data["current_q"]
    if idx >= len(context.user_data["questions"]):
        update.message.reply_text("✅ All questions completed. Logging responses...", reply_markup=ReplyKeyboardRemove())

        try:
            client = gspread.authorize(ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SCOPE))
            sheet = client.open(SHEET_NAME).worksheet(TAB_RESPONSES)
            today = datetime.datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d/%m/%Y")
            outlet = context.user_data["outlet"]
            slot = context.user_data["slot"]
            emp_name = context.user_data["emp_name"].replace("_", " ")

            for item in context.user_data["answers"]:
                sheet.append_row([today, outlet, slot, emp_name, item["question"], item["answer"], item.get("image_link", "")])

            update.message.reply_text("✅ Responses saved to Google Sheets.")
        except Exception as e:
            update.message.reply_text(f"❌ Error saving to sheet: {e}")

        return ConversationHandler.END

    q_data = context.user_data["questions"][idx]
    update.message.reply_text(f"❓ {q_data['question']}",
        reply_markup=ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True))
    return ASK_QUESTION

def handle_answer(update: Update, context):
    ans = update.message.text
    q_data = context.user_data["questions"][context.user_data["current_q"]]
    context.user_data["answers"].append({"question": q_data["question"], "answer": ans})

    if q_data["image_required"]:
        update.message.reply_text("📷 Please upload image for this step.")
        return ASK_IMAGE

    context.user_data["current_q"] += 1
    return ask_next_question(update, context)

def handle_image_upload(update: Update, context):
    if update.message.photo:
        photo = update.message.photo[-1]
        file = photo.get_file()

        emp_name = context.user_data.get("emp_name", "User")
        time_slot = context.user_data.get("slot", "Unknown").replace(" ", "")
        q_num = context.user_data["current_q"] + 1
        filename = f"{time_slot}_{emp_name}_Q{q_num}.jpg"
        filepath = os.path.join(IMAGE_FOLDER, secure_filename(filename))

        file.download(custom_path=filepath)

        gfile = drive.CreateFile({
            'title': filename,
            'parents': [{'id': DRIVE_FOLDER_ID}],
            'supportsAllDrives': True
        })
        gfile.SetContentFile(filepath)
        gfile.Upload(param={'supportsAllDrives': True})

        file_id = gfile['id']
        web_link = f"https://drive.google.com/uc?export=view&id={file_id}"
        context.user_data["answers"][-1]["image_link"] = web_link

        try:
            os.remove(filepath)
        except Exception:
            pass

        update.message.reply_text("✅ Image uploaded.")
    else:
        update.message.reply_text("❌ Please upload a photo.")
        return ASK_IMAGE

    context.user_data["current_q"] += 1
    return ask_next_question(update, context)

def cancel(update: Update, context):
    update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK"

def setup_dispatcher():
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_CONTACT: [MessageHandler(Filters.contact, handle_contact)],
            ASK_SLOT: [MessageHandler(Filters.text & ~Filters.command, load_questions)],
            ASK_QUESTION: [MessageHandler(Filters.text & ~Filters.command, handle_answer)],
            ASK_IMAGE: [MessageHandler(Filters.photo, handle_image_upload)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    dispatcher.add_handler(conv_handler)

def set_webhook():
    url = f"{NGROK_URL}{WEBHOOK_PATH}"
    bot.set_webhook(url)
    print(f"✅ Webhook set to: {url}")

if __name__ == "__main__":
    setup_dispatcher()
    set_webhook()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
