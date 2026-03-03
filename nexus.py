"""
╔══════════════════════════════════════════════╗
║     NEXUS SURVEILLANCE SYSTEM  v1.0          ║
║     Single-file — paste & run                ║
║                                              ║
║  INSTALL:  pip install flask flask-socketio  ║
║            opencv-python-headless numpy      ║
║            werkzeug psutil eventlet          ║
║                                              ║
║  RUN:      python nexus.py                   ║
║  OPEN:     http://localhost:5000             ║
║  LOGIN:    admin / admin123                  ║
╚══════════════════════════════════════════════╝
"""

# ─── pip install flask flask-socketio opencv-python-headless numpy werkzeug psutil eventlet ───
import cv2, numpy as np, threading, time, sqlite3, os, json
from datetime import datetime
from flask import Flask, render_template_string, Response, request, jsonify, redirect, url_for, session
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'nexus_surveillance_xK9mP_2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
DB_PATH  = 'nexus_surveillance.db'

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'operator',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS motion_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER DEFAULT 0,
            camera_name TEXT DEFAULT 'CAM-01',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            motion_level REAL DEFAULT 0,
            duration_ms INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            event_type TEXT,
            severity TEXT DEFAULT 'INFO',
            message TEXT
        );
    """)
    for uname, pwd, role in [('admin','admin123','admin'),('operator','op1234','operator')]:
        try:
            conn.execute("INSERT INTO users (username,password,role) VALUES (?,?,?)",
                         (uname, generate_password_hash(pwd), role))
        except: pass
    conn.commit(); conn.close()

def log_event(etype, msg, sev='INFO'):
    try:
        conn = get_db()
        conn.execute("INSERT INTO system_logs (event_type,severity,message) VALUES (?,?,?)", (etype,sev,msg))
        conn.commit(); conn.close()
    except: pass

