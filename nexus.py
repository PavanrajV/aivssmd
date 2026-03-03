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

# ══════════════════════════════════════════════
#  REGISTRY
# ══════════════════════════════════════════════
cameras: dict = {}

def get_or_create(cid):
    if cid not in cameras:
        cameras[cid] = Camera(cid)
    return cameras[cid]

def login_required(f):
    from functools import wraps
    @wraps(f)
    def dec(*a, **kw):
        if 'user_id' not in session:
            return jsonify({'error':'Unauthorized'}),401 if request.is_json else redirect(url_for('login'))
        return f(*a,**kw)
    return dec

# ══════════════════════════════════════════════
#  HTML TEMPLATES (inline)
# ══════════════════════════════════════════════
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NEXUS — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=JetBrains+Mono:wght@300;400;500&family=Syne:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#060810;--surface:#0b0e15;--border:#16202e;--accent:#00ff88;--accent2:#00ccff;--red:#ff3355;--text:#c8d3e0;--muted:#4a5568;--glow:0 0 30px rgba(0,255,136,.18)}
body{background:var(--bg);font-family:'Syne',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;color:var(--text)}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,.03)1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,.03)1px,transparent 1px);background-size:60px 60px;pointer-events:none;z-index:0}
body::after{content:'';position:fixed;inset:0;background:repeating-linear-gradient(transparent,transparent 2px,rgba(0,0,0,.08)2px,rgba(0,0,0,.08)4px);pointer-events:none;z-index:0}
.corner{position:fixed;width:80px;height:80px;border-color:var(--accent);border-style:solid;opacity:.35}
.corner.tl{top:20px;left:20px;border-width:2px 0 0 2px}.corner.tr{top:20px;right:20px;border-width:2px 2px 0 0}
.corner.bl{bottom:20px;left:20px;border-width:0 0 2px 2px}.corner.br{bottom:20px;right:20px;border-width:0 2px 2px 0}
.card{position:relative;z-index:10;width:440px;background:var(--surface);border:1px solid var(--border);padding:48px;animation:slideUp .6s cubic-bezier(.22,1,.36,1) both}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--accent),transparent)}
.card::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--accent2),transparent);opacity:.5}
@keyframes slideUp{from{opacity:0;transform:translateY(30px)}to{opacity:1;transform:translateY(0)}}
.logo-wrap{display:flex;align-items:center;gap:14px;margin-bottom:36px}
.logo-icon{width:48px;height:48px;border:2px solid var(--accent);display:grid;place-items:center;position:relative}
.logo-icon::after{content:'';position:absolute;inset:4px;border:1px solid rgba(0,255,136,.3)}
.logo-icon svg{width:22px;height:22px}
.brand{font-family:'Rajdhani',sans-serif;font-size:28px;font-weight:700;letter-spacing:4px;color:#fff}
.sub{font-family:'Rajdhani',sans-serif;font-size:11px;letter-spacing:3px;color:var(--accent);font-weight:500}
.heading{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-bottom:28px;display:flex;align-items:center;gap:10px}
.heading::after{content:'';flex:1;height:1px;background:var(--border)}
.field{margin-bottom:20px}
label{display:block;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:2px;color:var(--accent);text-transform:uppercase;margin-bottom:8px}
.input-wrap{position:relative}
.input-icon{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:14px;pointer-events:none}
input[type=text],input[type=password]{width:100%;background:rgba(0,0,0,.4);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:14px;padding:13px 14px 13px 40px;outline:none;transition:border-color .2s,box-shadow .2s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),var(--glow)}
.btn-login{width:100%;background:transparent;border:1px solid var(--accent);color:var(--accent);font-family:'Rajdhani',sans-serif;font-size:15px;font-weight:700;letter-spacing:4px;text-transform:uppercase;padding:14px;cursor:pointer;position:relative;overflow:hidden;transition:all .25s;margin-top:8px}
.btn-login::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(0,255,136,.15),transparent);transition:left .4s}
.btn-login:hover::before{left:100%}
.btn-login:hover{background:rgba(0,255,136,.08);box-shadow:var(--glow)}
.error{background:rgba(255,51,85,.1);border:1px solid rgba(255,51,85,.4);color:var(--red);font-family:'JetBrains Mono',monospace;font-size:12px;padding:10px 14px;margin-bottom:20px;display:flex;align-items:center;gap:8px;animation:shake .3s ease}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
.card-footer{margin-top:28px;padding-top:20px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.status-pill{display:flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.version{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px}
.demo-hint{margin-top:14px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);text-align:center;letter-spacing:1px}
.demo-hint span{color:var(--accent)}
</style>
</head>
<body>
<div class="corner tl"></div><div class="corner tr"></div>
<div class="corner bl"></div><div class="corner br"></div>
<div class="card">
  <div class="logo-wrap">
    <div class="logo-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="#00ff88" stroke-width="1.5">
        <circle cx="12" cy="12" r="4"/>
        <path d="M12 2v2M12 20v2M2 12h2M20 12h2"/>
        <path d="M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
      </svg>
    </div>
    <div><div class="brand">NEXUS</div><div class="sub">Surveillance System</div></div>
  </div>
  <div class="heading">Operator Authentication</div>
  {% if error %}<div class="error">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>{{ error }}</div>{% endif %}
  <form method="POST" autocomplete="off">
    <div class="field">
      <label>User ID</label>
      <div class="input-wrap">
        <span class="input-icon">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>
          </svg>
        </span>
        <input type="text" name="username" placeholder="Enter username" required autofocus>
      </div>
    </div>
    <div class="field">
      <label>Access Code</label>
      <div class="input-wrap">
        <span class="input-icon">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
          </svg>
        </span>
        <input type="password" name="password" placeholder="••••••••" required>
      </div>
    </div>
    <button type="submit" class="btn-login">▶ &nbsp;Authenticate &amp; Enter</button>
  </form>
  <div class="card-footer">
    <div class="status-pill"><div class="status-dot"></div>SYSTEM ONLINE</div>
    <div class="version">NEXUS v1.0</div>
  </div>
  <div class="demo-hint">Default: <span>admin</span> / <span>admin123</span></div>
</div>
</body>
</html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>NEXUS — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=JetBrains+Mono:wght@300;400;500&family=Syne:wght@400;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#060810;--surface:#0b0e15;--s2:#0f1520;--border:#16202e;--b2:#1e2d42;
  --accent:#00ff88;--cyan:#00ccff;--red:#ff3355;--amber:#ffaa00;
  --text:#c8d3e0;--muted:#4a5568;--dim:#2a3548;
  --glow-g:0 0 20px rgba(0,255,136,.2);--glow-r:0 0 20px rgba(255,51,85,.25);
  --sb:220px;
}
html,body{height:100%;font-family:'Syne',sans-serif;background:var(--bg);color:var(--text);overflow:hidden}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,.025)1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,.025)1px,transparent 1px);background-size:50px 50px;pointer-events:none;z-index:0}
.shell{display:grid;grid-template-columns:var(--sb) 1fr;grid-template-rows:56px 1fr;height:100vh;position:relative;z-index:1}

/* HEADER */
.header{grid-column:1/-1;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:14px;position:relative}
.header::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--accent),var(--cyan),transparent);opacity:.4}
.logo{display:flex;align-items:center;gap:10px;flex-shrink:0}
.logo-mark{width:32px;height:32px;border:1.5px solid var(--accent);display:grid;place-items:center;position:relative}
.logo-mark::after{content:'';position:absolute;inset:3px;border:1px solid rgba(0,255,136,.3)}
.logo-mark svg{width:14px;height:14px}
.brand{font-family:'Rajdhani',sans-serif;font-size:20px;font-weight:700;letter-spacing:4px;color:#fff}
.logo-sub{font-family:'Rajdhani',sans-serif;font-size:9px;letter-spacing:2px;color:var(--accent);margin-top:1px}
.hsp{flex:1}
.htime{font-family:'JetBrains Mono',monospace;font-size:18px;color:var(--accent);letter-spacing:2px}
.hdate{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px;margin-left:12px}
.pill{display:flex;align-items:center;gap:6px;background:var(--s2);border:1px solid var(--border);padding:5px 10px;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:1px;color:var(--muted);margin-left:16px}
.pill .dot{width:6px;height:6px;border-radius:50%}
.pill.online .dot{background:var(--accent);animation:pulse 2s infinite}
.pill.motion-pill .dot{background:var(--red);animation:blink .6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.btn-logout{background:transparent;border:1px solid var(--b2);color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:1px;padding:7px 14px;cursor:pointer;text-decoration:none;transition:all .2s;margin-left:10px}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* SIDEBAR */
.sidebar{background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden}
.sb-section{padding:16px 16px 6px;font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--dim);text-transform:uppercase}
.nav-item{display:flex;align-items:center;gap:10px;padding:11px 16px;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;transition:all .2s;border-left:2px solid transparent;user-select:none}
.nav-item svg{width:16px;height:16px;flex-shrink:0}
.nav-item:hover{color:var(--text);background:rgba(0,255,136,.04)}
.nav-item.active{color:var(--accent);border-left-color:var(--accent);background:rgba(0,255,136,.06)}
.nav-badge{margin-left:auto;background:var(--red);color:#fff;font-family:'JetBrains Mono',monospace;font-size:9px;padding:1px 5px;border-radius:2px;min-width:18px;text-align:center}
.cam-list{padding:8px}
.cam-card{background:var(--s2);border:1px solid var(--border);padding:10px;margin-bottom:6px;cursor:pointer;transition:border-color .2s}
.cam-card.motion-cam{border-color:var(--red)}
.cam-header{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.cam-led{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.cam-led.on{background:var(--accent);animation:pulse 2s infinite}
.cam-led.mot{background:var(--red);animation:blink .5s infinite}
.cam-name{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;color:var(--text);flex:1}
.cam-stxt{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--muted)}
.cam-bar{height:3px;background:var(--border);border-radius:1px;overflow:hidden}
.cam-bar-fill{height:100%;background:var(--accent);transition:width .3s;border-radius:1px}
.btn-cc{display:flex;align-items:center;justify-content:center;gap:4px;width:100%;margin-top:6px;background:transparent;border:1px solid var(--b2);color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:1px;padding:5px;cursor:pointer;transition:all .2s}
.btn-cc.start:hover{border-color:var(--accent);color:var(--accent)}
.btn-cc.stop:hover{border-color:var(--red);color:var(--red)}
.btn-add-cam{display:flex;align-items:center;justify-content:center;gap:6px;margin:6px 8px;width:calc(100% - 16px);padding:8px;background:transparent;border:1px dashed var(--dim);color:var(--muted);font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:1px;cursor:pointer;transition:all .2s}
.btn-add-cam:hover{border-color:var(--accent);color:var(--accent)}
.sb-footer{margin-top:auto;padding:14px 12px;border-top:1px solid var(--border)}
.user-row{display:flex;align-items:center;gap:8px}
.user-avatar{width:30px;height:30px;border:1px solid var(--accent);display:grid;place-items:center;font-family:'Rajdhani',sans-serif;font-size:13px;font-weight:700;color:var(--accent);flex-shrink:0}
.user-name{font-size:12px;font-weight:600;color:var(--text)}
.user-role{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:1px}

/* MAIN */
.main{overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column}
.panel{display:none;flex-direction:column;height:100%}
.panel.active{display:flex}

/* MONITOR */
.mon-panel{padding:14px;gap:12px}
.cam-grid{display:grid;gap:10px;flex:1}
.g1{grid-template-columns:1fr}
.g2{grid-template-columns:1fr 1fr}
.g4{grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr}
.vcont{position:relative;background:#000;border:1px solid var(--b2);overflow:hidden;min-height:0}
.vcont.motion-active{border-color:var(--red);box-shadow:var(--glow-r)}
.vcont img{width:100%;height:100%;object-fit:cover;display:block}
.vph{position:absolute;inset:0;display:none;place-items:center;background:var(--s2)}
.no-sig{font-family:'Rajdhani',sans-serif;font-size:20px;font-weight:700;letter-spacing:4px;color:var(--dim)}
.vov{position:absolute;top:0;left:0;right:0;padding:8px 10px;background:linear-gradient(#000c,transparent);display:flex;align-items:center;gap:8px}
.vtag{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;color:var(--accent);letter-spacing:1px}
.vrec{display:flex;align-items:center;gap:5px;font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--red);letter-spacing:1px}
.vrec .rdot{width:7px;height:7px;border-radius:50%;background:var(--red);animation:blink .7s infinite}
.vbot{position:absolute;bottom:0;left:0;right:0;padding:6px 10px;background:linear-gradient(transparent,#000b);display:flex;align-items:center}
.vmot{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--muted);margin-left:auto}
.mon-controls{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0}
.cbtn{display:flex;align-items:center;gap:7px;background:transparent;border:1px solid var(--b2);color:var(--text);font-family:'Rajdhani',sans-serif;font-size:12px;font-weight:600;letter-spacing:2px;padding:9px 16px;cursor:pointer;transition:all .2s;text-transform:uppercase}
.cbtn.p:hover{border-color:var(--accent);color:var(--accent);box-shadow:var(--glow-g)}
.cbtn.d:hover{border-color:var(--red);color:var(--red);box-shadow:var(--glow-r)}
.cbtn svg{width:13px;height:13px}
.gtog{display:flex;gap:6px;margin-left:auto}
.gbtn{width:32px;height:32px;border:1px solid var(--border);background:transparent;color:var(--muted);display:grid;place-items:center;cursor:pointer;transition:all .2s}
.gbtn.active,.gbtn:hover{border-color:var(--accent);color:var(--accent)}
.ticker{display:flex;align-items:center;gap:10px;background:var(--surface);border-top:1px solid var(--border);padding:7px 16px;flex-shrink:0}
.ticker-label{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;color:var(--amber);text-transform:uppercase;flex-shrink:0}
.ticker-content{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.ticker-time{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim);flex-shrink:0}

/* ANALYTICS */
.ana-panel{padding:14px;gap:12px;overflow-y:auto}
.stat-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.scard{background:var(--surface);border:1px solid var(--border);padding:16px;position:relative;overflow:hidden}
.scard::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.scard.g::before{background:var(--accent)}.scard.c::before{background:var(--cyan)}
.scard.r::before{background:var(--red)}.scard.a::before{background:var(--amber)}
.slabel{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.sval{font-family:'Rajdhani',sans-serif;font-size:36px;font-weight:700;line-height:1;color:#fff}
.ssub{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);margin-top:4px}
.sico{position:absolute;right:14px;top:14px;opacity:.12}
.sico svg{width:40px;height:40px}
.chart-row{display:grid;grid-template-columns:2fr 1fr;gap:10px}
.ccrd{background:var(--surface);border:1px solid var(--border);padding:16px;display:flex;flex-direction:column}
.ch{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.ctitle{font-family:'Rajdhani',sans-serif;font-size:13px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--text)}
.cbadge{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:1px;color:var(--muted);background:var(--s2);border:1px solid var(--border);padding:2px 6px}
.clive{margin-left:auto;display:flex;align-items:center;gap:5px;font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--accent);letter-spacing:1px}
.clive .dot{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:pulse 1.5s infinite}
canvas{display:block;width:100%!important}
.gr-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.gcrd{background:var(--surface);border:1px solid var(--border);padding:14px}
.glabel{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.gval{font-family:'Rajdhani',sans-serif;font-size:28px;font-weight:700;color:#fff;line-height:1}
.gpct{font-size:14px;color:var(--muted)}
.gbar{height:4px;margin-top:8px;background:var(--border);border-radius:2px;overflow:hidden}
.gbar-fill{height:100%;border-radius:2px;transition:width .6s}

/* ALERTS */
.alt-panel{padding:14px;gap:12px;overflow-y:auto}
.alt-header{display:flex;align-items:center;gap:10px}
.sec-title{font-family:'Rajdhani',sans-serif;font-size:14px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--text)}
.sec-title span{color:var(--accent)}
.alt-count{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--red);background:rgba(255,51,85,.12);border:1px solid rgba(255,51,85,.3);padding:2px 8px}
.tbl-wrap{background:var(--surface);border:1px solid var(--border);overflow-x:auto;flex:1}
table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:11px}
thead{position:sticky;top:0;z-index:1}
thead th{background:var(--s2);padding:10px 14px;text-align:left;font-size:9px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;border-bottom:1px solid var(--border);font-weight:500}
tbody tr{border-bottom:1px solid rgba(22,32,46,.7);transition:background .15s}
tbody tr:hover{background:rgba(0,255,136,.03)}
tbody td{padding:9px 14px;color:var(--text)}
.ts{color:var(--muted)}
.lbc{display:flex;align-items:center;gap:8px}
.mbar{flex:1;height:4px;background:var(--border);border-radius:1px;overflow:hidden;max-width:80px}
.mbar-fill{height:100%;border-radius:1px}
.bcam{background:rgba(0,204,255,.12);border:1px solid rgba(0,204,255,.3);color:var(--cyan);padding:1px 6px;font-size:9px;letter-spacing:1px}

/* LOGS */
.log-panel{padding:14px;gap:12px;overflow-y:auto}
.log-list{background:var(--surface);border:1px solid var(--border);font-family:'JetBrains Mono',monospace;font-size:11px;flex:1;overflow-y:auto}
.log-entry{display:grid;grid-template-columns:140px 70px 80px 1fr;gap:10px;padding:7px 14px;border-bottom:1px solid rgba(22,32,46,.5);align-items:center}
.log-entry:hover{background:rgba(0,255,136,.02)}
.log-ts{color:var(--muted);font-size:10px}.log-type{color:var(--cyan);font-size:9px;letter-spacing:1px}
.log-sev{font-size:9px;letter-spacing:1px}.log-sev.INFO{color:var(--muted)}.log-sev.WARNING{color:var(--amber)}.log-sev.ERROR{color:var(--red)}
.log-msg{color:var(--text);font-size:11px}

/* TOAST */
.toast-wrap{position:fixed;top:64px;right:16px;z-index:1000;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface);border:1px solid var(--red);padding:12px 16px;min-width:280px;display:flex;align-items:center;gap:12px;box-shadow:var(--glow-r);animation:tIn .3s cubic-bezier(.22,1,.36,1) both;pointer-events:all;cursor:pointer}
.toast.out{animation:tOut .3s ease forwards}
@keyframes tIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}
@keyframes tOut{to{opacity:0;transform:translateX(30px);height:0;padding:0;margin:0}}
.toast-ico{color:var(--red);flex-shrink:0}
.toast-title{font-family:'Rajdhani',sans-serif;font-size:13px;font-weight:700;letter-spacing:1px;color:var(--red)}
.toast-msg{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--muted);margin-top:2px}

::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--dim);border-radius:2px}
@media(max-width:900px){.stat-row{grid-template-columns:repeat(2,1fr)}.chart-row{grid-template-columns:1fr}}
@media(max-width:640px){:root{--sb:0px}.sidebar{display:none}.shell{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="toast-wrap" id="toastWrap"></div>
<div class="shell">
  <!-- HEADER -->
  <header class="header">
    <div class="logo">
      <div class="logo-mark"><svg viewBox="0 0 24 24" fill="none" stroke="#00ff88" stroke-width="1.5"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2"/></svg></div>
      <div><div class="brand">NEXUS</div><div class="logo-sub">Surveillance</div></div>
    </div>
    <div class="hsp"></div>
    <div class="htime" id="clk">00:00:00</div>
    <div class="hdate" id="clkd"></div>
    <div class="pill online"><div class="dot"></div><span id="sysStat">SYSTEM ONLINE</span></div>
    <div class="pill motion-pill" id="motPill" style="display:none"><div class="dot"></div>MOTION</div>
    <a href="/logout" class="btn-logout">⏻ LOGOUT</a>
  </header>

  <!-- SIDEBAR -->
  <aside class="sidebar">
    <div class="sb-section">Navigation</div>
    <div class="nav-item active" onclick="sw('monitor',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="1"/><path d="M8 21h8M12 17v4"/></svg>Live Monitor
    </div>
    <div class="nav-item" onclick="sw('analytics',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3v18h18"/><path d="M7 16l4-4 4 4 4-8"/></svg>Analytics
    </div>
    <div class="nav-item" onclick="sw('alerts',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>Alert History
      <span class="nav-badge" id="altBadge">0</span>
    </div>
    <div class="nav-item" onclick="sw('logs',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93A10 10 0 1 1 4.93 19.07"/></svg>System Logs
    </div>
    <div class="sb-section" style="margin-top:4px">Cameras</div>
    <div class="cam-list" id="camList"></div>
    <button class="btn-add-cam" onclick="addCam()">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>ADD CAMERA
    </button>
    <div class="sb-footer">
      <div class="user-row">
        <div class="user-avatar">{{ username[0]|upper }}</div>
        <div><div class="user-name">{{ username }}</div><div class="user-role">{{ role|upper }}</div></div>
      </div>
    </div>
  </aside>

  <!-- MAIN -->
  <main class="main">

    <!-- MONITOR -->
    <div class="panel active mon-panel" id="p-monitor">
      <div class="cam-grid g1" id="camGrid"></div>
      <div class="ticker">
        <span class="ticker-label">◆ FEED</span>
        <span class="ticker-content" id="tickMsg">Surveillance system active — All cameras operational</span>
        <span class="ticker-time" id="tickTime"></span>
      </div>
      <div class="mon-controls">
        <button class="cbtn p" onclick="startAll()">
          <svg viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21"/></svg>START ALL
        </button>
        <button class="cbtn d" onclick="stopAll()">
          <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>STOP ALL
        </button>
        <button class="cbtn" onclick="sw('analytics',document.querySelectorAll('.nav-item')[1])">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18M7 16l4-4 4 4 4-8"/></svg>ANALYTICS
        </button>
        <div class="gtog">
          <button class="gbtn active" id="gb1" onclick="setGrid(1,this)" title="1-up">
            <svg width="13" height="13" viewBox="0 0 12 12"><rect x="1" y="1" width="10" height="10" fill="currentColor" rx="1"/></svg>
          </button>
          <button class="gbtn" id="gb2" onclick="setGrid(2,this)" title="2-up">
            <svg width="13" height="13" viewBox="0 0 12 12">
              <rect x="1" y="1" width="4.5" height="10" fill="currentColor" rx="1"/>
              <rect x="6.5" y="1" width="4.5" height="10" fill="currentColor" rx="1"/>
            </svg>
          </button>
          <button class="gbtn" id="gb4" onclick="setGrid(4,this)" title="4-up">
            <svg width="13" height="13" viewBox="0 0 12 12">
              <rect x="1" y="1" width="4.5" height="4.5" fill="currentColor" rx="1"/>
              <rect x="6.5" y="1" width="4.5" height="4.5" fill="currentColor" rx="1"/>
              <rect x="1" y="6.5" width="4.5" height="4.5" fill="currentColor" rx="1"/>
              <rect x="6.5" y="6.5" width="4.5" height="4.5" fill="currentColor" rx="1"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- ANALYTICS -->
    <div class="panel ana-panel" id="p-analytics">
      <div class="stat-row">
        <div class="scard g">
          <div class="slabel">Cameras Online</div><div class="sval" id="sc-cams">0</div>
          <div class="ssub">of <span id="sc-ctot">0</span> configured</div>
          <div class="sico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><rect x="2" y="3" width="20" height="14" rx="1"/><path d="M8 21h8M12 17v4"/></svg></div>
        </div>
        <div class="scard c">
          <div class="slabel">Today's Alerts</div><div class="sval" id="sc-today">0</div>
          <div class="ssub">motion events</div>
          <div class="sico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/></svg></div>
        </div>
        <div class="scard r">
          <div class="slabel">Total Events</div><div class="sval" id="sc-total">0</div>
          <div class="ssub">all time</div>
          <div class="sico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><path d="M3 3v18h18M7 16l4-4 4 4 4-8"/></svg></div>
        </div>
        <div class="scard a">
          <div class="slabel">CPU Load</div><div class="sval" id="sc-cpu">0<span style="font-size:18px;color:var(--muted)">%</span></div>
          <div class="ssub">utilization</div>
          <div class="sico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg></div>
        </div>
      </div>
      <div class="chart-row">
        <div class="ccrd" style="min-height:220px">
          <div class="ch"><div class="ctitle">Motion Activity</div><div class="cbadge">LAST HOUR</div><div class="clive"><div class="dot"></div>LIVE</div></div>
          <div style="height:160px;position:relative"><canvas id="chMotion"></canvas></div>
        </div>
        <div class="ccrd" style="min-height:220px">
          <div class="ch"><div class="ctitle">Today</div><div class="cbadge">HOURLY</div></div>
          <div style="height:160px;position:relative"><canvas id="chToday"></canvas></div>
        </div>
      </div>
      <div class="gr-row">
        <div class="gcrd"><div class="glabel">CPU Usage</div><div class="gval"><span id="g-cpu">0</span><span class="gpct">%</span></div><div class="gbar"><div class="gbar-fill" id="gb-cpu" style="width:0%;background:var(--accent)"></div></div></div>
        <div class="gcrd"><div class="glabel">Memory</div><div class="gval"><span id="g-mem">0</span><span class="gpct">%</span></div><div class="gbar"><div class="gbar-fill" id="gb-mem" style="width:0%;background:var(--amber)"></div></div></div>
        <div class="gcrd"><div class="glabel">Disk Space</div><div class="gval"><span id="g-dsk">0</span><span class="gpct">%</span></div><div class="gbar"><div class="gbar-fill" id="gb-dsk" style="width:0%;background:var(--cyan)"></div></div></div>
      </div>
    </div>

    <!-- ALERTS -->
    <div class="panel alt-panel" id="p-alerts">
      <div class="alt-header">
        <div class="sec-title"><span>◆</span> Alert History</div>
        <div class="alt-count" id="altCount">0 events</div>
        <button class="cbtn" style="margin-left:auto;font-size:11px;padding:6px 14px" onclick="loadAlerts()">↺ REFRESH</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>#</th><th>Camera</th><th>Timestamp</th><th>Motion Level</th><th>Duration</th><th>Status</th></tr></thead>
          <tbody id="altTbody"></tbody>
        </table>
      </div>
    </div>

    <!-- LOGS -->
    <div class="panel log-panel" id="p-logs">
      <div class="alt-header">
        <div class="sec-title"><span>◆</span> System Logs</div>
        <button class="cbtn" style="margin-left:auto;font-size:11px;padding:6px 14px" onclick="loadLogs()">↺ REFRESH</button>
      </div>
      <div class="log-list" id="logList"></div>
    </div>

  </main>
</div>

<script>
const socket = io();
let cams = {}, chM = null, chT = null, curPanel = 'monitor', alertCount = 0;

/* CLOCK */
function tick(){
  const n=new Date(), p=x=>String(x).padStart(2,'0');
  document.getElementById('clk').textContent=`${p(n.getHours())}:${p(n.getMinutes())}:${p(n.getSeconds())}`;
  document.getElementById('clkd').textContent=n.toLocaleDateString('en-US',{weekday:'short',year:'numeric',month:'short',day:'numeric'}).toUpperCase();
}
setInterval(tick,1000); tick();

/* PANEL */
function sw(id,el){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('p-'+id).classList.add('active');
  if(el) el.classList.add('active');
  curPanel=id;
  if(id==='analytics') loadAnalytics();
  if(id==='alerts')    loadAlerts();
  if(id==='logs')      loadLogs();
}

/* CAMERA CONTROL */
function startAll(){ Object.keys(cams).forEach(id=>startCam(+id)); if(!Object.keys(cams).length) startCam(0); }
function stopAll(){ Object.keys(cams).forEach(id=>stopCam(+id)); }
function startCam(id){ fetch('/api/camera/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_id:id})}).then(r=>r.json()).then(()=>refreshStats()); }
function stopCam(id){ fetch('/api/camera/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({camera_id:id})}).then(()=>refreshStats()); }
function addCam(){ fetch('/api/camera/add',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(d=>{if(d.success){refreshStats();renderGrid();}}); }

function refreshStats(){
  fetch('/api/camera/stats').then(r=>r.json()).then(d=>{
    cams=d; renderSidebar(); syncGrid(d);
    const anyMot=Object.values(d).some(c=>c.motion);
    document.getElementById('motPill').style.display=anyMot?'flex':'none';
  });
}

/* SIDEBAR */
function renderSidebar(){
  const L=document.getElementById('camList'); L.innerHTML='';
  Object.values(cams).forEach(cam=>{
    const div=document.createElement('div');
    div.className='cam-card'+(cam.motion?' motion-cam':'');
    const pct=cam.motion_level;
    const col=pct>60?'var(--red)':pct>30?'var(--amber)':'var(--accent)';
    div.innerHTML=`
      <div class="cam-header">
        <div class="cam-led ${cam.online?(cam.motion?'mot':'on'):''}"></div>
        <div class="cam-name">${cam.name}</div>
        <div class="cam-stxt">${cam.online?(cam.motion?'▲ MOTION':'● LIVE'):'○ OFF'}</div>
      </div>
      <div class="cam-bar"><div class="cam-bar-fill" style="width:${pct}%;background:${col}"></div></div>
      <button class="btn-cc ${cam.running?'stop':'start'}" onclick="event.stopPropagation();${cam.running?'stopCam':'startCam'}(${cam.camera_id})">
        ${cam.running?'■ STOP':'▶ START'}
      </button>`;
    L.appendChild(div);
  });
}

/* VIDEO GRID */
function renderGrid(){
  const G=document.getElementById('camGrid'); G.innerHTML='';
  const ids=Object.keys(cams); if(!ids.length) ids.push('0');
  ids.forEach(id=>{
    const cam=cams[id]||{name:'CAM-'+(+id+1),online:false,motion:false,motion_level:0};
    const w=document.createElement('div');
    w.className='vcont'; w.id='vc-'+id;
    w.innerHTML=`
      <img src="/video_feed/${id}" onerror="this.style.display='none';document.getElementById('vph-${id}').style.display='grid'">
      <div class="vph" id="vph-${id}"><div class="no-sig">NO SIGNAL</div></div>
      <div class="vov">
        <div class="vtag">${cam.name}</div>
        <div class="vrec"><div class="rdot"></div>REC</div>
      </div>
      <div class="vbot"><div class="vmot" id="vml-${id}">MTN: 0.00%</div></div>`;
    G.appendChild(w);
  });
}

function syncGrid(data){
  Object.values(data).forEach(cam=>{
    const id=cam.camera_id;
    const vc=document.getElementById('vc-'+id);
    if(vc) vc.classList.toggle('motion-active',cam.motion);
    const ml=document.getElementById('vml-'+id);
    if(ml) ml.textContent=`MTN: ${cam.motion_level.toFixed(2)}%`;
  });
}

function setGrid(n,btn){
  document.querySelectorAll('.gbtn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const G=document.getElementById('camGrid');
  G.className=`cam-grid g${n}`;
}

/* CHARTS */
const CD={
  animation:false,
  plugins:{legend:{display:false},tooltip:{backgroundColor:'#0b0e15',borderColor:'#16202e',borderWidth:1,titleColor:'#00ff88',bodyColor:'#c8d3e0',titleFont:{family:'JetBrains Mono'},bodyFont:{family:'JetBrains Mono'}}},
  scales:{
    x:{grid:{color:'rgba(30,45,66,.6)'},ticks:{color:'#4a5568',font:{family:'JetBrains Mono',size:9}}},
    y:{grid:{color:'rgba(30,45,66,.6)'},ticks:{color:'#4a5568',font:{family:'JetBrains Mono',size:9}}}
  },
  responsive:true,maintainAspectRatio:false
};

function initCharts(){
  chM=new Chart(document.getElementById('chMotion').getContext('2d'),{type:'line',data:{labels:[],datasets:[{data:[],borderColor:'#00ff88',backgroundColor:'rgba(0,255,136,.08)',borderWidth:2,fill:true,tension:0.4,pointRadius:3,pointBackgroundColor:'#00ff88'}]},options:CD});
  chT=new Chart(document.getElementById('chToday').getContext('2d'),{type:'bar',data:{labels:[],datasets:[{data:[],backgroundColor:'rgba(0,204,255,.25)',borderColor:'#00ccff',borderWidth:1,borderRadius:2}]},options:{...CD,plugins:{...CD.plugins,tooltip:{...CD.plugins.tooltip,titleColor:'#00ccff'}}}});
}

function loadAnalytics(){
  if(!chM) initCharts();
  fetch('/api/system/stats').then(r=>r.json()).then(d=>{
    document.getElementById('sc-cams').textContent=d.cameras_online;
    document.getElementById('sc-ctot').textContent=d.cameras_total;
    document.getElementById('sc-today').textContent=d.today_alerts;
    document.getElementById('sc-total').textContent=d.total_alerts;
    document.getElementById('sc-cpu').textContent=Math.round(d.cpu);
    ['cpu','mem','dsk'].forEach((k,i)=>{
      const v=[d.cpu,d.memory,d.disk][i];
      document.getElementById('g-'+k).textContent=Math.round(v);
      document.getElementById('gb-'+k).style.width=v+'%';
    });
  });
  fetch('/api/motion/history').then(r=>r.json()).then(data=>{
    chM.data.labels=data.map(d=>d.time);
    chM.data.datasets[0].data=data.map(d=>d.count);
    chM.update();
  });
  fetch('/api/motion/today').then(r=>r.json()).then(data=>{
    const hs=Object.keys(data).sort();
    chT.data.labels=hs.map(h=>h+':00');
    chT.data.datasets[0].data=hs.map(h=>data[h]);
    chT.update();
  });
}

/* ALERTS */
function loadAlerts(){
  fetch('/api/alerts/recent').then(r=>r.json()).then(rows=>{
    document.getElementById('altCount').textContent=rows.length+' events';
    document.getElementById('altBadge').textContent=rows.length;
    const tb=document.getElementById('altTbody'); tb.innerHTML='';
    rows.forEach(row=>{
      const pct=Math.min(row.motion_level,100);
      const col=pct>60?'var(--red)':pct>30?'var(--amber)':'var(--accent)';
      const dur=row.duration_ms>0?(row.duration_ms/1000).toFixed(1)+'s':'—';
      const tr=document.createElement('tr');
      tr.innerHTML=`<td class="ts">#${row.id}</td><td><span class="bcam">${row.camera_name}</span></td><td class="ts">${row.timestamp}</td>
        <td><div class="lbc"><div class="mbar"><div class="mbar-fill" style="width:${pct}%;background:${col}"></div></div><span style="color:${col}">${pct.toFixed(1)}%</span></div></td>
        <td class="ts">${dur}</td><td style="color:var(--accent);font-size:9px">LOGGED</td>`;
      tb.appendChild(tr);
    });
  });
}

/* LOGS */
function loadLogs(){
  fetch('/api/system/logs').then(r=>r.json()).then(rows=>{
    const L=document.getElementById('logList'); L.innerHTML='';
    rows.forEach(row=>{
      const d=document.createElement('div'); d.className='log-entry';
      d.innerHTML=`<span class="log-ts">${row.timestamp}</span><span class="log-type">${row.event_type}</span><span class="log-sev ${row.severity}">${row.severity}</span><span class="log-msg">${row.message}</span>`;
      L.appendChild(d);
    });
  });
}

/* TOAST */
function toast(title,msg){
  const W=document.getElementById('toastWrap');
  const t=document.createElement('div'); t.className='toast';
  t.innerHTML=`<div class="toast-ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg></div>
    <div><div class="toast-title">${title}</div><div class="toast-msg">${msg}</div></div>`;
  t.onclick=()=>{t.classList.add('out');setTimeout(()=>t.remove(),300);};
  W.appendChild(t);
  setTimeout(()=>{t.classList.add('out');setTimeout(()=>t.remove(),300);},4500);
}

/* AUDIO */
let aCtx=null;
function beep(){
  try{
    if(!aCtx)aCtx=new(window.AudioContext||window.webkitAudioContext)();
    const o=aCtx.createOscillator(),g=aCtx.createGain();
    o.connect(g);g.connect(aCtx.destination);
    o.type='sine';o.frequency.setValueAtTime(880,aCtx.currentTime);
    o.frequency.exponentialRampToValueAtTime(440,aCtx.currentTime+.2);
    g.gain.setValueAtTime(.12,aCtx.currentTime);g.gain.exponentialRampToValueAtTime(.001,aCtx.currentTime+.3);
    o.start(aCtx.currentTime);o.stop(aCtx.currentTime+.3);
  }catch(e){}
}

/* SOCKET */
socket.on('motion_alert',data=>{
  alertCount++;
  document.getElementById('altBadge').textContent=alertCount;
  document.getElementById('motPill').style.display='flex';
  toast(`▲ MOTION — ${data.camera_name}`,`Level: ${data.motion_level}%  ·  Time: ${data.timestamp}`);
  document.getElementById('tickMsg').textContent=`Motion on ${data.camera_name}  ·  Level: ${data.motion_level}%  ·  ${data.timestamp}`;
  document.getElementById('tickTime').textContent=new Date().toLocaleTimeString();
  beep();
  refreshStats();
  if(curPanel==='analytics') loadAnalytics();
  if(curPanel==='alerts')    loadAlerts();
});

/* INIT */
setInterval(refreshStats,2500);
setInterval(()=>{ if(curPanel==='analytics') loadAnalytics(); },30000);
window.addEventListener('DOMContentLoaded',()=>{
  initCharts();
  fetch('/api/camera/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"camera_id":0}'}).then(()=>setTimeout(()=>{refreshStats();renderGrid();},600));
});
</script>
</body>
</html>"""

