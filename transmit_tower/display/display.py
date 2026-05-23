#!/usr/bin/env python3
"""
Data Run — Transmit Tower Display
Waveshare 10.1" DSI on Raspberry Pi, driven by the ESP32 Transmit Tower over USB serial.

Usage:
    python3 display.py [serial_port]
    python3 display.py /dev/ttyUSB0        # default

Dependencies:
    pip install pygame pyserial

On Raspberry Pi OS with desktop, run from SSH:
    DISPLAY=:0 python3 display.py
On a headless / Wayland image:
    SDL_VIDEODRIVER=wayland python3 display.py

ESC to quit.
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
N_SEGS    = 16   # must match NEO_COUNT in Arduino

# ── Palette ───────────────────────────────────────────────────────────────────
BG        = (  4,   5,  14)
CYAN      = (  0, 180, 255)
CYAN_DIM  = (  0,  20,  40)
CYAN_MID  = (  0,  70, 110)
YELLOW    = (200, 180,   0)
YEL_DIM   = ( 16,  14,   0)
GREEN     = (  0, 210,  70)
WHITE     = (210, 210, 210)
GREY      = ( 55,  60,  75)
GREY_DIM  = ( 20,  22,  30)
TITLE_C   = (  0, 230, 255)
TITLE_DIM = (  0,  80, 110)

# ── Shared game state (written by serial thread, read by render thread) ───────
_state_lock = threading.Lock()
_gs = {"state": "IDLE", "elapsed": 0, "duration": 5000, "score": 0, "winscore": 5}

def get_gs():
    with _state_lock:
        return dict(_gs)

def set_gs(state, elapsed, duration, score, winscore):
    with _state_lock:
        _gs.update(state=state, elapsed=elapsed, duration=duration,
                   score=score, winscore=winscore)

# ── Terminal log (circular, written by both threads) ─────────────────────────
_tlock = threading.Lock()
_tlog  = deque(maxlen=120)

def tpush(line):
    line = line.strip()
    if line:
        with _tlock:
            _tlog.append(line[:200])

def tsnap():
    with _tlock:
        return list(_tlog)

# ── Fake terminal noise templates ─────────────────────────────────────────────
_NOISE_TEMPLATES = [
    "HANDSHAKE 0x{a:04X} -> ACK 0x{b:04X}",
    "BLOCK {n:04d} INTEGRITY CHECK PASS [{c:08X}]",
    "SYNC PKT SEQ:{s:05d} LAT:{l:3d}ms",
    "CH:{ch:02d} SIG:{db:+d}dBm LOCKED",
    "BUF {a:04X}:{b:04X} FLUSH OK {sz:d}B",
    "ROUTE 10.0.{x:d}.{y:d} -> 192.168.{z:d}.1 UP",
    "SECTOR {n:03d} RD OK CRC:{c:04X}",
    "STREAM RATE {r:d} KB/s",
    "RETRY {t:d}/3 ... OK",
    "FRAGMENT {fa:03d}/{fb:03d} REASSEMBLED",
    "NODE {n:02d} PING {l:d}ms TTL:{tt:d}",
    "AES-256 BLK {blk:04X} DECRYPTED",
    "XFER WINDOW {w:d} SEGS",
    "0x{a:04X}: {b:02X} {c:02X} {d:02X} {e:02X}  {f:02X} {g:02X} {h:02X} {i:02X}",
    "CHECKSUM {c:08X} VERIFIED",
    "CARRIER LOCK FREQ {fr:d}.{fm:d} MHz",
    "INJECT PKT {n:04d} QUEUED",
    "ERROR CORRECT PASS BER {ber:.4f}",
]

def _rand_noise():
    t = random.choice(_NOISE_TEMPLATES)
    return t.format(
        a=random.randint(0, 0xFFFF), b=random.randint(0, 0xFF),
        c=random.randint(0, 0xFFFFFFFF), s=random.randint(0, 99999),
        l=random.randint(1, 250), ch=random.randint(0, 15),
        db=random.randint(-95, -35), n=random.randint(0, 999),
        x=random.randint(1, 254), y=random.randint(1, 254),
        z=random.randint(0, 9), r=random.randint(10, 980),
        t=random.randint(1, 3), fa=random.randint(0, 127),
        fb=random.randint(128, 255), tt=random.randint(32, 128),
        blk=random.randint(0, 0xFFFF), w=random.randint(4, 64),
        sz=random.randint(64, 4096), fr=random.randint(433, 915),
        fm=random.randint(0, 9), ber=random.uniform(0, 0.002),
        d=random.randint(0,255), e=random.randint(0,255),
        f=random.randint(0,255), g=random.randint(0,255),
        h=random.randint(0,255), i=random.randint(0,255),
    )

# ── Background threads ────────────────────────────────────────────────────────
def noise_thread():
    while True:
        time.sleep(random.uniform(0.07, 0.28))
        tpush(_rand_noise())

def serial_thread():
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=1)
            tpush(f"PORT {PORT} OPEN OK")
            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("##STATE "):
                    parts = line.split()
                    if len(parts) == 6:
                        try:
                            set_gs(parts[1], int(parts[2]), int(parts[3]),
                                   int(parts[4]), int(parts[5]))
                        except ValueError:
                            pass
                else:
                    tpush(line)
        except serial.SerialException as e:
            tpush(f"SERIAL ERR: {e}")
            time.sleep(3)
        except Exception as e:
            tpush(f"ERR: {e}")
            time.sleep(3)

# ── Demo simulation thread ────────────────────────────────────────────────────
def demo_thread():
    winscore = 5
    score    = 0
    duration = 5000
    while True:
        set_gs("IDLE", 0, duration, score, winscore)
        tpush("DEMO: awaiting card...")
        time.sleep(2)

        set_gs("READY", 0, duration, score, winscore)
        tpush("DEMO: card detected")
        time.sleep(1.5)

        tpush("DEMO: uploading...")
        start = time.time()
        while True:
            elapsed = int((time.time() - start) * 1000)
            if elapsed >= duration:
                break
            set_gs("UPLOADING", elapsed, duration, score, winscore)
            time.sleep(0.05)

        score += 1
        tpush(f"DEMO: packet uploaded! ({score}/{winscore})")
        if score >= winscore:
            set_gs("GAMEOVER", duration, duration, score, winscore)
            tpush("DEMO: *** ATTACKERS WIN ***")
            time.sleep(6)
            score = 0

# ── Drawing helpers ───────────────────────────────────────────────────────────
def segmented_bar(surf, x, y, w, h, frac, fill_col, dim_col, n=N_SEGS, gap=4):
    seg_w = (w - gap * (n - 1)) / n
    filled = int(frac * n + 0.5)
    for i in range(n):
        sx  = int(x + i * (seg_w + gap))
        sw  = max(1, int(seg_w))
        col = fill_col if i < filled else dim_col
        pygame.draw.rect(surf, col, (sx, y, sw, h), border_radius=4)

def hline(surf, y, col=GREY, pad=44, thick=1):
    pygame.draw.line(surf, col, (pad, y), (W - pad, y), thick)

def blit_center(surf, s, cx, y):
    surf.blit(s, (cx - s.get_width() // 2, y))

def blit_right(surf, s, rx, y):
    surf.blit(s, (rx - s.get_width(), y))

def make_scanlines():
    s = pygame.Surface((W, H), pygame.SRCALPHA)
    for ly in range(0, H, 2):
        pygame.draw.line(s, (0, 0, 0, 30), (0, ly), (W, ly))
    return s

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    pygame.init()
    try:
        screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN | pygame.NOFRAME)
    except Exception:
        screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("DATA RUN — TRANSMIT TOWER")
    pygame.mouse.set_visible(False)

    # Fonts — all monospace for the terminal aesthetic
    f_huge   = pygame.font.SysFont("monospace", 88, bold=True)   # NUKETOWN
    f_sub    = pygame.font.SysFont("monospace", 18, bold=True)   # subtitle
    f_state  = pygame.font.SysFont("monospace", 32, bold=True)   # status
    f_label  = pygame.font.SysFont("monospace", 16, bold=True)   # bar labels
    f_count  = pygame.font.SysFont("monospace", 38, bold=True)   # packet count
    f_term   = pygame.font.SysFont("monospace", 13)              # terminal

    scanlines = make_scanlines()
    clock     = pygame.time.Clock()
    PAD       = 48

    # Layout zones (y positions)
    BANNER_Y   = 0
    BANNER_H   = 108
    SEP1_Y     = BANNER_H + 1
    STATUS_Y   = SEP1_Y + 8
    STATUS_H   = 46
    SEP2_Y     = STATUS_Y + STATUS_H + 6
    SCORE_LBL  = SEP2_Y + 10
    SCORE_BAR  = SCORE_LBL + 22
    SEP3_Y     = SCORE_BAR + 52 + 6
    ULOAD_LBL  = SEP3_Y + 10
    ULOAD_BAR  = ULOAD_LBL + 22
    SEP4_Y     = ULOAD_BAR + 52 + 6
    TERM_Y     = SEP4_Y + 6

    BAR_W      = W - PAD * 2
    BAR_H      = 52

    blink      = True
    last_blink = time.time()

    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                pygame.quit(); sys.exit()

        now_t = time.time()
        if now_t - last_blink >= 0.45:
            blink = not blink
            last_blink = now_t

        gs = get_gs()
        screen.fill(BG)

        # ── Banner ────────────────────────────────────────────────────────────
        hline(screen, 1, CYAN, thick=2)
        title_surf = f_huge.render("NUKETOWN", True, TITLE_C)
        blit_center(screen, title_surf, W // 2, 6)
        sub_surf = f_sub.render(
            "[ D A T A   U P L I N K   S T A T I O N ]", True, TITLE_DIM)
        blit_center(screen, sub_surf, W // 2, 98)
        hline(screen, SEP1_Y, CYAN, thick=2)

        # ── Status bar ────────────────────────────────────────────────────────
        state_cfg = {
            "IDLE":      ("AWAITING CARD INPUT",              WHITE),
            "READY":     ("CARD DETECTED — INITIATE UPLOAD",  YELLOW),
            "UPLOADING": ("UPLOADING DATA ...",               CYAN),
            "GAMEOVER":  ("* * *  A T T A C K E R S  W I N  * * *",
                          GREEN if blink else YELLOW),
        }
        slabel, scol = state_cfg.get(gs["state"], ("UNKNOWN STATE", GREY))
        st_surf = f_state.render(slabel, True, scol)
        blit_center(screen, st_surf, W // 2, STATUS_Y + 7)
        hline(screen, SEP2_Y)

        # ── Score bar ─────────────────────────────────────────────────────────
        frac_score = gs["score"] / max(gs["winscore"], 1)

        lbl = f_label.render("PACKETS UPLOADED", True, CYAN_MID)
        screen.blit(lbl, (PAD, SCORE_LBL))
        cnt = f_label.render(f"{gs['score']} / {gs['winscore']}", True, CYAN)
        blit_right(screen, cnt, W - PAD, SCORE_LBL)

        segmented_bar(screen, PAD, SCORE_BAR, BAR_W, BAR_H,
                      frac_score, CYAN, CYAN_DIM)
        hline(screen, SEP3_Y)

        # ── Upload bar ────────────────────────────────────────────────────────
        if gs["state"] == "UPLOADING":
            frac_up   = gs["elapsed"] / max(gs["duration"], 1)
            up_col    = YELLOW
            pct_col   = YELLOW
        elif gs["state"] == "GAMEOVER":
            frac_up   = 1.0
            up_col    = GREEN
            pct_col   = GREEN
        else:
            frac_up   = 0.0
            up_col    = GREY_DIM
            pct_col   = GREY

        ulbl = f_label.render("CURRENT UPLOAD", True, up_col)
        screen.blit(ulbl, (PAD, ULOAD_LBL))
        pct_surf = f_label.render(f"{int(frac_up * 100)}%", True, pct_col)
        blit_right(screen, pct_surf, W - PAD, ULOAD_LBL)

        segmented_bar(screen, PAD, ULOAD_BAR, BAR_W, BAR_H,
                      frac_up, YELLOW, YEL_DIM)
        hline(screen, SEP4_Y)

        # ── Terminal scroll ───────────────────────────────────────────────────
        lines = tsnap()
        lh    = f_term.get_height() + 2
        vis   = max(1, (H - TERM_Y) // lh)
        start = max(0, len(lines) - vis)
        for i, ln in enumerate(lines[start:]):
            age    = len(lines) - (start + i) - 1
            bright = max(70, 200 - age * 7)
            col    = (0, bright, int(bright * 0.30))
            ts     = f_term.render(ln, True, col)
            screen.blit(ts, (PAD, TERM_Y + i * lh))

        # ── Scanlines overlay ─────────────────────────────────────────────────
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
