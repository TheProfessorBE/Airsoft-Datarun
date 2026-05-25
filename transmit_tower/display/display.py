#!/usr/bin/env python3
"""
Data Run — Transmit Tower Display
Waveshare 10.1" DSI on Raspberry Pi, driven by the ESP32 Transmit Tower over USB serial.

Usage:
    python3 display.py [port]   (default /dev/ttyUSB0)
    python3 display.py --demo   (simulate without Arduino)
On RPi: WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/$(id -u) SDL_VIDEODRIVER=wayland python3 display.py
"""

import sys, threading, time, random, math
import pygame
import serial
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────────
DEMO_MODE = "--demo" in sys.argv
args      = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT      = args[0] if args else "/dev/ttyUSB0"
BAUD      = 115200
W, H      = 1280, 800
FPS       = 30
N_SEGS    = 16

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

def _rand_noise():
    t = random.choice(_NOISE)
    return t.format(
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
        gs = get_gs()
        if gs["state"] == "UPLOADING" and gs["elapsed"] > gs["duration"] * 0.75:
            delay = random.uniform(0.02, 0.09)
        elif gs["state"] == "UPLOADING":
            delay = random.uniform(0.05, 0.18)
        else:
            delay = random.uniform(0.08, 0.28)
        time.sleep(delay)
        tpush(_rand_noise())

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
    ws, sc, dur = 5, 0, 5000
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
        if sc >= ws:
            set_gs("GAMEOVER", dur, dur, sc, ws); tpush("DEMO: *** ATTACKERS WIN ***")
            time.sleep(6); sc = 0

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
        bx  = cx + int(math.cos(c[0]) * c[1] * r)
        by  = cy + int(math.sin(c[0]) * c[1] * r)
        a   = max(0.0, 1.0 - c[2])
        pygame.draw.circle(surf, (0, int(220 * a), int(90 * a)),
                           (bx, by), max(1, int(4 * a)))
    pygame.draw.circle(surf, (0, 160, 60), (cx, cy), r, 2)

# ── Waveform strip ────────────────────────────────────────────────────────────
def draw_wave(surf, x, y, w, h, t, state, frac):
    mid = y + h // 2
    pygame.draw.line(surf, (0, 25, 18), (x, mid), (x + w, mid), 1)
    if state == "UPLOADING":
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
    try:
        screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN | pygame.NOFRAME)
    except Exception:
        screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("DATA RUN")
    pygame.mouse.set_visible(False)

    f_huge  = pygame.font.SysFont("monospace", 88, bold=True)
    f_sub   = pygame.font.SysFont("monospace", 18, bold=True)
    f_state = pygame.font.SysFont("monospace", 32, bold=True)
    f_label = pygame.font.SysFont("monospace", 16, bold=True)
    f_pct   = pygame.font.SysFont("monospace", 40, bold=True)
    f_cdn   = pygame.font.SysFont("monospace", 22, bold=True)
    f_sys   = pygame.font.SysFont("monospace", 13)
    f_term  = pygame.font.SysFont("monospace", 13)

    circuit   = make_circuit_bg()
    vignette  = make_vignette()
    scanlines = make_scanlines()
    _init_gbuf()
    flash_surf = pygame.Surface((W, H), pygame.SRCALPHA)
    win_surf   = pygame.Surface((W, H), pygame.SRCALPHA)

    clock = pygame.time.Clock()
    PAD   = 48;  BAR_W = W - PAD * 2;  BAR_H = 52

    # Layout
    BANNER_H   = 108;  SEP1 = BANNER_H + 1
    STATUS_Y   = SEP1 + 8;     SEP2      = STATUS_Y + 46 + 6
    SCORE_LBL  = SEP2 + 12;    SCORE_BAR = SCORE_LBL + 22
    SCORE_ICON = SCORE_BAR + BAR_H + 10;  SEP3 = SCORE_ICON + 34 + 8
    ULOAD_LBL  = SEP3 + 12;    ULOAD_BAR = ULOAD_LBL + 22
    PCT_Y      = ULOAD_BAR + BAR_H + 6
    CDN_Y      = PCT_Y + 52
    SEP4       = CDN_Y + 30
    WAVE_Y     = SEP4 + 5;     WAVE_H = 48
    SEP5       = WAVE_Y + WAVE_H + 4;  TERM_Y = SEP5 + 4

    # Radar
    RCX = W - 58;  RCY = 54;  RR = 46

    # System status panel (top-left of banner)
    SYS = ["UPLINK  : ACTIVE", "ENCRYPT : AES-256", "PROTOCOL: SECURE"]

    # Anim state
    blink        = True;  last_blink = time.time()
    flash_alpha  = 0;     prev_score = 0
    scan_phase   = 0.0;   radar_angle = 0.0
    wave_t       = 0.0;   last_t = time.time()

    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: pygame.quit(); sys.exit()
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                pygame.quit(); sys.exit()

        now_t  = time.time();  dt = min(now_t - last_t, 0.1);  last_t = now_t
        if now_t - last_blink >= 0.45:
            blink = not blink;  last_blink = now_t

        gs          = get_gs()
        scan_phase  = (scan_phase + 0.14) % math.tau
        radar_angle = (radar_angle + 0.04) % math.tau
        wave_t      = now_t
        update_radar(radar_angle)
        tick_particles(dt)

        # Score change → particle burst
        if gs["score"] != prev_score:
            flash_alpha = 200
            cx = PAD + int(BAR_W * gs["score"] / max(gs["winscore"], 1))
            spawn_particles(clamp(cx, PAD, W - PAD), SCORE_BAR + BAR_H // 2, CYAN, n=80)
            prev_score = gs["score"]

        # GAMEOVER → continuous particle rain
        if gs["state"] == "GAMEOVER" and len(_particles) < 140:
            col = CYAN if random.random() > 0.4 else GREEN
            spawn_particles(random.randint(PAD, W - PAD),
                            SCORE_BAR + BAR_H // 2, col, n=2)

        # ── Draw ──────────────────────────────────────────────────────────────
        screen.fill(BG)
        screen.blit(circuit, (0, 0))

        # Subtle green tint during GAMEOVER
        if gs["state"] == "GAMEOVER":
            win_surf.fill((0, 180, 50, 12));  screen.blit(win_surf, (0, 0))

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
        scfg = {
            "IDLE":      ("AWAITING CARD INPUT",              WHITE),
            "READY":     ("CARD DETECTED — INITIATE UPLOAD",  YELLOW),
            "UPLOADING": ("UPLOADING DATA ...",               CYAN),
            "GAMEOVER":  ("* * *  A T T A C K E R S  W I N  * * *",
                          GREEN if blink else YELLOW),
        }
        s_label, s_col = scfg.get(gs["state"], ("UNKNOWN STATE", GREY))
        blit_c(screen, f_state.render(s_label, True, s_col), W // 2, STATUS_Y + 6)
        hline(screen, SEP2)

        # Score bar — pulse when idle
        pulse      = 0.88 + 0.12 * abs(math.sin(now_t * 1.8)) if gs["state"] == "IDLE" else 1.0
        score_cyan = tuple(int(c * pulse) for c in CYAN)
        frac_s     = gs["score"] / max(gs["winscore"], 1)
        screen.blit(f_label.render("DATA PACKETS UPLOADED", True, CYAN_MID), (PAD, SCORE_LBL))
        draw_glow_bar(screen, PAD, SCORE_BAR, BAR_W, BAR_H, frac_s, score_cyan, CYAN_DIM)
        draw_brackets(screen, PAD - 5, SCORE_BAR - 5, BAR_W + 10, BAR_H + 10, CYAN_MID)
        draw_packet_icons(screen, W // 2, SCORE_ICON, gs["score"], gs["winscore"])
        hline(screen, SEP3)

        # Upload bar
        if gs["state"] == "UPLOADING":
            frac_u = gs["elapsed"] / max(gs["duration"], 1);  u_col = YELLOW
        elif gs["state"] == "GAMEOVER":
            frac_u = 1.0;  u_col = GREEN
        else:
            frac_u = 0.0;  u_col = GREY

        screen.blit(f_label.render("CURRENT UPLOAD", True, u_col), (PAD, ULOAD_LBL))
        blit_r(screen, f_label.render(f"{int(frac_u * 100)}%", True, u_col), W - PAD, ULOAD_LBL)
        draw_glow_bar(screen, PAD, ULOAD_BAR, BAR_W, BAR_H, frac_u, YELLOW, YEL_DIM)
        b_col = YELLOW if gs["state"] in ("UPLOADING", "GAMEOVER") else (28, 25, 0)
        draw_brackets(screen, PAD - 5, ULOAD_BAR - 5, BAR_W + 10, BAR_H + 10, b_col)

        # Scan cursor on leading edge
        if gs["state"] == "UPLOADING" and frac_u > 0.005:
            sx  = int(PAD + BAR_W * min(frac_u, 0.9995))
            pl  = int(abs(math.sin(scan_phase)) * 100 + 155)
            pygame.draw.line(screen, (255, 255, pl),
                             (sx, ULOAD_BAR - 8), (sx, ULOAD_BAR + BAR_H + 8), 3)

        # Percentage + countdown
        blit_c(screen, f_pct.render(f"{int(frac_u * 100)}%", True, u_col), W // 2, PCT_Y)
        if gs["state"] == "UPLOADING":
            rem     = max(0.0, (gs["duration"] - gs["elapsed"]) / 1000.0)
            cdn_col = ORANGE if rem < gs["duration"] / 4000 else YELLOW
            blit_c(screen, f_cdn.render(f"{rem:.1f}s  REMAINING", True, cdn_col), W // 2, CDN_Y)
        elif gs["state"] == "GAMEOVER":
            blit_c(screen, f_cdn.render("TRANSFER COMPLETE", True, GREEN), W // 2, CDN_Y)
        hline(screen, SEP4)

        # Waveform
        draw_wave(screen, PAD, WAVE_Y, BAR_W, WAVE_H, wave_t, gs["state"], frac_u)
        hline(screen, SEP5, (0, 35, 25))

        # Terminal
        lines = tsnap()
        lh    = f_term.get_height() + 2
        vis   = max(1, (H - TERM_Y) // lh)
        start = max(0, len(lines) - vis)
        for i, ln in enumerate(lines[start:]):
            age    = len(lines) - (start + i) - 1
            bright = max(55, 185 - age * 7)
            if "[ERR]" in ln:
                t_col = (195, int(bright * 0.4), 0)
            elif "[WARN]" in ln:
                t_col = (170, int(bright * 0.7), 0)
            elif any(k in ln for k in ("WIN", "packet", "DEMO:")):
                t_col = (0, min(255, bright + 50), int(bright * 0.35))
            else:
                t_col = (0, bright, int(bright * 0.27))
            screen.blit(f_term.render(ln, True, t_col), (PAD, TERM_Y + i * lh))

        # Particles (above terminal, below overlays)
        draw_particles(screen)

        # Flash on score
        if flash_alpha > 0:
            flash_surf.fill((*CYAN, flash_alpha))
            screen.blit(flash_surf, (0, 0))
            flash_alpha = max(0, flash_alpha - 15)

        screen.blit(vignette,  (0, 0))
        screen.blit(scanlines, (0, 0))
        pygame.display.flip()
        clock.tick(FPS)


if __name__ == "__main__":
    if DEMO_MODE:
        threading.Thread(target=demo_thread, daemon=True).start()
    else:
        threading.Thread(target=serial_thread, daemon=True).start()
    threading.Thread(target=noise_thread, daemon=True).start()
    main()
