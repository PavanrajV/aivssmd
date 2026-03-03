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

