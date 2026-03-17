"""
reset_session.py — Wipes today's trades and resets bankroll.
Run this to 'rerun' the simulation with new accuracy logic.
"""
import sqlite3
import os
from datetime import date

db_path = os.path.join(os.path.dirname(__file__), "data", "trades.db")

def reset():
    if not os.path.exists(db_path):
        print("Database not found.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    today = str(date.today())
    
    # 1. Delete today's trades
    cursor.execute("DELETE FROM trades WHERE date(timestamp) = ?", (today,))
    deleted_trades = cursor.rowcount
    
    # 2. Delete today's summary
    cursor.execute("DELETE FROM daily_summary WHERE date = ?", (today,))
    
    conn.commit()
    conn.close()
    
    print(f"✓ Reset complete. Deleted {deleted_trades} trades from {today}.")
    print("✓ Restart the bot now to begin the '100% accurate' session.")

if __name__ == "__main__":
    reset()
