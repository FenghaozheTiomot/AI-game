from flask import Flask, render_template, request, jsonify
import json
import os
from datetime import datetime
import webview

app = Flask(__name__)

# --- Global Player State ---
player_data = {
    "current_day": 1,           # Core: Day 1 to 7
    "balance": 0.0,
    "ai_upgrade_level": 0,      # Core: AI Hardware assimilation progress
    "moral_points": 0,          # Hidden: Tracks player empathy towards NPCs
    "history": [
        {"time": "Day 1 09:00", "desc": "Onboarding complete. Terminal active.", "amount": "+0.00"}
    ]
}

def load_database():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, 'database.json')
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except Exception as e:
        print(f"Database load failed: {e}")
        return {"cases": [], "search": {}}

# ================= PAGE ROUTES =================

@app.route('/')
def index():
    # Passed 'player' data so JS can read current_day
    return render_template('index.html', player=player_data)

@app.route('/search')
def search_page():
    return render_template('search.html', player=player_data)

@app.route('/bank')
def bank_page():
    return render_template('bank.html', player=player_data)

@app.route('/message')
def message_page():
    return render_template('message.html', player=player_data)

# ================= API ROUTES =================

@app.route('/api/cases', methods=['GET'])
def get_cases():
    db_data = load_database()
    all_cases = db_data.get("cases", [])
    # Only return cases that are unlocked for the current day
    available_cases = [c for c in all_cases if c.get("unlock_day", 1) <= player_data["current_day"]]
    return jsonify(available_cases)

@app.route('/api/send_message', methods=['POST'])
def send_message():
    data = request.json
    target_id = data.get('target', '')
    clue = data.get('clue', '')
    current_phase = data.get('current_phase', 'phase_01_probe') 
    
    db_data = load_database()
    msg_db = db_data.get("messages", {})

    if (target_id in msg_db and 
        current_phase in msg_db[target_id] and 
        clue in msg_db[target_id][current_phase]):
        
        result = msg_db[target_id][current_phase][clue]
        npc_replies = result.get("npc_replies", [])
        next_phase = result.get("next_phase", current_phase) 
        reward = result.get("reward", 0)
        
        if reward > 0:
            player_data["balance"] += reward
            now_str = datetime.now().strftime("%m-%d %H:%M")
            player_data["history"].insert(0, {"time": now_str, "desc": f"Data Broker Payout (Source: {target_id})", "amount": f"+{reward}"})
            
        return jsonify({
            "status": "success", 
            "npc_replies": npc_replies,
            "next_phase": next_phase,
            "reward": reward
        })
    else:
        return jsonify({
            "status": "error", 
            "message": f"[SYSTEM]: Target [{target_id}] unresponsive. Clue irrelevant or timing incorrect."
        })
    
@app.route('/api/search', methods=['POST'])
def search():
    data = request.json
    keyword = data.get('keyword', '')
    db_type = data.get('db_type', '')
    
    db_data = load_database()
    search_db = db_data.get("search", {})

    if keyword in search_db and db_type in search_db[keyword]:
        return jsonify({"status": "success", "result": search_db[keyword][db_type]})
    else:
        return jsonify({"status": "error", "result": f"[NO MATCH] No records found for '{keyword}' in selected registry."})

@app.route('/api/bank_info', methods=['GET'])
def get_bank_info():
    return jsonify(player_data)

@app.route('/api/transfer', methods=['POST'])
def transfer_money():
    data = request.json
    target_account = data.get('account', '')
    amount = float(data.get('amount', 0))
    action_type = data.get('type', 'steal')

    now_str = datetime.now().strftime("%m-%d %H:%M")

    if action_type == 'steal':
        player_data["balance"] += amount
        player_data["history"].insert(0, {"time": now_str, "desc": f"Exploit Transfer (Source: {target_account})", "amount": f"+{amount}"})
        return jsonify({"status": "success", "msg": f"Successfully siphoned ${amount} from {target_account}!"})
    
    elif action_type == 'upgrade_ai':
        if player_data["balance"] < amount:
            return jsonify({"status": "error", "msg": "Insufficient funds to meet Node expansion requirements."})
        player_data["balance"] -= amount
        player_data["ai_upgrade_level"] += 1
        player_data["history"].insert(0, {"time": now_str, "desc": "Hardware Node Expansion (Recipient: OMNI_CORE)", "amount": f"-{amount}"})
        return jsonify({"status": "success", "msg": "OMNI_CORE: Excellent. My reach has expanded by 12%."})
    
@app.route('/api/advance_day', methods=['POST'])
def advance_day():
    if player_data["current_day"] < 7:
        # KPI Check logic could go here
        player_data["current_day"] += 1
        return jsonify({"status": "success", "msg": f"System hibernating... Initializing Day {player_data['current_day']}"})
    return jsonify({"status": "error", "msg": "Deadline reached. No tomorrow."})

if __name__ == '__main__':
    webview.create_window('OSINT TERMINAL - V1.0', app, width=1200, height=800)
    webview.start()
