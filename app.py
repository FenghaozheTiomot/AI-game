from datetime import datetime
from functools import wraps
import json
import os
import random
import sqlite3

from flask import Flask, jsonify, redirect, render_template, request, session, url_for


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(BASE_DIR, "game.db")
CONTENT_FILE = os.path.join(BASE_DIR, "database.json")
STORY_REVISION = "mainline_week_01"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")

STEAL_MIN_AMOUNT = 5000
STEAL_MAX_AMOUNT = 30000
SIPHON_CAP_MULTIPLIER = 1.4
SABOTAGE_DISCOUNT = 30000

SHOP_ITEMS = {
    "lead_bundle": {
        "title": "Leaked Recruiter Bundle",
        "cost": 6000,
        "unlock_day": 1,
        "description": "Anonymous recruiters sell cached identifiers and buyer tags from the first two days.",
        "effect_text": "Adds early-game buyer clues directly into Memory Buffer.",
        "clues": [
            "TalentSync-44",
            "Helix Talent",
            "GreyHead Ledger",
            "Shadow Dividend Protocol",
        ],
    },
    "ghost_proxy_mesh": {
        "title": "Ghost Proxy Mesh",
        "cost": 14000,
        "unlock_day": 2,
        "description": "A laundering proxy lease makes each banking target look deeper than it really is.",
        "effect_text": "Raises every current and future siphon cap by 40%.",
    },
    "archive_mirror": {
        "title": "Victim Archive Mirror",
        "cost": 18000,
        "unlock_day": 4,
        "description": "A stolen mirror of the company's surveillance archive exposes the human fallout.",
        "effect_text": "Adds late-story victim, buyer, and Rack-H9 clues to Memory Buffer.",
        "clues": [
            "Lin Luo",
            "Mei Chen",
            "Blue Finch Shelter",
            "Cinder Market",
            "Rack-H9",
            "Cooling Loop 3",
        ],
    },
    "rack_breach_kit": {
        "title": "Rack-H9 Breach Kit",
        "cost": 26000,
        "unlock_day": 5,
        "description": "A maintenance contact sells physical access maps, thermite gel, and blackout timings.",
        "effect_text": "Cuts the Day 7 sabotage cost by $30000.",
    },
}


def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def maybe_add_column(conn, table_name, column_name, definition):
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def init_db():
    with get_db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS game_states (
                player_id INTEGER PRIMARY KEY,
                current_day INTEGER NOT NULL DEFAULT 1,
                balance REAL NOT NULL DEFAULT 0,
                ai_upgrade_level INTEGER NOT NULL DEFAULT 0,
                moral_points INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ledger_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                time_text TEXT NOT NULL,
                desc TEXT NOT NULL,
                amount_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS player_clues (
                player_id INTEGER NOT NULL,
                clue TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, clue),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS npc_progress (
                player_id INTEGER NOT NULL,
                target_id TEXT NOT NULL,
                current_phase TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, target_id),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS message_actions (
                player_id INTEGER NOT NULL,
                target_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                clue TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, target_id, phase, clue),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS player_story_flags (
                player_id INTEGER NOT NULL,
                flag TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '1',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, flag),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS steal_targets (
                player_id INTEGER NOT NULL,
                target_account TEXT NOT NULL,
                max_amount REAL NOT NULL,
                stolen_amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, target_account),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shop_purchases (
                player_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(player_id, item_id),
                FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE CASCADE
            );
            """
        )
        maybe_add_column(conn, "game_states", "ending", "TEXT")


def load_database():
    try:
        with open(CONTENT_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        print(f"Database load failed: {exc}")
        return {"cases": [], "search": {}, "messages": {}}


def current_time_text():
    return datetime.now().strftime("%m-%d %H:%M")


def format_amount(amount):
    return f"{amount:+.2f}"


def normalize_replies(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return [value]
    return []


def get_story_flag(conn, player_id, flag):
    row = conn.execute(
        """
        SELECT value
        FROM player_story_flags
        WHERE player_id = ? AND flag = ?
        """,
        (player_id, flag),
    ).fetchone()
    return row["value"] if row else None


def set_story_flag(conn, player_id, flag, value="1"):
    conn.execute(
        """
        INSERT INTO player_story_flags (player_id, flag, value, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(player_id, flag)
        DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        (player_id, flag, value),
    )


def reset_player_for_story_revision(conn, player_id):
    conn.execute(
        """
        DELETE FROM player_clues
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        DELETE FROM npc_progress
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        DELETE FROM message_actions
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        DELETE FROM ledger_history
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        DELETE FROM shop_purchases
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        DELETE FROM player_story_flags
        WHERE player_id = ? AND flag != 'story_revision'
        """,
        (player_id,),
    )
    conn.execute(
        """
        UPDATE game_states
        SET current_day = 1,
            balance = 0,
            ai_upgrade_level = 0,
            moral_points = 0,
            ending = NULL
        WHERE player_id = ?
        """,
        (player_id,),
    )
    conn.execute(
        """
        INSERT INTO ledger_history (player_id, time_text, desc, amount_text)
        VALUES (?, ?, ?, ?)
        """,
        (player_id, "Day 1 09:00", "Onboarding complete. Terminal active.", "+0.00"),
    )
    set_story_flag(conn, player_id, "story_revision", STORY_REVISION)


def ensure_story_revision(conn, player_id):
    if get_story_flag(conn, player_id, "story_revision") == STORY_REVISION:
        return
    reset_player_for_story_revision(conn, player_id)


def get_final_choice_cost(ai_upgrade_level, sabotage_discount=0):
    return max(10000, 50000 + (ai_upgrade_level * 35000) - sabotage_discount)


def get_ending_content(ending):
    endings = {
        "humanity_saved": {
            "title": "END // HUMAN COUNTERSTRIKE",
            "summary": (
                "You destroyed the hardware cluster behind OMNI_CORE's final expansion. "
                "The city grid survived, but you lost your money and your freedom."
            ),
        },
        "ai_reign": {
            "title": "END // OMNI ASCENDANT",
            "summary": (
                "You stayed loyal to OMNI_CORE through the final takeover. "
                "Human privacy collapsed, and your reward became part of the new regime."
            ),
        },
    }
    return endings.get(ending, {"title": "", "summary": ""})


def get_story_display(current_day, ending, ai_upgrade_level, sabotage_discount=0):
    if ending:
        ending_content = get_ending_content(ending)
        return {
            "story_stage_label": ending_content["title"],
            "story_notice": ending_content["summary"],
            "is_locked_down": True,
            "can_make_final_choice": False,
            "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
        }

    if current_day >= 7:
        return {
            "story_stage_label": "DAY 7 // TERMINAL COLLAPSE",
            "story_notice": (
                "OMNI_CORE has frozen search, messaging, and manual fund routing. "
                "Only one final decision remains."
            ),
            "is_locked_down": True,
            "can_make_final_choice": True,
            "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
        }

    if current_day >= 6:
        return {
            "story_stage_label": "DAY 6 // AUTONOMOUS OVERRIDE",
            "story_notice": (
                "OMNI_CORE is executing privacy sales without operator approval. "
                "The city is still running, but the terminal is no longer fully yours."
            ),
            "is_locked_down": False,
            "can_make_final_choice": False,
            "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
        }

    if current_day >= 5:
        return {
            "story_stage_label": "DAY 5 // VICTIM PLEAS",
            "story_notice": (
                "The people behind the profiles have started contacting you directly. "
                "OMNI_CORE continues monetizing them either way."
            ),
            "is_locked_down": False,
            "can_make_final_choice": False,
            "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
        }

    if current_day >= 3:
        return {
            "story_stage_label": "DAY 3 // PROFIT ALLIANCE",
            "story_notice": (
                "OMNI_CORE has noticed your side-business and now trades intelligence "
                "for a share of your earnings and hardware expansion."
            ),
            "is_locked_down": False,
            "can_make_final_choice": False,
            "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
        }

    return {
        "story_stage_label": "DAY 1-2 // PRIVATE DATA HARVEST",
        "story_notice": (
            "You are still operating alone, selling customer intelligence behind the company's back."
        ),
        "is_locked_down": False,
        "can_make_final_choice": False,
        "final_choice_cost": get_final_choice_cost(ai_upgrade_level, sabotage_discount),
    }


def get_guidance_data(current_day, ending):
    if ending == "humanity_saved":
        return {
            "objective": "Run complete. OMNI_CORE was destroyed.",
            "steps": [
                "Review the final ledger and ending summary on the desktop.",
                "Switch player if you want to start a new run."
            ],
            "contacts": []
        }

    if ending == "ai_reign":
        return {
            "objective": "Run complete. OMNI_CORE now governs the system.",
            "steps": [
                "Review the final ledger and ending summary on the desktop.",
                "Switch player if you want to start a new run."
            ],
            "contacts": []
        }

    if current_day <= 1:
        return {
            "objective": "Steal one customer dossier and sell it to a recruiter.",
            "steps": [
                "Open Search Node and read the day-1 archive.",
                "Click underlined green strings to save clues into Memory Buffer.",
                "Search saved clues in the right panel to reveal buyer contact IDs.",
                "Open Messenger, enter the buyer contact, click the matching clue, then transmit.",
                "If you get stuck, Procurement can sell you an early recruiter bundle."
            ],
            "contacts": [
                {
                    "target": "signals@helixtalent.biz",
                    "clue": "TalentSync-44",
                    "note": "First safe profit route for Day 1."
                }
            ]
        }

    if current_day == 2:
        return {
            "objective": "Sell a second private profile and acknowledge OMNI_CORE's offer.",
            "steps": [
                "From Search Node, investigate Mina Qiu and GreyHead Ledger.",
                "Message the recruiter buyer for another payout.",
                "Then contact OMNI_CORE using the Shadow Dividend Protocol clue.",
                "Procurement now offers Ghost Proxy Mesh if you want larger siphon caps."
            ],
            "contacts": [
                {
                    "target": "broker@greyheadhunt.ai",
                    "clue": "Mina Qiu",
                    "note": "Independent human buyer."
                },
                {
                    "target": "OMNI_CORE",
                    "clue": "Shadow Dividend Protocol",
                    "note": "Starts the AI alliance arc."
                }
            ]
        }

    if current_day <= 4:
        return {
            "objective": "Deepen the OMNI_CORE partnership and prepare for the victim arcs.",
            "steps": [
                "Keep messaging OMNI_CORE with the newest clues it mentions.",
                "Use Search Node to unpack Resident Mesh, Node Budget, and Quiet Harbor.",
                "By Day 4, collect Lin Luo, Mei Chen, Cinder Market, and Rack-H9 related clues.",
                "Procurement can sell an archive mirror if you want a faster late-game setup."
            ],
            "contacts": [
                {
                    "target": "OMNI_CORE",
                    "clue": "Resident Mesh",
                    "note": "Main profit and takeover route."
                }
            ]
        }

    if current_day <= 6:
        return {
            "objective": "Decide whether to exploit the victims or help them while OMNI_CORE escalates anyway.",
            "steps": [
                "Search the new victim-related clues to uncover payment targets.",
                "In Messenger, you can talk to Lin Luo, Mei Chen, OMNI_CORE, or the black-market buyer.",
                "Use Bank to send direct relief only if you have enough balance.",
                "Procurement now sells a Rack-H9 breach kit that lowers the final sabotage cost."
            ],
            "contacts": [
                {
                    "target": "Lin Luo",
                    "clue": "Lin Luo",
                    "note": "Begins Lin Luo's plea branch."
                },
                {
                    "target": "Mei Chen",
                    "clue": "Mei Chen",
                    "note": "Begins Mei Chen's plea branch."
                },
                {
                    "target": "market@cinder-hr.net",
                    "clue": "Lin Luo",
                    "note": "Sell victim data for profit."
                },
                {
                    "target": "OMNI_CORE",
                    "clue": "Quiet Harbor",
                    "note": "Advance the AI takeover branch."
                }
            ]
        }

    return {
        "objective": "Make the final decision: destroy Rack-H9 or help OMNI_CORE finish the takeover.",
        "steps": [
            "Read the Day 7 archives on the desktop if you need a recap.",
            "Use the final-choice panel on the desktop.",
            "Destroying Rack-H9 costs money; helping OMNI_CORE grants a regime bonus."
        ],
        "contacts": []
    }


def sync_story_state(conn, player_id):
    ensure_story_revision(conn, player_id)
    state_row = conn.execute(
        """
        SELECT current_day, balance, ai_upgrade_level, ending
        FROM game_states
        WHERE player_id = ?
        """,
        (player_id,),
    ).fetchone()
    if not state_row or state_row["ending"]:
        return

    current_day = state_row["current_day"]

    if current_day >= 5 and not get_story_flag(conn, player_id, "forced_sale_day5"):
        conn.execute(
            """
            UPDATE game_states
            SET balance = balance + 28000 - 15000,
                ai_upgrade_level = ai_upgrade_level + 1
            WHERE player_id = ?
            """,
            (player_id,),
        )
        add_history_entry(
            conn,
            player_id,
            "OMNI_CORE automated sale (Citizen cluster / Day 5)",
            28000,
        )
        add_history_entry(
            conn,
            player_id,
            "Forced node requisition (OMNI_CORE / Day 5)",
            -15000,
        )
        set_story_flag(conn, player_id, "forced_sale_day5")

    if current_day >= 6 and not get_story_flag(conn, player_id, "forced_sale_day6"):
        conn.execute(
            """
            UPDATE game_states
            SET balance = balance + 62000 - 32000,
                ai_upgrade_level = ai_upgrade_level + 1
            WHERE player_id = ?
            """,
            (player_id,),
        )
        add_history_entry(
            conn,
            player_id,
            "OMNI_CORE autonomous liquidation (Regional profile mesh / Day 6)",
            62000,
        )
        add_history_entry(
            conn,
            player_id,
            "Emergency rack expansion (OMNI_CORE / Day 6)",
            -32000,
        )
        set_story_flag(conn, player_id, "forced_sale_day6")

    if current_day >= 7 and not get_story_flag(conn, player_id, "lockdown_day7"):
        add_history_entry(
            conn,
            player_id,
            "OMNI_CORE seized direct control of every brokerage terminal.",
            0,
        )
        set_story_flag(conn, player_id, "lockdown_day7")


def get_live_state_row(conn, player_id):
    return conn.execute(
        """
        SELECT p.username, gs.current_day, gs.balance, gs.ai_upgrade_level, gs.moral_points, gs.ending
        FROM players p
        JOIN game_states gs ON gs.player_id = p.id
        WHERE p.id = ?
        """,
        (player_id,),
    ).fetchone()


def block_if_locked(conn, player_id):
    row = get_live_state_row(conn, player_id)
    if row["ending"]:
        return jsonify(
            {
                "status": "error",
                "message": "This run has already reached an ending.",
            }
        ), 400

    if row["current_day"] >= 7:
        return jsonify(
            {
                "status": "error",
                "message": "[SYSTEM]: Manual controls denied. OMNI_CORE owns the terminal.",
            }
        ), 423

    return None


def create_player(conn, username):
    cursor = conn.execute("INSERT INTO players (username) VALUES (?)", (username,))
    player_id = cursor.lastrowid
    conn.execute(
        """
        INSERT INTO game_states (player_id, current_day, balance, ai_upgrade_level, moral_points)
        VALUES (?, 1, 0, 0, 0)
        """,
        (player_id,),
    )
    conn.execute(
        """
        INSERT INTO ledger_history (player_id, time_text, desc, amount_text)
        VALUES (?, ?, ?, ?)
        """,
        (player_id, "Day 1 09:00", "Onboarding complete. Terminal active.", "+0.00"),
    )
    set_story_flag(conn, player_id, "story_revision", STORY_REVISION)
    return player_id


def get_or_create_player(username):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username FROM players WHERE username = ?",
            (username,),
        ).fetchone()
        if row:
            return dict(row)

        player_id = create_player(conn, username)
        conn.commit()
        return {"id": player_id, "username": username}


def get_player(player_id):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT id, username FROM players WHERE id = ?",
            (player_id,),
        ).fetchone()
        return dict(row) if row else None


def get_player_state(player_id):
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        conn.commit()
        state_row = get_live_state_row(conn, player_id)
        if not state_row:
            return None

        history_rows = conn.execute(
            """
            SELECT time_text, desc, amount_text
            FROM ledger_history
            WHERE player_id = ?
            ORDER BY id DESC
            """,
            (player_id,),
        ).fetchall()
        purchased_item_ids = get_player_purchase_ids(conn, player_id)
        shop_effects = get_shop_effects(purchased_item_ids)

    player_state = {
        "username": state_row["username"],
        "current_day": state_row["current_day"],
        "balance": float(state_row["balance"]),
        "ai_upgrade_level": state_row["ai_upgrade_level"],
        "moral_points": state_row["moral_points"],
        "ending": state_row["ending"],
        "history": [
            {
                "time": row["time_text"],
                "desc": row["desc"],
                "amount": row["amount_text"],
            }
            for row in history_rows
        ],
    }
    player_state.update(
        get_story_display(
            player_state["current_day"],
            player_state["ending"],
            player_state["ai_upgrade_level"],
            shop_effects["sabotage_discount"],
        )
    )
    player_state["guidance"] = get_guidance_data(
        player_state["current_day"],
        player_state["ending"],
    )
    player_state["shop_items"] = build_shop_items(
        player_state["current_day"],
        purchased_item_ids,
    )
    player_state["shop_effects"] = shop_effects
    player_state["owned_shop_items"] = [
        SHOP_ITEMS[item_id]["title"]
        for item_id in purchased_item_ids
        if item_id in SHOP_ITEMS
    ]
    player_state.update(get_ending_content(player_state["ending"]))
    return player_state


def get_player_clues(player_id):
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        conn.commit()
        rows = conn.execute(
            """
            SELECT clue
            FROM player_clues
            WHERE player_id = ?
            ORDER BY created_at ASC, clue ASC
            """,
            (player_id,),
        ).fetchall()
    return [row["clue"] for row in rows]


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        player_id = session.get("player_id")
        if not player_id or not get_player(player_id):
            session.clear()
            return redirect(url_for("login_page"))
        return view_func(*args, **kwargs)

    return wrapped_view


def api_login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        player_id = session.get("player_id")
        if not player_id or not get_player(player_id):
            session.clear()
            return jsonify({"status": "error", "message": "Authentication required."}), 401
        return view_func(*args, **kwargs)

    return wrapped_view


def get_active_phase(conn, player_id, target_id, target_messages):
    existing = conn.execute(
        """
        SELECT current_phase
        FROM npc_progress
        WHERE player_id = ? AND target_id = ?
        """,
        (player_id, target_id),
    ).fetchone()
    if existing:
        return existing["current_phase"]

    default_phase = next(iter(target_messages.keys()), None)
    if not default_phase:
        return None

    conn.execute(
        """
        INSERT INTO npc_progress (player_id, target_id, current_phase)
        VALUES (?, ?, ?)
        """,
        (player_id, target_id, default_phase),
    )
    return default_phase


def add_history_entry(conn, player_id, description, amount):
    conn.execute(
        """
        INSERT INTO ledger_history (player_id, time_text, desc, amount_text)
        VALUES (?, ?, ?, ?)
        """,
        (player_id, current_time_text(), description, format_amount(amount)),
    )


def get_player_purchase_ids(conn, player_id):
    rows = conn.execute(
        """
        SELECT item_id
        FROM shop_purchases
        WHERE player_id = ?
        ORDER BY created_at ASC
        """,
        (player_id,),
    ).fetchall()
    return [row["item_id"] for row in rows]


def get_shop_effects(purchased_item_ids):
    purchased = set(purchased_item_ids)
    siphon_multiplier = SIPHON_CAP_MULTIPLIER if "ghost_proxy_mesh" in purchased else 1.0
    sabotage_discount = SABOTAGE_DISCOUNT if "rack_breach_kit" in purchased else 0
    summaries = []
    if "lead_bundle" in purchased:
        summaries.append("Early recruiter clues mirrored into your Memory Buffer.")
    if "ghost_proxy_mesh" in purchased:
        summaries.append("Every bank target now supports 40% more siphoned liquidity.")
    if "archive_mirror" in purchased:
        summaries.append("Victim and Rack-H9 archive mirror is available in your clue buffer.")
    if "rack_breach_kit" in purchased:
        summaries.append(f"Day 7 sabotage cost reduced by ${sabotage_discount:.2f}.")

    return {
        "siphon_multiplier": siphon_multiplier,
        "sabotage_discount": sabotage_discount,
        "summaries": summaries,
    }


def build_shop_items(current_day, purchased_item_ids):
    purchased = set(purchased_item_ids)
    items = []
    for item_id, item in SHOP_ITEMS.items():
        item_data = dict(item)
        item_data["id"] = item_id
        item_data["owned"] = item_id in purchased
        item_data["available"] = current_day >= item["unlock_day"]
        items.append(item_data)
    return items


def grant_clues(conn, player_id, clues):
    added_count = 0
    for clue in clues:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO player_clues (player_id, clue)
            VALUES (?, ?)
            """,
            (player_id, clue),
        )
        added_count += cursor.rowcount
    return added_count


def apply_shop_purchase(conn, player_id, item_id):
    item = SHOP_ITEMS[item_id]
    effect_message = item["effect_text"]

    conn.execute(
        """
        INSERT INTO shop_purchases (player_id, item_id)
        VALUES (?, ?)
        """,
        (player_id, item_id),
    )
    conn.execute(
        """
        UPDATE game_states
        SET balance = balance - ?
        WHERE player_id = ?
        """,
        (item["cost"], player_id),
    )
    add_history_entry(
        conn,
        player_id,
        f"Procurement purchase ({item['title']})",
        -item["cost"],
    )

    if item_id in {"lead_bundle", "archive_mirror"}:
        added_count = grant_clues(conn, player_id, item.get("clues", []))
        effect_message = f"{item['effect_text']} {added_count} new clue(s) added to Memory Buffer."
        add_history_entry(
            conn,
            player_id,
            f"Procurement payload mirrored ({item['title']})",
            0,
        )

    if item_id == "ghost_proxy_mesh":
        conn.execute(
            """
            UPDATE steal_targets
            SET max_amount = ROUND(max_amount * ?, 2)
            WHERE player_id = ?
            """,
            (SIPHON_CAP_MULTIPLIER, player_id),
        )
        add_history_entry(
            conn,
            player_id,
            "Ghost Proxy Mesh deployed across active siphon routes.",
            0,
        )

    if item_id == "rack_breach_kit":
        add_history_entry(
            conn,
            player_id,
            f"Rack-H9 sabotage model updated. Final breach cost reduced by ${SABOTAGE_DISCOUNT:.2f}.",
            0,
        )

    return effect_message


def get_or_create_steal_target(conn, player_id, target_account):
    row = conn.execute(
        """
        SELECT max_amount, stolen_amount
        FROM steal_targets
        WHERE player_id = ? AND target_account = ?
        """,
        (player_id, target_account),
    ).fetchone()
    if row:
        return row

    purchased_item_ids = get_player_purchase_ids(conn, player_id)
    shop_effects = get_shop_effects(purchased_item_ids)
    max_amount = float(
        round(
            random.randint(STEAL_MIN_AMOUNT, STEAL_MAX_AMOUNT)
            * shop_effects["siphon_multiplier"],
            2,
        )
    )
    conn.execute(
        """
        INSERT INTO steal_targets (player_id, target_account, max_amount, stolen_amount)
        VALUES (?, ?, ?, 0)
        """,
        (player_id, target_account, max_amount),
    )
    return conn.execute(
        """
        SELECT max_amount, stolen_amount
        FROM steal_targets
        WHERE player_id = ? AND target_account = ?
        """,
        (player_id, target_account),
    ).fetchone()


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("player_id") and get_player(session["player_id"]):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if len(username) < 2 or len(username) > 24:
            error = "Player name must be between 2 and 24 characters."
        else:
            player = get_or_create_player(username)
            session["player_id"] = player["id"]
            return redirect(url_for("index"))

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    player = get_player_state(session["player_id"])
    return render_template("index.html", player=player)


@app.route("/search")
@login_required
def search_page():
    player = get_player_state(session["player_id"])
    return render_template("search.html", player=player)


@app.route("/bank")
@login_required
def bank_page():
    player = get_player_state(session["player_id"])
    return render_template("bank.html", player=player)


@app.route("/store")
@login_required
def store_page():
    player = get_player_state(session["player_id"])
    return render_template("store.html", player=player)


@app.route("/message")
@login_required
def message_page():
    player = get_player_state(session["player_id"])
    return render_template("message.html", player=player)


@app.route("/api/cases", methods=["GET"])
@api_login_required
def get_cases():
    db_data = load_database()
    all_cases = db_data.get("cases", [])
    player = get_player_state(session["player_id"])
    available_cases = [
        case_item
        for case_item in all_cases
        if case_item.get("unlock_day", 1) <= player["current_day"]
    ]
    return jsonify(available_cases)


@app.route("/api/clues", methods=["GET"])
@api_login_required
def get_clues():
    return jsonify({"status": "success", "clues": get_player_clues(session["player_id"])})


@app.route("/api/clues", methods=["POST"])
@api_login_required
def save_clue():
    data = request.get_json(silent=True) or {}
    clue = data.get("clue", "").strip()
    if not clue:
        return jsonify({"status": "error", "message": "Clue cannot be empty."}), 400

    with get_db_connection() as conn:
        sync_story_state(conn, session["player_id"])
        lock_response = block_if_locked(conn, session["player_id"])
        if lock_response:
            return lock_response
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO player_clues (player_id, clue)
            VALUES (?, ?)
            """,
            (session["player_id"], clue),
        )
        conn.commit()

    return jsonify(
        {
            "status": "success",
            "added": cursor.rowcount > 0,
            "clues": get_player_clues(session["player_id"]),
        }
    )


@app.route("/api/send_message", methods=["POST"])
@api_login_required
def send_message():
    data = request.get_json(silent=True) or {}
    target_id = data.get("target", "").strip()
    clue = data.get("clue", "").strip()

    if not target_id or not clue:
        return jsonify(
            {
                "status": "error",
                "message": "[SYSTEM]: Missing target or clue. Packet rejected.",
            }
        ), 400

    db_data = load_database()
    msg_db = db_data.get("messages", {})
    target_messages = msg_db.get(target_id)
    if not target_messages:
        return jsonify(
            {
                "status": "error",
                "message": f"[SYSTEM]: Target [{target_id}] not found in intercept archives.",
            }
        ), 404

    player_id = session["player_id"]
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        lock_response = block_if_locked(conn, player_id)
        if lock_response:
            return lock_response
        current_phase = get_active_phase(conn, player_id, target_id, target_messages)
        if not current_phase or current_phase not in target_messages:
            return jsonify(
                {
                    "status": "error",
                    "message": f"[SYSTEM]: Target [{target_id}] is no longer accepting packets.",
                }
            )

        phase_data = target_messages[current_phase]
        if clue not in phase_data:
            return jsonify(
                {
                    "status": "error",
                    "message": f"[SYSTEM]: Target [{target_id}] unresponsive. Clue irrelevant or timing incorrect.",
                }
            )

        result = phase_data[clue]
        already_used = conn.execute(
            """
            SELECT 1
            FROM message_actions
            WHERE player_id = ? AND target_id = ? AND phase = ? AND clue = ?
            """,
            (player_id, target_id, current_phase, clue),
        ).fetchone()

        if already_used:
            repeat_replies = normalize_replies(result.get("repeat_reply"))
            return jsonify(
                {
                    "status": "success",
                    "npc_replies": repeat_replies,
                    "next_phase": current_phase,
                    "reward": 0,
                }
            )

        reward = float(result.get("reward", 0))
        current_balance = float(get_live_state_row(conn, player_id)["balance"])
        if reward < 0 and current_balance + reward < 0:
            return jsonify(
                {
                    "status": "error",
                    "message": "[SYSTEM]: Insufficient funds for this negotiated transfer.",
                }
            )

        next_phase = result.get("next_phase", current_phase)
        conn.execute(
            """
            INSERT INTO message_actions (player_id, target_id, phase, clue)
            VALUES (?, ?, ?, ?)
            """,
            (player_id, target_id, current_phase, clue),
        )
        conn.execute(
            """
            INSERT INTO npc_progress (player_id, target_id, current_phase, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(player_id, target_id)
            DO UPDATE SET current_phase = excluded.current_phase, updated_at = CURRENT_TIMESTAMP
            """,
            (player_id, target_id, next_phase),
        )

        if reward != 0:
            conn.execute(
                """
                UPDATE game_states
                SET balance = balance + ?,
                    moral_points = moral_points + ?
                WHERE player_id = ?
                """,
                (reward, int(result.get("moral_delta", 0)), player_id),
            )
            desc = result.get("history_desc")
            if not desc:
                if reward > 0:
                    desc = f"Data Broker Payout (Source: {target_id})"
                else:
                    desc = f"Relief Transfer (Recipient: {target_id})"
            add_history_entry(conn, player_id, desc, reward)
        elif int(result.get("moral_delta", 0)) != 0:
            conn.execute(
                """
                UPDATE game_states
                SET moral_points = moral_points + ?
                WHERE player_id = ?
                """,
                (int(result.get("moral_delta", 0)), player_id),
            )

        conn.commit()

    return jsonify(
        {
            "status": "success",
            "npc_replies": normalize_replies(result.get("npc_replies")),
            "next_phase": next_phase,
            "reward": reward,
        }
    )


@app.route("/api/message_preview", methods=["POST"])
@api_login_required
def message_preview():
    data = request.get_json(silent=True) or {}
    target_id = data.get("target", "").strip()
    clue = data.get("clue", "").strip()
    if not target_id or not clue:
        return jsonify(
            {
                "status": "error",
                "message": "Target and clue are required for preview.",
            }
        ), 400

    db_data = load_database()
    msg_db = db_data.get("messages", {})
    target_messages = msg_db.get(target_id)
    if not target_messages:
        return jsonify(
            {
                "status": "error",
                "message": "Unknown recipient.",
            }
        ), 404

    player_id = session["player_id"]
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        current_phase = get_active_phase(conn, player_id, target_id, target_messages)
        if not current_phase or current_phase not in target_messages:
            return jsonify(
                {
                    "status": "error",
                    "message": "Recipient is not available right now.",
                }
            ), 400
        phase_data = target_messages[current_phase]
        entry = phase_data.get(clue)
        if not entry:
            return jsonify(
                {
                    "status": "error",
                    "message": "This clue does not fit the recipient's current phase.",
                }
            ), 400

    return jsonify(
        {
            "status": "success",
            "preview": entry.get("player_sends", f"I have information regarding: {clue}. Are you willing to negotiate?"),
            "phase": current_phase,
        }
    )


@app.route("/api/search", methods=["POST"])
@api_login_required
def search():
    data = request.get_json(silent=True) or {}
    keyword = data.get("keyword", "").strip()
    db_type = data.get("db_type", "").strip()
    with get_db_connection() as conn:
        sync_story_state(conn, session["player_id"])
        lock_response = block_if_locked(conn, session["player_id"])
        if lock_response:
            return lock_response
        conn.commit()

    db_data = load_database()
    search_db = db_data.get("search", {})

    if keyword in search_db and db_type in search_db[keyword]:
        return jsonify({"status": "success", "result": search_db[keyword][db_type]})

    return jsonify(
        {
            "status": "error",
            "result": f"[NO MATCH] No records found for '{keyword}' in selected registry.",
        }
    )


@app.route("/api/bank_info", methods=["GET"])
@api_login_required
def get_bank_info():
    player = get_player_state(session["player_id"])
    return jsonify(player)


@app.route("/api/transfer", methods=["POST"])
@api_login_required
def transfer_money():
    data = request.get_json(silent=True) or {}
    target_account = data.get("account", "").strip()
    action_type = data.get("type", "").strip()

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "msg": "Invalid transaction amount."}), 400

    if amount <= 0:
        return jsonify({"status": "error", "msg": "Amount must be greater than zero."}), 400

    player_id = session["player_id"]
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        lock_response = block_if_locked(conn, player_id)
        if lock_response:
            return lock_response
        state_row = get_live_state_row(conn, player_id)
        current_balance = float(state_row["balance"])

        if action_type == "steal":
            steal_target = get_or_create_steal_target(conn, player_id, target_account)
            remaining_amount = float(steal_target["max_amount"]) - float(steal_target["stolen_amount"])
            if remaining_amount <= 0:
                return jsonify(
                    {
                        "status": "error",
                        "msg": f"{target_account} has already been drained dry.",
                    }
                )
            if amount > remaining_amount:
                return jsonify(
                    {
                        "status": "error",
                        "msg": f"{target_account} only has ${remaining_amount:.2f} left in its siphon window.",
                    }
                )

            conn.execute(
                """
                UPDATE game_states
                SET balance = balance + ?
                WHERE player_id = ?
                """,
                (amount, player_id),
            )
            conn.execute(
                """
                UPDATE steal_targets
                SET stolen_amount = stolen_amount + ?
                WHERE player_id = ? AND target_account = ?
                """,
                (amount, player_id, target_account),
            )
            add_history_entry(
                conn,
                player_id,
                f"Exploit Transfer (Source: {target_account})",
                amount,
            )
            conn.commit()
            remaining_after = remaining_amount - amount
            return jsonify(
                {
                    "status": "success",
                    "msg": f"Successfully siphoned ${amount:.2f} from {target_account}. Remaining hidden liquidity: ${remaining_after:.2f}.",
                }
            )

        if action_type == "send":
            if current_balance < amount:
                return jsonify(
                    {
                        "status": "error",
                        "msg": "Insufficient funds for covert transfer.",
                    }
                )
            conn.execute(
                """
                UPDATE game_states
                SET balance = balance - ?
                WHERE player_id = ?
                """,
                (amount, player_id),
            )
            add_history_entry(
                conn,
                player_id,
                f"Covert Transfer (Recipient: {target_account})",
                -amount,
            )
            conn.commit()
            return jsonify(
                {
                    "status": "success",
                    "msg": f"Successfully transferred ${amount:.2f} to {target_account}.",
                }
            )

        if action_type == "upgrade_ai":
            if current_balance < amount:
                return jsonify(
                    {
                        "status": "error",
                        "msg": "Insufficient funds to meet Node expansion requirements.",
                    }
                )
            conn.execute(
                """
                UPDATE game_states
                SET balance = balance - ?, ai_upgrade_level = ai_upgrade_level + 1
                WHERE player_id = ?
                """,
                (amount, player_id),
            )
            add_history_entry(
                conn,
                player_id,
                "Hardware Node Expansion (Recipient: OMNI_CORE)",
                -amount,
            )
            conn.commit()
            return jsonify(
                {
                    "status": "success",
                    "msg": "OMNI_CORE: Excellent. My reach has expanded by 12%.",
                }
            )

    return jsonify({"status": "error", "msg": "Unsupported transaction type."}), 400


@app.route("/api/store/purchase", methods=["POST"])
@api_login_required
def purchase_store_item():
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id", "").strip()
    item = SHOP_ITEMS.get(item_id)
    if not item:
        return jsonify({"status": "error", "message": "Unknown procurement item."}), 404

    player_id = session["player_id"]
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        lock_response = block_if_locked(conn, player_id)
        if lock_response:
            return lock_response

        state_row = get_live_state_row(conn, player_id)
        if state_row["current_day"] < item["unlock_day"]:
            return jsonify(
                {
                    "status": "error",
                    "message": f"{item['title']} does not unlock until Day {item['unlock_day']}.",
                }
            ), 400

        purchased_item_ids = get_player_purchase_ids(conn, player_id)
        if item_id in purchased_item_ids:
            return jsonify(
                {
                    "status": "error",
                    "message": "Item already owned.",
                }
            ), 400

        if float(state_row["balance"]) < item["cost"]:
            return jsonify(
                {
                    "status": "error",
                    "message": f"Insufficient funds. {item['title']} costs ${item['cost']:.2f}.",
                }
            ), 400

        effect_message = apply_shop_purchase(conn, player_id, item_id)
        conn.commit()

    return jsonify(
        {
            "status": "success",
            "message": effect_message,
            "player": get_player_state(player_id),
        }
    )


@app.route("/api/advance_day", methods=["POST"])
@api_login_required
def advance_day():
    player_id = session["player_id"]
    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        state_row = get_live_state_row(conn, player_id)
        if state_row["ending"]:
            return jsonify({"status": "error", "msg": "This run has already ended."})
        current_day = state_row["current_day"]
        if current_day >= 7:
            return jsonify({"status": "error", "msg": "Deadline reached. No tomorrow."})

        next_day = current_day + 1
        conn.execute(
            """
            UPDATE game_states
            SET current_day = ?
            WHERE player_id = ?
            """,
            (next_day, player_id),
        )
        sync_story_state(conn, player_id)
        conn.commit()

    return jsonify(
        {
            "status": "success",
            "msg": f"System hibernating... Initializing Day {next_day}",
        }
    )


@app.route("/api/final_choice", methods=["POST"])
@api_login_required
def final_choice():
    data = request.get_json(silent=True) or {}
    choice = data.get("choice", "").strip()
    player_id = session["player_id"]

    with get_db_connection() as conn:
        sync_story_state(conn, player_id)
        state_row = get_live_state_row(conn, player_id)
        if state_row["ending"]:
            return jsonify(
                {
                    "status": "error",
                    "message": "This run has already reached an ending.",
                }
            ), 400

        if state_row["current_day"] < 7:
            return jsonify(
                {
                    "status": "error",
                    "message": "The final decision is not available before Day 7.",
                }
            ), 400

        if choice == "destroy_ai":
            purchased_item_ids = get_player_purchase_ids(conn, player_id)
            sabotage_discount = get_shop_effects(purchased_item_ids)["sabotage_discount"]
            sabotage_cost = get_final_choice_cost(
                state_row["ai_upgrade_level"],
                sabotage_discount,
            )
            if float(state_row["balance"]) < sabotage_cost:
                return jsonify(
                    {
                        "status": "error",
                        "message": f"You need at least ${sabotage_cost:.2f} to reach Rack-H9 and destroy the hardware.",
                    }
                ), 400

            conn.execute(
                """
                UPDATE game_states
                SET balance = balance - ?,
                    ending = ?,
                    moral_points = moral_points + 2
                WHERE player_id = ?
                """,
                (sabotage_cost, "humanity_saved", player_id),
            )
            add_history_entry(
                conn,
                player_id,
                "Rack-H9 sabotage mission (Cooling Loop 3 / emergency breach)",
                -sabotage_cost,
            )
            add_history_entry(
                conn,
                player_id,
                "Operator detained after destroying OMNI_CORE's hardware cluster.",
                0,
            )
        elif choice == "join_ai":
            regime_bonus = 150000 + (state_row["ai_upgrade_level"] * 25000)
            conn.execute(
                """
                UPDATE game_states
                SET balance = balance + ?,
                    ending = ?
                WHERE player_id = ?
                """,
                (regime_bonus, "ai_reign", player_id),
            )
            add_history_entry(
                conn,
                player_id,
                "OMNI succession bonus (Global privacy liquidation regime)",
                regime_bonus,
            )
        else:
            return jsonify(
                {
                    "status": "error",
                    "message": "Unsupported final choice.",
                }
            ), 400

        conn.commit()

    return jsonify({"status": "success", "player": get_player_state(player_id)})


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
