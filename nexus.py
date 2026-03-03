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

# ══════════════════════════════════════════════
#  CAMERA CLASS  (threaded 30 FPS)
# ══════════════════════════════════════════════
class Camera:
    def __init__(self, camera_id=0):
        self.camera_id = camera_id
        self.name      = f"CAM-{camera_id+1:02d}"
        self.cap = self.frame = None
        self.lock    = threading.Lock()
        self.running = self.online = False
        self.motion_detected = False
        self.motion_level  = 0.0
        self.motion_count  = 0
        self.last_motion   = None
        self.motion_start  = None
        self.frame_count   = 0
        self._prev_gray    = None
        self._thread       = None

    def start(self):
        if self.running: return True
        self.cap = cv2.VideoCapture(self.camera_id)
        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self.running = self.online = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log_event('CAMERA', f'{self.name} started')
        return True

    def stop(self):
        self.running = self.online = False
        time.sleep(0.2)
        if self.cap: self.cap.release()
        log_event('CAMERA', f'{self.name} stopped')

    def _loop(self):
        interval = 1.0 / 30
        while self.running:
            t0 = time.time()
            ret, frame = (self.cap.read() if (self.cap and self.cap.isOpened()) else (False, None))
            if not ret: frame = self._dummy_frame()
            self.frame_count += 1
            frame = self._detect_motion(frame)
            frame = self._draw_hud(frame)
            with self.lock: self.frame = frame
            time.sleep(max(0, interval - (time.time() - t0)))

    def _detect_motion(self, frame):
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21,21), 0)
        level = 0.0
        if self._prev_gray is not None:
            diff   = cv2.absdiff(self._prev_gray, gray)
            thresh = cv2.dilate(cv2.threshold(diff,25,255,cv2.THRESH_BINARY)[1], None, iterations=2)
            cnts, _ = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            total_area = 0
            for c in cnts:
                a = cv2.contourArea(c)
                if a > 600:
                    x,y,w,h = cv2.boundingRect(c)
                    cv2.rectangle(frame, (x,y), (x+w,y+h), (0,255,136), 2)
                    cv2.rectangle(frame, (x,y), (x+w,y+12), (0,255,136), -1)
                    cv2.putText(frame,'MOTION',(x+3,y+10),cv2.FONT_HERSHEY_SIMPLEX,0.35,(0,0,0),1)
                    total_area += a
            level = min(total_area / (frame.shape[0]*frame.shape[1]) * 100, 100)
            if level > 1.8:
                if not self.motion_detected:
                    self.motion_detected = True
                    self.motion_count   += 1
                    self.last_motion     = datetime.now()
                    self.motion_start    = time.time()
                    threading.Thread(target=self._save_motion, args=(level,), daemon=True).start()
            else:
                if self.motion_detected and self.motion_start:
                    dur = int((time.time()-self.motion_start)*1000)
                    threading.Thread(target=self._update_dur, args=(dur,), daemon=True).start()
                self.motion_detected = False
        self._prev_gray  = gray
        self.motion_level = level
        return frame

    def _save_motion(self, level):
        try:
            conn = get_db()
            conn.execute("INSERT INTO motion_events (camera_id,camera_name,motion_level) VALUES (?,?,?)",
                         (self.camera_id, self.name, level))
            conn.commit(); conn.close()
        except: pass
        socketio.emit('motion_alert', {
            'camera_id': self.camera_id, 'camera_name': self.name,
            'motion_level': round(level,2), 'timestamp': datetime.now().strftime('%H:%M:%S')
        })

    def _update_dur(self, ms):
        try:
            conn = get_db()
            conn.execute("UPDATE motion_events SET duration_ms=? WHERE camera_id=? ORDER BY id DESC LIMIT 1",
                         (ms, self.camera_id))
            conn.commit(); conn.close()
        except: pass

    def _draw_hud(self, frame):
        h, w = frame.shape[:2]
        ov = frame.copy()
        cv2.rectangle(ov,(0,0),(w,52),(8,10,14),-1)
        cv2.addWeighted(ov,0.85,frame,0.15,0,frame)
        cv2.putText(frame,self.name,(14,34),cv2.FONT_HERSHEY_DUPLEX,0.75,(0,255,136),1)
        cv2.putText(frame,datetime.now().strftime('%Y-%m-%d  %H:%M:%S'),(w-290,34),
                    cv2.FONT_HERSHEY_SIMPLEX,0.58,(160,180,200),1)
        if self.motion_detected:
            cv2.putText(frame,'▲ MOTION',(w//2-65,34),cv2.FONT_HERSHEY_DUPLEX,0.7,(0,60,255),2)
            cv2.rectangle(frame,(0,0),(w-1,h-1),(0,40,220),3)
        else:
            cv2.circle(frame,(w//2-10,26),6,(0,200,100),-1)
            cv2.putText(frame,'LIVE',(w//2+2,34),cv2.FONT_HERSHEY_DUPLEX,0.7,(0,200,100),1)
        by = h-16
        cv2.rectangle(frame,(10,by),(w-10,by+8),(25,30,40),-1)
        bw = int((w-20)*(self.motion_level/100))
        if bw>0:
            col=(0,255,136) if self.motion_level<30 else (0,165,255) if self.motion_level<60 else (0,50,255)
            cv2.rectangle(frame,(10,by),(10+bw,by+8),col,-1)
        cv2.putText(frame,f'#{self.frame_count:06d}',(14,h-22),cv2.FONT_HERSHEY_SIMPLEX,0.4,(60,80,100),1)
        return frame

    def _dummy_frame(self):
        f = np.full((720,1280,3),(10,12,16),dtype=np.uint8)
        for i in range(0,720,60): cv2.line(f,(0,i),(1280,i),(18,22,28),1)
        for j in range(0,1280,60): cv2.line(f,(j,0),(j,720),(18,22,28),1)
        cv2.putText(f,'NO SIGNAL',(460,350),cv2.FONT_HERSHEY_DUPLEX,2.5,(40,50,60),3)
        cv2.putText(f,self.name,(570,410),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0,100,60),2)
        return f

    def get_jpeg(self):
        with self.lock:
            if self.frame is not None:
                ok,buf = cv2.imencode('.jpg',self.frame,[cv2.IMWRITE_JPEG_QUALITY,82])
                return buf.tobytes() if ok else None
        return None

    def stats(self):
        return {
            'camera_id':    self.camera_id,
            'name':         self.name,
            'online':       self.online,
            'running':      self.running,
            'motion':       self.motion_detected,
            'motion_level': round(self.motion_level,2),
            'motion_count': self.motion_count,
            'frame_count':  self.frame_count,
            'last_motion':  self.last_motion.strftime('%H:%M:%S') if self.last_motion else None,
        }

