#!/usr/bin/env python3
"""
Data Run — Transmit Tower Display v2
Waveshare 10.1" DSI on Raspberry Pi, driven by the ESP32 Transmit Tower over USB serial.

v2 adds: boot sequence, ALERT mode on final packet, animated data-flow dots,
         system footer, SCP-style intel overlay on each packet score.

Usage:
    python3 display2.py [port]          (default /dev/ttyUSB0)
    python3 display2.py --demo          (simulate without Arduino)
    python3 display2.py --demo --record (demo + save datarun_TIMESTAMP.mp4)
    python3 display2.py --record        (live + record)
On RPi: WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) SDL_VIDEODRIVER=wayland python3 display2.py
"""

import sys, threading, time, random, math, json, os, subprocess, signal
import pygame
import serial
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────────
DEMO_MODE   = "--demo"   in sys.argv
RECORD_MODE = "--record" in sys.argv
args        = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT      = args[0] if args else "/dev/ttyUSB0"
BAUD      = 115200
W, H      = 1280, 800
FPS       = 30
N_SEGS    = 16
FOOTER_H  = 26

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = (  4,   5,  14)
CYAN     = (  0, 180, 255)
CYAN_DIM = (  0,  18,  36)
CYAN_MID = (  0,  60, 100)
YELLOW   = (200, 180,   0)
YEL_DIM  = ( 14,  12,   0)
GREEN    = (  0, 210,  70)
WHITE    = (215, 215, 215)
GREY     = ( 55,  60,  75)
TITLE_C  = (  0, 230, 255)
ORANGE   = (220, 110,   0)
RED      = (220,  30,  10)
RED_DIM  = ( 30,   4,   2)

def clamp(v, lo, hi): return max(lo, min(hi, v))

# ── Shared state ──────────────────────────────────────────────────────────────
_slock = threading.Lock()
_gs    = {"state": "IDLE", "elapsed": 0, "duration": 5000, "score": 0, "winscore": 5}

def get_gs():
    with _slock: return dict(_gs)

def set_gs(state, elapsed, duration, score, winscore):
    with _slock:
        _gs.update(state=state, elapsed=elapsed, duration=duration,
                   score=score, winscore=winscore)

# ── Terminal log ──────────────────────────────────────────────────────────────
_tlock = threading.Lock()
_tlog  = deque(maxlen=120)

def tpush(line):
    line = line.strip()
    if line:
        with _tlock: _tlog.append(line[:200])

def tsnap():
    with _tlock: return list(_tlog)

# ── Noise ─────────────────────────────────────────────────────────────────────
_NOISE = [
    "HANDSHAKE 0x{a:04X} -> ACK 0x{b:04X}",
    "BLK {n:04d} CRC PASS [{c:08X}]",
    "SYNC SEQ:{s:05d} LAT:{l:3d}ms",
    "CH:{ch:02d} SIG:{db:+d}dBm",
    "BUF {a:04X}:{b:04X} FLUSH {sz:d}B",
    "ROUTE 10.0.{x:d}.{y:d} -> 192.168.{z:d}.1",
    "CRC:{c:04X} SECTOR {n:03d} OK",
    "STREAM {r:d} KB/s",
    "RETRY {t:d}/3 ... OK",
    "FRAG {fa:03d}/{fb:03d} REASSEMBLED",
    "AES-256 BLK {blk:04X} DECRYPTED",
    "0x{a:04X}: {b:02X} {c:02X} {d:02X} {e:02X}  {f:02X} {g:02X} {h:02X} {i:02X}",
    "CRC32 {c:08X} VERIFIED",
    "INJECT PKT {n:04d} QUEUED",
    "BER {ber:.5f} NOMINAL",
    "XMIT WINDOW {w:d} SEGS",
    "LINK QUAL {q:d}%",
    "[WARN] LATENCY {l:d}ms ABOVE THRESHOLD",
    "[ERR] RETRANSMIT SEQ {s:05d}",
    "DECODE BLK {n:03d} PENDING",
    "SECTOR {a:04X}-{b:04X} CLEAN",
    "RSSI:{db:+d}dBm STABLE",
    "NODE {n:02d} HEARTBEAT OK",
]

_ALERT_NOISE = [
    "[ERR] INTRUSION DETECTED — SECTOR {n:03d}",
    "[ERR] FIREWALL BREACH NODE {n:02d} — CONTAINING",
    "[ERR] UNAUTHORIZED UPLINK IN PROGRESS",
    "[WARN] CRITICAL: FINAL DATA PACKET UPLOADING",
    "[ERR] COUNTERMEASURES ENGAGED",
    "[WARN] ENEMY OPERATOR AT TERMINAL",
    "[ERR] ABORT SEQUENCE BLOCKED",
    "[ERR] ACCESS LEVEL OVERRIDDEN",
    "[WARN] DATA EXFILTRATION IMMINENT",
    "[ERR] LAST PACKET — INTERCEPTION FAILED",
]

def _rand_noise(alert=False):
    pool = _ALERT_NOISE if alert and random.random() < 0.65 else _NOISE
    tmpl = random.choice(pool)
    return tmpl.format(
        a=random.randint(0, 0xFFFF), b=random.randint(0, 0xFF),
        c=random.randint(0, 0xFFFFFFFF), s=random.randint(0, 99999),
        l=random.randint(1, 350), ch=random.randint(0, 15),
        db=random.randint(-95, -35), n=random.randint(0, 999),
        x=random.randint(1, 254), y=random.randint(1, 254),
        z=random.randint(0, 9), r=random.randint(10, 980),
        t=random.randint(1, 3), fa=random.randint(0, 127),
        fb=random.randint(128, 255), blk=random.randint(0, 0xFFFF),
        w=random.randint(4, 64), sz=random.randint(64, 4096),
        ber=random.uniform(0, 0.002), q=random.randint(60, 100),
        d=random.randint(0, 255), e=random.randint(0, 255),
        f=random.randint(0, 255), g=random.randint(0, 255),
        h=random.randint(0, 255), i=random.randint(0, 255),
    )

# ── Threads ───────────────────────────────────────────────────────────────────
def noise_thread():
    while True:
        gs    = get_gs()
        alert = gs["state"] == "UPLOADING" and gs["score"] >= gs["winscore"] - 1 and gs["winscore"] > 1
        if alert:
            delay = random.uniform(0.015, 0.055)
        elif gs["state"] == "UPLOADING" and gs["elapsed"] > gs["duration"] * 0.75:
            delay = random.uniform(0.02, 0.09)
        elif gs["state"] == "UPLOADING":
            delay = random.uniform(0.05, 0.18)
        else:
            delay = random.uniform(0.08, 0.28)
        time.sleep(delay)
        tpush(_rand_noise(alert=alert))

def serial_thread():
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=1)
            tpush(f"PORT {PORT} OPEN OK")
            while True:
                raw = ser.readline()
                if not raw: continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line: continue
                if line.startswith("##STATE "):
                    p = line.split()
                    if len(p) == 6:
                        try: set_gs(p[1], int(p[2]), int(p[3]), int(p[4]), int(p[5]))
                        except ValueError: pass
                else:
                    tpush(line)
        except Exception as e:
            tpush(f"SERIAL ERR: {e}")
            time.sleep(3)

def demo_thread():
    ws, sc, dur = 5, 0, 3000
    while True:
        set_gs("IDLE", 0, dur, sc, ws);  tpush("DEMO: awaiting card"); time.sleep(2)
        set_gs("READY", 0, dur, sc, ws); tpush("DEMO: card detected");  time.sleep(1.5)
        tpush("DEMO: uploading...")
        t0 = time.time()
        while True:
            el = int((time.time() - t0) * 1000)
            if el >= dur: break
            set_gs("UPLOADING", el, dur, sc, ws); time.sleep(0.04)
        sc += 1; tpush(f"DEMO: packet uploaded! ({sc}/{ws})")
        set_gs("IDLE", 0, dur, sc, ws)
        time.sleep(INTEL_DUR + 2.0)  # let players read the intel before next cycle
        if sc >= ws:
            set_gs("GAMEOVER", dur, dur, sc, ws); tpush("DEMO: *** ATTACKERS WIN ***")
            time.sleep(6); sc = 0

# ── Boot sequence ─────────────────────────────────────────────────────────────
_BOOT_LINES = [
    ("NUKETOWN SYSTEMS BIOS v2.47.1", False, 0.25),
    ("CPU: NTW-X32 CORTEX @ 240MHz  |  RAM: 512KB  |  FLASH: 4MB", False, 0.20),
    ("POWER SUPPLY: OK  |  WATCHDOG: ARMED  |  SECURE ENCLAVE: READY", False, 0.20),
    ("", False, 0.12),
    ("VERIFYING FIRMWARE SIGNATURE................... [OK]", True, 0.22),
    ("LOADING RFID MODULE (MFRC522v2)................ [OK]", True, 0.18),
    ("LOADING LED STRIP (NEOPIXEL x16)............... [OK]", True, 0.18),
    ("MOUNTING NVS PARTITION......................... [OK]", True, 0.18),
    ("READING GAME CONFIG: upload_ms=20000 win=5..... [OK]", True, 0.25),
    ("", False, 0.12),
    ("INITIATING UPLINK SEQUENCE...", False, 0.40),
    ("  >> HANDSHAKE  0x4E54 -> ACK 0x574F          [OK]", True, 0.22),
    ("  >> AES-256-GCM CHANNEL ESTABLISHED          [OK]", True, 0.22),
    ("  >> PROTOCOL: NUKETOWN SECURE LINK v3.1", False, 0.18),
    ("  >> LINK LATENCY: 12ms  JITTER: 2ms", False, 0.18),
    ("", False, 0.18),
    (">>> SECURE LINK ESTABLISHED <<<", False, 0.35),
    (">>> DATA UPLINK STATION ONLINE <<<", False, 0.30),
]

def boot_sequence(screen, f_term, f_state, present_fn=None):
    if present_fn is None:
        present_fn = pygame.display.flip
    clock   = pygame.time.Clock()
    shown   = []
    lh      = f_term.get_height() + 3
    y0      = 82
    title_s = f_state.render("[ NUKETOWN UPLINK STATION  —  INITIALIZING ]", True, (0, 100, 60))
    tx      = W // 2 - title_s.get_width() // 2

    def redraw():
        screen.fill((0, 0, 0))
        pygame.draw.line(screen, (0, 70, 40), (40, 66), (W - 40, 66), 1)
        screen.blit(title_s, (tx, 20))
        for idx, (ln, ok, _d) in enumerate(shown):
            if not ln:
                continue
            if ln.startswith(">>>"):
                col = (0, 230, 90)
            elif ok and "[OK]" in ln:
                col = (0, 180, 70)
            elif ln.startswith("  >>"):
                col = (0, 130, 100)
            else:
                col = (0, 150, 95)
            screen.blit(f_term.render(ln, True, col), (80, y0 + idx * lh))
        if shown:
            last_ln = shown[-1][0]
            cx = 80 + f_term.size(last_ln)[0]
            cy = y0 + (len(shown) - 1) * lh
            if int(time.time() * 2) % 2 == 0:
                pygame.draw.rect(screen, (0, 180, 80), (cx + 2, cy, 8, lh - 2))
        present_fn()

    for text, ok, delay in _BOOT_LINES:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: pygame.quit(); sys.exit()
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                pygame.quit(); sys.exit()
        shown.append((text, ok, delay))
        end = time.time() + delay
        while time.time() < end:
            redraw(); clock.tick(30)
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT: pygame.quit(); sys.exit()
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit()

    end = time.time() + 0.9
    while time.time() < end:
        redraw(); clock.tick(30)

    fade = pygame.Surface((W, H)); fade.fill((0, 0, 0))
    for alpha in range(0, 256, 20):
        fade.set_alpha(alpha); screen.blit(fade, (0, 0))
        present_fn(); clock.tick(40)

# ── SCP Intel data ────────────────────────────────────────────────────────────
INTEL_DUR = 8.0

# Fallback data used only if intel/ folder is missing or empty
_INTEL_FALLBACK = [
    {
        "id": "SCP-7741", "name": "THE PALE VISITOR", "class": "KETER",
        "containment": "Subject requires ████████-grade barriers at all times. Direct eye contact is strictly prohibited.",
        "description":  "Humanoid entity, height approximately 2.4m. No discernible facial features. Capable of ████████ through solid matter.",
        "addendum":     "Incident 7741-C: Three agents found in [DATA EXPUNGED] state following a 12-minute containment lapse.",
    },
    {
        "id": "SCP-7742", "name": "THE WATCHER", "class": "EUCLID",
        "containment": "Must be kept in complete darkness. Any rhythmic clicking from containment must be reported immediately.",
        "description":  "Amorphous dark mass containing between ██ and ███ photoreceptive organs that track movement independently.",
        "addendum":     "SCP-7742 has demonstrated the ability to ████████ without triggering motion sensors.",
    },
    {
        "id": "SCP-7743", "name": "THE CRAWLER", "class": "EUCLID",
        "containment": "Held in a ██████-reinforced underground chamber. Entry requires two Level-3 personnel minimum.",
        "description":  "Hexapedal predatory entity, 1.8m in length. Has twice breached containment. Maximum recorded speed: ██ km/h.",
        "addendum":     "SCP-7743 appears to recognize individual personnel. Reclassification to Keter is under review.",
    },
    {
        "id": "SCP-7744", "name": "THE LONG MAN", "class": "KETER",
        "containment": "Not successfully contained. Maintain 200m minimum distance. Do not engage alone.",
        "description":  "Bipedal entity, estimated height 3.7m. Limb proportions exceed anatomical possibility. No facial features observed.",
        "addendum":     "SCP-7744 was last confirmed near this operational area 48 hours prior. Do not approach.",
    },
    {
        "id": "SCP-7745", "name": "SIGNAL", "class": "KETER",
        "containment": "Cannot be contained by conventional means. All recording equipment within range will be [DATA CORRUPTED].",
        "description":  "Entity exists partially outside the visible electromagnetic spectrum. Physical interaction has been ████████.",
        "addendum":     "[VISUAL RECORD CORRUPTED — SEE ADDENDUM 7745-D] [ADDENDUM 7745-D ALSO CORRUPTED]",
    },
]

def _load_intel():
    base    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel")
    entries = []
    if os.path.isdir(base):
        for folder in sorted(os.listdir(base)):
            fp = os.path.join(base, folder, "intel.json")
            if os.path.isfile(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        entry = json.load(f)
                    # attach image path if present (checked at draw time)
                    for ext in ("png", "jpg", "jpeg", "webp"):
                        img_path = os.path.join(base, folder, f"image.{ext}")
                        if os.path.isfile(img_path):
                            entry["_image_path"] = img_path
                            break
                    entries.append(entry)
                except Exception:
                    pass
    return entries if entries else _INTEL_FALLBACK

INTEL = _load_intel()

# Cache scaled portrait images so they're only loaded/scaled once per session
_img_cache: dict = {}

# ── Monster portrait drawing ───────────────────────────────────────────────────
def _grain(surf, x, y, w, h, n=280):
    for _ in range(n):
        px = x + random.randint(0, w - 1)
        py = y + random.randint(0, h - 1)
        v  = random.randint(10, 55)
        pygame.draw.rect(surf, (v, int(v * 1.04), v), (px, py, 1, 1))

def _photo_frame(surf, x, y, w, h):
    """Add scan lines and vignette on top of a portrait."""
    for sy in range(y, y + h, 3):
        pygame.draw.line(surf, (0, 0, 0), (x, sy), (x + w, sy), 1)
    for i in range(0, 55, 3):
        a = int(200 * ((55 - i) / 55) ** 2.2)
        r = (0, 0, 0)
        s = pygame.Surface((w - i * 2, h - i * 2), pygame.SRCALPHA)
        pygame.draw.rect(s, (*r, a), (0, 0, w - i * 2, h - i * 2), 2)
        surf.blit(s, (x + i, y + i))

def _portrait_pale_visitor(surf, x, y, w, h, t):
    pygame.draw.rect(surf, (3, 5, 3), (x, y, w, h))
    cx = x + w // 2
    # Body silhouette — tall narrow
    bw, bh = int(w * 0.27), int(h * 0.70)
    by = y + int(h * 0.30)
    pygame.draw.ellipse(surf, (26, 28, 25), (cx - bw // 2, by, bw, bh))
    # Head — oval, slightly lighter
    hw, hh = int(w * 0.22), int(h * 0.20)
    hy = by - hh + int(hh * 0.12)
    pygame.draw.ellipse(surf, (46, 49, 44), (cx - hw // 2, hy, hw, hh))
    # Face — smooth featureless, lighter
    fw, fh = int(hw * 0.78), int(hh * 0.80)
    pygame.draw.ellipse(surf, (70, 73, 68),
                        (cx - fw // 2, hy + int(hh * 0.10), fw, fh))
    # Arms — too long, past hips
    shoulder_y = by + int(bh * 0.10)
    arm_end_y  = by + bh + int(h * 0.05)
    for dx in [-1, 1]:
        ax = cx + dx * (bw // 2 - 3)
        pygame.draw.line(surf, (20, 22, 20),
                         (ax, shoulder_y),
                         (ax + dx * int(w * 0.12), arm_end_y), 5)

def _portrait_watcher(surf, x, y, w, h, t):
    pygame.draw.rect(surf, (2, 3, 2), (x, y, w, h))
    cx, cy = x + w // 2, y + h // 2
    # Barely-visible dark mass
    pygame.draw.ellipse(surf, (7, 9, 7),
                        (cx - int(w * 0.42), cy - int(h * 0.32),
                         int(w * 0.84), int(h * 0.64)))
    # Eyes — positions, sizes, iris colours (deterministic)
    eyes = [
        (0.28, 0.28, 23, 14, (25, 190, 45)),
        (0.62, 0.22, 19, 11, (190, 160, 20)),
        (0.46, 0.48, 27, 17, (25, 35, 210)),
        (0.18, 0.54, 16, 9,  (210, 25, 25)),
        (0.68, 0.40, 21, 13, (25, 210, 170)),
        (0.50, 0.68, 15, 9,  (185, 25, 185)),
        (0.34, 0.62, 21, 13, (25, 185, 55)),
        (0.72, 0.58, 17, 10, (200, 120, 25)),
        (0.13, 0.36, 13, 8,  (25, 165, 190)),
    ]
    for ex_f, ey_f, ew, eh, iris in eyes:
        ex = x + int(w * ex_f)
        ey = y + int(h * ey_f)
        pygame.draw.ellipse(surf, (205, 205, 195),
                            (ex - ew, ey - eh, ew * 2, eh * 2))
        iw, ih = int(ew * 0.68), int(eh * 0.68)
        pygame.draw.ellipse(surf, iris, (ex - iw, ey - ih, iw * 2, ih * 2))
        pr = max(2, int(ew * 0.34))
        pygame.draw.circle(surf, (0, 0, 0), (ex, ey), pr)
        pygame.draw.circle(surf, (255, 255, 255),
                           (ex - pr // 3, ey - pr // 3), 2)
        # Slow individual blink
        bv = math.sin(t * 0.65 + (ew * 7 + eh * 13) % 11)
        if bv > 0.82:
            frac   = (bv - 0.82) / 0.18
            lid_h  = int(eh * 2 * frac)
            pygame.draw.rect(surf, (4, 6, 4),
                             (ex - ew, ey - eh, ew * 2, min(lid_h, eh * 2)))

def _portrait_crawler(surf, x, y, w, h, t):
    pygame.draw.rect(surf, (3, 5, 3), (x, y, w, h))
    cx = x + w // 2
    cy = y + int(h * 0.54)
    bw, bh = int(w * 0.68), int(h * 0.17)
    pygame.draw.ellipse(surf, (30, 34, 27), (cx - bw // 2, cy - bh // 2, bw, bh))
    # Six legs — deterministic offsets
    for i in range(6):
        lx  = cx - bw // 2 + int(bw * (i + 0.5) / 6)
        ll  = int(h * 0.14) + i * 5
        lox = (i % 2) * 2 - 1
        pygame.draw.line(surf, (22, 25, 19),
                         (lx, cy + bh // 3),
                         (lx + lox * 9, cy + bh // 3 + ll), 3)
    # Head
    hcx = x + int(w * 0.13)
    hw, hh = int(w * 0.19), int(h * 0.13)
    pygame.draw.ellipse(surf, (36, 40, 30),
                        (hcx - hw // 2, cy - hh // 2, hw, hh))
    pygame.draw.circle(surf, (175, 155, 15), (hcx, cy - hh // 4), 5)
    pygame.draw.circle(surf, (0, 0, 0),     (hcx, cy - hh // 4), 3)
    for j in range(5):
        pygame.draw.line(surf, (8, 8, 6),
                         (hcx - hw // 3 + j * 5, cy + 3),
                         (hcx - hw // 3 + j * 5 + 2, cy + 12), 2)

def _portrait_long_man(surf, x, y, w, h, t):
    pygame.draw.rect(surf, (3, 4, 3), (x, y, w, h))
    cx = x + w // 2
    # Head — small, near top
    hr   = int(w * 0.09)
    hy   = y + int(h * 0.05) + hr
    pygame.draw.circle(surf, (38, 40, 36), (cx, hy), hr)
    pygame.draw.ellipse(surf, (60, 63, 57),
                        (cx - int(hr * 0.72), hy - int(hr * 0.55),
                         int(hr * 1.44), int(hr * 1.1)))
    # Torso
    tw, th = int(w * 0.11), int(h * 0.44)
    ty = hy + hr - 2
    pygame.draw.rect(surf, (32, 34, 30), (cx - tw // 2, ty, tw, th))
    # Legs — thin, reaching to bottom
    lb = y + h - int(h * 0.02)
    pygame.draw.line(surf, (26, 28, 24),
                     (cx - tw // 3, ty + th),
                     (cx - tw // 2 - 7, lb), 4)
    pygame.draw.line(surf, (26, 28, 24),
                     (cx + tw // 3, ty + th),
                     (cx + tw // 2 + 7, lb), 4)
    # Arms — far too long, reaching below waist
    ay = ty + int(th * 0.14)
    ab = ty + th + int(h * 0.17)
    pygame.draw.line(surf, (28, 30, 26),
                     (cx - tw // 2, ay),
                     (cx - tw // 2 - int(w * 0.24), ab), 4)
    pygame.draw.line(surf, (28, 30, 26),
                     (cx + tw // 2, ay),
                     (cx + tw // 2 + int(w * 0.24), ab), 4)
    # Extra scan-noise bands
    for i in range(10):
        sy = y + int(h * i / 10) + random.randint(0, h // 10 - 1)
        pygame.draw.line(surf, (0, 0, 0), (x, sy), (x + w, sy), 1)

def _portrait_signal(surf, x, y, w, h, t):
    pygame.draw.rect(surf, (1, 2, 1), (x, y, w, h))
    # Horizontal noise bands
    for i in range(40):
        by2  = y + int(h * i / 40)
        bh2  = random.randint(2, h // 28)
        v    = random.randint(0, 35)
        pygame.draw.rect(surf, (v // 3, v // 2, v // 3), (x, by2, w, bh2))
    # Faint central figure suggestion — slightly brighter vertical region
    fig_w, fig_h = int(w * 0.32), int(h * 0.68)
    fig_s = pygame.Surface((fig_w, fig_h), pygame.SRCALPHA)
    fig_s.fill((18, 28, 18, 35))
    surf.blit(fig_s, (x + (w - fig_w) // 2, y + (h - fig_h) // 2))
    # Horizontal tear lines
    for _ in range(6):
        ty2  = y + random.randint(0, h - 1)
        tlen = random.randint(w // 5, w)
        tx2  = x + random.randint(0, w - tlen)
        v    = random.randint(15, 50)
        pygame.draw.rect(surf, (v // 4, v // 3, v // 4),
                         (tx2, ty2, tlen, random.randint(2, 7)))
    # Heavy grain
    _grain(surf, x, y, w, h, n=500)

def draw_monster_portrait(surf, x, y, w, h, idx, t):
    entry     = INTEL[idx % len(INTEL)]
    img_path  = entry.get("_image_path")
    used_image = False
    if img_path:
        cache_key = (img_path, w, h)
        if cache_key not in _img_cache:
            try:
                raw = pygame.image.load(img_path).convert()
                _img_cache[cache_key] = pygame.transform.scale(raw, (w, h))
            except Exception:
                _img_cache[cache_key] = None
        img_surf = _img_cache.get(cache_key)
        if img_surf:
            surf.blit(img_surf, (x, y))
            used_image = True
    if not used_image:
        fn = [_portrait_pale_visitor, _portrait_watcher, _portrait_crawler,
              _portrait_long_man, _portrait_signal]
        fn[idx % len(fn)](surf, x, y, w, h, t)
        _grain(surf, x, y, w, h)
    _photo_frame(surf, x, y, w, h)
    pygame.draw.rect(surf, (50, 70, 50), (x, y, w, h), 1)

# ── Intel overlay ─────────────────────────────────────────────────────────────
# Panel geometry
_OV_X, _OV_Y, _OV_W, _OV_H = 60, 52, 1160, 696
_HDR_H  = 44
_PORT_W = 400
_CDN_H  = 38

_PORT_X = _OV_X + 10
_PORT_Y = _OV_Y + _HDR_H + 10
_PORT_H = _OV_H - _HDR_H - _CDN_H - 24

_TXT_X  = _PORT_X + _PORT_W + 14
_TXT_Y  = _PORT_Y
_TXT_W  = (_OV_X + _OV_W - 10) - _TXT_X
_TXT_H  = _PORT_H

_CDN_Y2 = _OV_Y + _OV_H - _CDN_H - 8

_CLASS_COL = {"KETER": (220, 30, 10), "EUCLID": (220, 140, 0), "SAFE": (0, 200, 60)}

def _wrap(text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines

def draw_intel_overlay(screen, f_state, f_label, f_cdn, f_body, entry, timer, max_timer, t, blink):
    idx = INTEL.index(entry)

    # Dim backdrop
    dim = pygame.Surface((W, H), pygame.SRCALPHA)
    dim.fill((0, 0, 0, 215))
    screen.blit(dim, (0, 0))

    # Panel background + border
    pygame.draw.rect(screen, (6, 7, 10),   (_OV_X, _OV_Y, _OV_W, _OV_H))
    pygame.draw.rect(screen, (150, 0, 0),  (_OV_X, _OV_Y, _OV_W, _OV_H), 2)

    # Header bar
    hcol = (130, 0, 0) if blink else (110, 0, 0)
    pygame.draw.rect(screen, hcol, (_OV_X, _OV_Y, _OV_W, _HDR_H))
    hdr_s = f_state.render(
        "⚠   RESTRICTED ACCESS  //  INTEL PACKAGE RECEIVED   ⚠", True, (255, 210, 0))
    screen.blit(hdr_s, (_OV_X + _OV_W // 2 - hdr_s.get_width() // 2, _OV_Y + 6))

    # Portrait
    draw_monster_portrait(screen, _PORT_X, _PORT_Y, _PORT_W, _PORT_H, idx, t)

    # ID label below portrait
    id_col = _CLASS_COL.get(entry["class"], WHITE)
    id_s   = f_label.render(f"{entry['id']}  //  {entry['name']}", True, id_col)
    screen.blit(id_s, (_PORT_X, _PORT_Y + _PORT_H + 6))
    cls_s  = f_label.render(f"OBJECT CLASS: {entry['class']}", True, id_col)
    screen.blit(cls_s, (_PORT_X, _PORT_Y + _PORT_H + 22))

    # Text area — section headers + wrapped body
    ty  = _TXT_Y
    lh  = f_body.get_height() + 4
    lhH = f_label.get_height() + 4

    def section(header, body, hcol2, bcol):
        nonlocal ty
        hs = f_label.render(header, True, hcol2)
        screen.blit(hs, (_TXT_X, ty));  ty += lhH + 2
        for ln in _wrap(body, f_body, _TXT_W):
            if ty + lh > _TXT_Y + _TXT_H: break
            screen.blit(f_body.render(ln, True, bcol), (_TXT_X, ty));  ty += lh
        ty += 10

    pygame.draw.line(screen, (80, 0, 0), (_TXT_X, ty), (_TXT_X + _TXT_W, ty), 1)
    ty += 8

    section("[ SPECIAL CONTAINMENT PROCEDURES ]",
            entry["containment"], (180, 0, 0), (165, 140, 140))
    section("[ DESCRIPTION ]",
            entry["description"], (140, 120, 0), (165, 155, 110))
    section("[ ADDENDUM ]",
            entry["addendum"], (80, 100, 80), (130, 160, 130))

    # Bottom separator
    pygame.draw.line(screen, (80, 0, 0),
                     (_OV_X + 8, _CDN_Y2 - 6), (_OV_X + _OV_W - 8, _CDN_Y2 - 6), 1)

    # Countdown bar
    frac_c  = timer / max(max_timer, 1)
    bar_w_c = int((_OV_W - 20) * frac_c)
    pygame.draw.rect(screen, (30, 4, 4),  (_OV_X + 10, _CDN_Y2, _OV_W - 20, _CDN_H - 6))
    pygame.draw.rect(screen, (180, 0, 0), (_OV_X + 10, _CDN_Y2, bar_w_c,    _CDN_H - 6))
    cdn_s = f_cdn.render(
        f"AUTO-DISMISS IN {timer:.1f}s  //  DO NOT DISTRIBUTE  //  LEVEL 5 CLEARANCE REQUIRED",
        True, (200, 180, 180))
    screen.blit(cdn_s, (_OV_X + _OV_W // 2 - cdn_s.get_width() // 2,
                        _CDN_Y2 + (_CDN_H - 6 - cdn_s.get_height()) // 2))

# ── Particle system ───────────────────────────────────────────────────────────
_particles = []

class _P:
    __slots__ = ['x', 'y', 'vx', 'vy', 'life', 'col', 'sz']
    def __init__(self, x, y, col):
        self.x   = float(x);  self.y = float(y)
        a        = random.uniform(0, math.tau)
        spd      = random.uniform(2, 9)
        self.vx  = math.cos(a) * spd
        self.vy  = math.sin(a) * spd - random.uniform(0, 4)
        self.life = 1.0;  self.col = col;  self.sz = random.randint(2, 5)

def spawn_particles(x, y, col, n=60):
    for _ in range(n): _particles.append(_P(x, y, col))

def tick_particles(dt):
    i = 0
    while i < len(_particles):
        p = _particles[i]
        p.x += p.vx;  p.y += p.vy;  p.vy += 0.2;  p.life -= dt * 1.3
        if p.life <= 0: _particles.pop(i)
        else: i += 1

def draw_particles(surf):
    for p in _particles:
        col = tuple(max(0, int(c * p.life)) for c in p.col)
        sz  = max(1, int(p.sz * p.life))
        pygame.draw.circle(surf, col, (int(p.x), int(p.y)), sz)

# ── Data flow dots ────────────────────────────────────────────────────────────
_flow_dots = []

class _FD:
    __slots__ = ['x', 'y', 'spd', 'bright']
    def __init__(self, bar_x, bar_y, bar_h):
        self.x      = float(bar_x)
        self.y      = float(bar_y + random.randint(6, bar_h - 6))
        self.spd    = random.uniform(4, 12)
        self.bright = random.randint(180, 255)

def update_flow_dots(bar_x, bar_y, bar_w, bar_h, frac):
    limit = bar_x + bar_w * max(frac, 0)
    if frac > 0.02 and random.random() < 0.45:
        _flow_dots.append(_FD(bar_x, bar_y, bar_h))
    i = 0
    while i < len(_flow_dots):
        d = _flow_dots[i]
        d.x += d.spd
        if d.x >= limit: _flow_dots.pop(i)
        else: i += 1

def draw_flow_dots(surf, bar_x, bar_y, bar_w, bar_h, frac):
    limit = bar_x + bar_w * max(frac, 0)
    span  = max(limit - bar_x, 1)
    for d in _flow_dots:
        progress = (d.x - bar_x) / span
        dim = int(d.bright * (1.0 - progress * 0.55))
        col = (dim, int(dim * 0.88), 0)
        pygame.draw.circle(surf, col, (int(d.x), int(d.y)), 2)

# ── Radar ─────────────────────────────────────────────────────────────────────
_contacts = []

def update_radar(sweep):
    global _contacts
    for c in _contacts: c[2] += 0.025
    _contacts = [c for c in _contacts if c[2] < 1.0]
    if random.random() < 0.04:
        _contacts.append([sweep + random.uniform(-0.4, 0.4),
                           random.uniform(0.15, 0.92), 0.0])

def draw_radar(surf, cx, cy, r, sweep):
    pygame.draw.circle(surf, (0, 12, 8), (cx, cy), r)
    for rr in [r // 3, 2 * r // 3]:
        pygame.draw.circle(surf, (0, 40, 25), (cx, cy), rr, 1)
    pygame.draw.line(surf, (0, 35, 22), (cx - r, cy), (cx + r, cy), 1)
    pygame.draw.line(surf, (0, 35, 22), (cx, cy - r), (cx, cy + r), 1)
    for i in range(28, 0, -1):
        ta  = sweep - i * 0.09
        dim = int(100 * (1 - i / 28))
        ex  = cx + int(math.cos(ta) * r * 0.92)
        ey  = cy + int(math.sin(ta) * r * 0.92)
        pygame.draw.line(surf, (0, dim, dim // 2), (cx, cy), (ex, ey), 1)
    ex = cx + int(math.cos(sweep) * r * 0.92)
    ey = cy + int(math.sin(sweep) * r * 0.92)
    pygame.draw.line(surf, (0, 200, 80), (cx, cy), (ex, ey), 2)
    for c in _contacts:
        bx = cx + int(math.cos(c[0]) * c[1] * r)
        by = cy + int(math.sin(c[0]) * c[1] * r)
        a  = max(0.0, 1.0 - c[2])
        pygame.draw.circle(surf, (0, int(220 * a), int(90 * a)),
                           (bx, by), max(1, int(4 * a)))
    pygame.draw.circle(surf, (0, 160, 60), (cx, cy), r, 2)

# ── Waveform strip ────────────────────────────────────────────────────────────
def draw_wave(surf, x, y, w, h, t, state, frac, alert=False):
    mid = y + h // 2
    pygame.draw.line(surf, (0, 25, 18), (x, mid), (x + w, mid), 1)
    if alert:
        freq  = 0.052 + random.uniform(-0.012, 0.012)
        amp   = 20 + random.randint(-5, 9)
        speed = 0.14; c1 = RED; c2 = RED_DIM
    elif state == "UPLOADING":
        freq = 0.028 + frac * 0.05;  amp = 7 + int(frac * 16)
        speed = 0.055 + frac * 0.10; c1 = CYAN;   c2 = (0, 50, 90)
    elif state == "GAMEOVER":
        freq = 0.065; amp = 22; speed = 0.13; c1 = GREEN;  c2 = (0, 55, 18)
    elif state == "READY":
        freq = 0.028; amp = 9;  speed = 0.045; c1 = YELLOW; c2 = (50, 42, 0)
    else:
        freq = 0.018; amp = 4;  speed = 0.022; c1 = (0, 75, 115); c2 = (0, 18, 32)
    for col, poff, thick in [(c2, math.pi, 1), (c1, 0, 2)]:
        pts = [(x + px, clamp(mid + int(math.sin(px * freq + t * speed * 60 + poff) * amp),
                              y + 1, y + h - 1))
               for px in range(0, w, 1)]
        if len(pts) > 1: pygame.draw.lines(surf, col, False, pts, thick)

# ── Pre-rendered surfaces ─────────────────────────────────────────────────────
def make_circuit_bg():
    s = pygame.Surface((W, H), pygame.SRCALPHA)
    random.seed(42)
    for _ in range(130):
        x1 = random.randint(0, W);  y1 = random.randint(0, H)
        ln = random.randint(25, 200); col = (0, 50, 80, 26)
        if random.random() > 0.5:
            pygame.draw.line(s, col, (x1, y1), (x1 + ln, y1), 1)
            if random.random() > 0.5:
                pygame.draw.line(s, col, (x1 + ln, y1),
                                 (x1 + ln, y1 + random.randint(20, 80)), 1)
        else:
            pygame.draw.line(s, col, (x1, y1), (x1, y1 + ln), 1)
            if random.random() > 0.5:
                pygame.draw.line(s, col, (x1, y1 + ln),
                                 (x1 + random.randint(20, 80), y1 + ln), 1)
    for _ in range(50):
        pygame.draw.circle(s, (0, 80, 120, 36),
                           (random.randint(0, W), random.randint(0, H)), 2)
    random.seed()
    return s

def make_vignette():
    s = pygame.Surface((W, H), pygame.SRCALPHA)
    for i in range(0, 160, 2):
        a = int(160 * ((160 - i) / 160) ** 2.2)
        pygame.draw.rect(s, (0, 0, 0, a), (i, i, W - i * 2, H - i * 2), 2)
    return s

def make_scanlines():
    s = pygame.Surface((W, H), pygame.SRCALPHA)
    for y in range(0, H, 2):
        pygame.draw.line(s, (0, 0, 0, 26), (0, y), (W, y))
    return s

_gbuf = None
def _init_gbuf():
    global _gbuf
    _gbuf = pygame.Surface((W + 60, 112), pygame.SRCALPHA)

# ── Drawing helpers ───────────────────────────────────────────────────────────
def draw_glow_bar(surf, x, y, w, h, frac, fill_col, dim_col, n=N_SEGS, gap=4):
    seg_w    = (w - gap * (n - 1)) / n
    filled   = int(frac * n + 0.5)
    filled_w = max(0, int(filled * (seg_w + gap) - gap))
    if filled_w > 0:
        _gbuf.fill((0, 0, 0, 0));  P = 15
        for spread, alpha in [(15, 14), (9, 30), (5, 52)]:
            pygame.draw.rect(_gbuf, (*fill_col, alpha),
                             (P - spread, P - spread,
                              filled_w + spread * 2, h + spread * 2), border_radius=8)
        surf.blit(_gbuf, (x - P, y - P))
    for i in range(n):
        sx  = int(x + i * (seg_w + gap));  sw = max(1, int(seg_w))
        col = (tuple(min(255, int(c * 1.4)) for c in fill_col)
               if i == filled - 1 else fill_col) if i < filled else dim_col
        pygame.draw.rect(surf, col, (sx, y, sw, h), border_radius=4)

def draw_brackets(surf, x, y, w, h, col, sz=18, t=2):
    for bx, by, dx, dy in [(x, y, 1, 1), (x+w, y, -1, 1),
                             (x, y+h, 1, -1), (x+w, y+h, -1, -1)]:
        pygame.draw.line(surf, col, (bx, by), (bx + dx * sz, by), t)
        pygame.draw.line(surf, col, (bx, by), (bx, by + dy * sz), t)

def draw_packet_icons(surf, cx, y, score, winscore, sz=24, gap=10):
    total = winscore * sz + (winscore - 1) * gap;  x0 = cx - total // 2
    for i in range(winscore):
        px  = x0 + i * (sz + gap);  on = i < score
        col = CYAN if on else CYAN_DIM
        pygame.draw.rect(surf, col, (px, y, sz, sz), border_radius=4)
        if on:
            inner = tuple(min(255, int(c * 1.5)) for c in CYAN)
            pygame.draw.rect(surf, inner, (px + 6, y + 6, sz - 12, sz - 12), border_radius=2)

def draw_alert_border(surf, t, alert):
    if not alert: return
    pulse = int(80 + 80 * abs(math.sin(t * 4.0)))
    col   = (min(255, 150 + pulse), 0, 0)
    pygame.draw.rect(surf, col, (0, 0, W, H), 5)
    sz = 44
    for cx, cy, dx, dy in [(0, 0, 1, 1), (W-1, 0, -1, 1), (0, H-1, 1, -1), (W-1, H-1, -1, -1)]:
        pygame.draw.line(surf, col, (cx, cy), (cx + dx * sz, cy), 7)
        pygame.draw.line(surf, col, (cx, cy), (cx, cy + dy * sz), 7)

def draw_footer(surf, f_foot, start_time, now_t, cpu, mem, temp):
    fy = H - FOOTER_H
    pygame.draw.line(surf, (0, 50, 35), (0, fy), (W, fy), 1)
    pygame.draw.rect(surf, (0, 4, 2), (0, fy + 1, W, FOOTER_H - 1))
    uptime_s   = int(now_t - start_time)
    hh, rem    = divmod(uptime_s, 3600)
    mm, ss     = divmod(rem, 60)
    text = (f"  CPU: {cpu} MHz  |  MEM: {mem:.1f} GB  |  TEMP: {temp}°C  |  "
            f"UPTIME: {hh:02d}:{mm:02d}:{ss:02d}  |  LINK: AES-256  |  NODE: UPLINK-01")
    surf.blit(f_foot.render(text, True, (0, 100, 65)), (0, fy + 6))

def hline(surf, y, col=GREY, pad=44, thick=1):
    pygame.draw.line(surf, col, (pad, y), (W - pad, y), thick)

def blit_c(surf, s, cx, y): surf.blit(s, (cx - s.get_width() // 2, y))
def blit_r(surf, s, rx, y): surf.blit(s, (rx - s.get_width(), y))

def glitch(text, chance=0.04):
    if random.random() > chance: return text
    chars = list(text)
    for _ in range(random.randint(1, 3)):
        i = random.randint(0, len(chars) - 1)
        if chars[i] != ' ': chars[i] = random.choice('!#@%░▒▓?$')
    return ''.join(chars)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    pygame.init()

    # Auto-scale to fit the available display (handles X11/MobaXterm windowed use)
    info   = pygame.display.Info()
    scale  = min(info.current_w / W, info.current_h / H, 1.0)
    win_w  = max(int(W * scale), 320)
    win_h  = max(int(H * scale), 200)
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    flags      = (pygame.FULLSCREEN | pygame.NOFRAME) if is_wayland else 0
    fps        = FPS if is_wayland else 15
    try:
        window = pygame.display.set_mode((win_w, win_h), flags)
    except Exception:
        window = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption("DATA RUN")
    pygame.mouse.set_visible(False)

    # Render internally at full 1280×800; scale to window if needed
    screen      = pygame.Surface((W, H)) if scale < 1.0 else window
    scaled_surf = pygame.Surface((win_w, win_h)) if scale < 1.0 else None

    rec_proc = None  # ffmpeg process, started after boot if --record

    def present():
        if scaled_surf is not None:
            pygame.transform.scale(screen, (win_w, win_h), scaled_surf)
            window.blit(scaled_surf, (0, 0))
        if rec_proc is not None:
            try:
                rec_proc.stdin.write(pygame.image.tobytes(screen, "RGB"))
            except Exception:
                pass
        pygame.display.flip()

    def quit_clean():
        if rec_proc is not None:
            try:
                rec_proc.stdin.close()
                rec_proc.wait(timeout=60)
            except Exception:
                pass
        pygame.quit()
        sys.exit()

    signal.signal(signal.SIGINT, lambda s, f: quit_clean())

    f_huge  = pygame.font.SysFont("monospace", 88, bold=True)
    f_sub   = pygame.font.SysFont("monospace", 18, bold=True)
    f_state = pygame.font.SysFont("monospace", 32, bold=True)
    f_label = pygame.font.SysFont("monospace", 20, bold=True)
    f_pct   = pygame.font.SysFont("monospace", 40, bold=True)
    f_cdn   = pygame.font.SysFont("monospace", 22, bold=True)
    f_sys        = pygame.font.SysFont("monospace", 13)
    f_term       = pygame.font.SysFont("monospace", 17, bold=True)
    f_foot       = pygame.font.SysFont("monospace", 12)
    f_intel_body = pygame.font.SysFont("monospace", 21, bold=True)

    boot_sequence(screen, f_term, f_state, present_fn=present)

    if DEMO_MODE:
        threading.Thread(target=demo_thread, daemon=True).start()
    else:
        threading.Thread(target=serial_thread, daemon=True).start()
    threading.Thread(target=noise_thread, daemon=True).start()

    if RECORD_MODE:
        ts       = time.strftime("%Y%m%d_%H%M%S")
        rec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                f"datarun_{ts}.mp4")
        rec_proc = subprocess.Popen(
            ["ffmpeg", "-y",
             "-f", "rawvideo", "-vcodec", "rawvideo",
             "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
             "-i", "pipe:",
             "-vcodec", "libx264", "-pix_fmt", "yuv420p",
             "-crf", "23", "-preset", "fast",
             rec_path],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        tpush(f"REC -> {os.path.basename(rec_path)}")

    circuit    = make_circuit_bg()
    vignette   = make_vignette()
    scanlines  = make_scanlines()
    _init_gbuf()
    flash_surf  = pygame.Surface((W, H), pygame.SRCALPHA)
    win_surf    = pygame.Surface((W, H), pygame.SRCALPHA)
    alert_surf  = pygame.Surface((W, H), pygame.SRCALPHA)

    clock = pygame.time.Clock()
    PAD   = 48;  BAR_W = W - PAD * 2;  BAR_H = 52

    # Layout
    BANNER_H   = 120;  SEP1 = BANNER_H + 1
    STATUS_Y   = SEP1 + 8;    SEP2      = STATUS_Y + 46 + 6
    SCORE_LBL  = SEP2 + 12;   SCORE_BAR = SCORE_LBL + 22
    SCORE_ICON = SCORE_BAR + BAR_H + 10;  SEP3 = SCORE_ICON + 34 + 8
    ULOAD_LBL  = SEP3 + 12;   ULOAD_BAR = ULOAD_LBL + 22
    PCT_Y      = ULOAD_BAR + BAR_H + 6
    CDN_Y      = PCT_Y + 52
    SEP4       = CDN_Y + 30
    WAVE_Y     = SEP4 + 5;    WAVE_H = 48
    SEP5       = WAVE_Y + WAVE_H + 4;  TERM_Y = SEP5 + 4

    RCX = W - 58;  RCY = 54;  RR = 46
    SYS = ["UPLINK  : ACTIVE", "ENCRYPT : AES-256", "PROTOCOL: SECURE"]

    blink        = True;  last_blink = time.time()
    flash_alpha  = 0;     prev_score = 0
    scan_phase   = 0.0;   radar_angle = 0.0
    wave_t       = 0.0;   last_t = time.time()

    # Footer state
    start_time  = time.time()
    cpu         = random.randint(820, 860)
    mem         = round(random.uniform(3.8, 4.2), 1)
    temp        = random.randint(46, 50)
    last_footer = 0.0

    # Intel state
    intel_show  = False
    intel_timer = 0.0
    intel_entry = None

    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: quit_clean()
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE: quit_clean()
                if intel_show: intel_show = False
            if ev.type == pygame.MOUSEBUTTONDOWN:
                if intel_show: intel_show = False

        now_t = time.time();  dt = min(now_t - last_t, 0.1);  last_t = now_t
        if now_t - last_blink >= 0.45:
            blink = not blink;  last_blink = now_t

        gs    = get_gs()
        alert = (gs["state"] == "UPLOADING"
                 and gs["score"] >= gs["winscore"] - 1
                 and gs["winscore"] > 1)

        scan_phase  = (scan_phase + 0.14) % math.tau
        radar_angle = (radar_angle + 0.04) % math.tau
        wave_t      = now_t
        update_radar(radar_angle)
        tick_particles(dt)

        # Intel timer countdown
        if intel_show:
            intel_timer -= dt
            if intel_timer <= 0:
                intel_show = False

        if now_t - last_footer >= 0.5:
            last_footer = now_t
            cpu  = clamp(cpu  + random.randint(-8, 8),  780, 940)
            mem  = round(clamp(mem + random.uniform(-0.05, 0.05), 3.6, 4.5), 1)
            temp = clamp(temp + random.randint(-1, 1),   44,  58)

        # Score change → particles + intel
        if gs["score"] != prev_score and gs["score"] > 0:
            flash_alpha = 200
            cx = PAD + int(BAR_W * gs["score"] / max(gs["winscore"], 1))
            spawn_particles(clamp(cx, PAD, W - PAD), SCORE_BAR + BAR_H // 2, CYAN, n=80)
            intel_entry = INTEL[(gs["score"] - 1) % len(INTEL)]
            intel_show  = True
            intel_timer = INTEL_DUR
            prev_score  = gs["score"]

        if gs["state"] == "GAMEOVER" and len(_particles) < 140:
            col = CYAN if random.random() > 0.4 else GREEN
            spawn_particles(random.randint(PAD, W - PAD),
                            SCORE_BAR + BAR_H // 2, col, n=2)

        if gs["state"] == "UPLOADING":
            frac_u = gs["elapsed"] / max(gs["duration"], 1);  u_col = YELLOW
        elif gs["state"] == "GAMEOVER":
            frac_u = 1.0;  u_col = GREEN
        else:
            frac_u = 0.0;  u_col = GREY

        if gs["state"] == "UPLOADING":
            update_flow_dots(PAD, ULOAD_BAR, BAR_W, BAR_H, frac_u)
        else:
            _flow_dots.clear()

        # ── Draw ──────────────────────────────────────────────────────────────
        screen.fill(BG)
        screen.blit(circuit, (0, 0))

        if gs["state"] == "GAMEOVER":
            win_surf.fill((0, 180, 50, 12)); screen.blit(win_surf, (0, 0))

        if alert:
            a_intensity = int(10 + 9 * abs(math.sin(now_t * 3.0)))
            alert_surf.fill((80, 0, 0, a_intensity)); screen.blit(alert_surf, (0, 0))

        # Banner
        hline(screen, 1, CYAN, thick=2)
        blit_c(screen, f_huge.render(glitch("NUKETOWN"), True, TITLE_C), W // 2, 6)
        blit_c(screen, f_sub.render("[ D A T A   U P L I N K   S T A T I O N ]",
                                    True, (0, 75, 108)), W // 2, 95)
        for si, sl in enumerate(SYS):
            screen.blit(f_sys.render(sl, True, (0, 90, 130)), (PAD, 14 + si * 18))
        draw_radar(screen, RCX, RCY, RR, radar_angle)
        hline(screen, SEP1, CYAN, thick=2)

        # Status
        if alert:
            s_label = "!  CRITICAL — FINAL PACKET UPLOADING  !"
            s_col   = RED if blink else ORANGE
        else:
            scfg = {
                "IDLE":      ("AWAITING CARD INPUT",             WHITE),
                "READY":     ("CARD DETECTED — INITIATE UPLOAD", YELLOW),
                "UPLOADING": ("UPLOADING DATA ...",              CYAN),
                "GAMEOVER":  ("* * *  A T T A C K E R S  W I N  * * *",
                              GREEN if blink else YELLOW),
            }
            s_label, s_col = scfg.get(gs["state"], ("UNKNOWN STATE", GREY))
        blit_c(screen, f_state.render(s_label, True, s_col), W // 2, STATUS_Y + 6)
        hline(screen, SEP2)

        # Score bar
        pulse      = 0.88 + 0.12 * abs(math.sin(now_t * 1.8)) if gs["state"] == "IDLE" else 1.0
        score_cyan = tuple(int(c * pulse) for c in CYAN)
        frac_s     = gs["score"] / max(gs["winscore"], 1)
        screen.blit(f_label.render("DATA PACKETS UPLOADED", True, CYAN_MID), (PAD, SCORE_LBL))
        draw_glow_bar(screen, PAD, SCORE_BAR, BAR_W, BAR_H, frac_s, score_cyan, CYAN_DIM)
        draw_brackets(screen, PAD - 5, SCORE_BAR - 5, BAR_W + 10, BAR_H + 10, CYAN_MID)
        draw_packet_icons(screen, W // 2, SCORE_ICON, gs["score"], gs["winscore"])
        hline(screen, SEP3)

        # Upload bar
        screen.blit(f_label.render("CURRENT UPLOAD", True, u_col), (PAD, ULOAD_LBL))
        blit_r(screen, f_label.render(f"{int(frac_u * 100)}%", True, u_col), W - PAD, ULOAD_LBL)
        draw_glow_bar(screen, PAD, ULOAD_BAR, BAR_W, BAR_H, frac_u, YELLOW, YEL_DIM)
        b_col = YELLOW if gs["state"] in ("UPLOADING", "GAMEOVER") else (28, 25, 0)
        draw_brackets(screen, PAD - 5, ULOAD_BAR - 5, BAR_W + 10, BAR_H + 10, b_col)

        if gs["state"] == "UPLOADING":
            draw_flow_dots(screen, PAD, ULOAD_BAR, BAR_W, BAR_H, frac_u)

        if gs["state"] == "UPLOADING" and frac_u > 0.005:
            sx  = int(PAD + BAR_W * min(frac_u, 0.9995))
            pl  = int(abs(math.sin(scan_phase)) * 100 + 155)
            pygame.draw.line(screen, (255, 255, pl),
                             (sx, ULOAD_BAR - 8), (sx, ULOAD_BAR + BAR_H + 8), 3)

        blit_c(screen, f_pct.render(f"{int(frac_u * 100)}%", True, u_col), W // 2, PCT_Y)
        if gs["state"] == "UPLOADING":
            rem     = max(0.0, (gs["duration"] - gs["elapsed"]) / 1000.0)
            cdn_col = ORANGE if rem < gs["duration"] / 4000 else YELLOW
            blit_c(screen, f_cdn.render(f"{rem:.1f}s  REMAINING", True, cdn_col), W // 2, CDN_Y)
        elif gs["state"] == "GAMEOVER":
            blit_c(screen, f_cdn.render("TRANSFER COMPLETE", True, GREEN), W // 2, CDN_Y)
        hline(screen, SEP4)

        draw_wave(screen, PAD, WAVE_Y, BAR_W, WAVE_H, wave_t, gs["state"], frac_u, alert=alert)
        hline(screen, SEP5, (0, 35, 25))

        # Terminal
        lines = tsnap()
        lh    = f_term.get_height() + 2
        vis   = max(1, (H - FOOTER_H - TERM_Y) // lh)
        start = max(0, len(lines) - vis)
        for i, ln in enumerate(lines[start:]):
            age    = len(lines) - (start + i) - 1
            bright = max(55, 185 - age * 7)
            if "[ERR]" in ln:
                t_col = (210, int(bright * 0.3), 0)
            elif "[WARN]" in ln:
                t_col = (170, int(bright * 0.7), 0)
            elif any(k in ln for k in ("WIN", "packet", "DEMO:")):
                t_col = (0, min(255, bright + 50), int(bright * 0.35))
            else:
                t_col = (0, bright, int(bright * 0.27))
            screen.blit(f_term.render(ln, True, t_col), (PAD, TERM_Y + i * lh))

        draw_particles(screen)

        if flash_alpha > 0:
            flash_surf.fill((*CYAN, flash_alpha))
            screen.blit(flash_surf, (0, 0))
            flash_alpha = max(0, flash_alpha - 15)

        screen.blit(vignette,  (0, 0))
        screen.blit(scanlines, (0, 0))

        draw_alert_border(screen, now_t, alert)
        draw_footer(screen, f_foot, start_time, now_t, cpu, mem, temp)

        # Intel overlay — drawn last, on top of everything
        if intel_show and intel_entry is not None:
            draw_intel_overlay(screen, f_state, f_label, f_cdn, f_intel_body,
                               intel_entry, intel_timer, INTEL_DUR, now_t, blink)

        present()
        clock.tick(fps)


if __name__ == "__main__":
    main()
