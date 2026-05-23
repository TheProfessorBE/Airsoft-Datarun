/*
 * Data Run — Transmit Tower (Sink)
 *
 * Insert a charged card, then hold INITIATE for the full upload duration.
 * Each successful upload scores one data packet. First team to reach
 * win_score packets wins.
 * Releasing INITIATE goes back to "waiting for button."
 * ABORT cancels the upload — the enemy team presses this after eliminating
 * the carrier.
 *
 * LED strip idle: cumulative score bar (dim background, bright fill).
 * LED strip uploading: upload progress bar (replaces score display).
 *
 * NVS config menu: open serial at 115200, press 'c' within 3 s of boot.
 *
 * Pin map
 *   RFID SS:      5   RST: 22   (SPI: MOSI 23, MISO 19, SCK 18)
 *   LED strip:    4   (NeoPixel, NEO_COUNT pixels)
 *   Buzzer:       15
 *   BTN_INITIATE: 13  (active LOW, internal pullup)
 *   BTN_ABORT:    14  (active LOW, internal pullup)
 *
 * Libraries: MFRC522v2, Adafruit NeoPixel
 */

#include <SPI.h>
#include <MFRC522v2.h>
#include <MFRC522DriverSPI.h>
#include <MFRC522DriverPinSimple.h>
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>

// ── Hardware pins ─────────────────────────────────────────────────────────────
#define SS_PIN        5
#define RST_PIN       22
#define NEO_PIN       4
#define NEO_COUNT     16
#define BUZZER_PIN    15
#define BTN_INITIATE  13
#define BTN_ABORT     14

// ── Shared card constants (must match source station) ─────────────────────────
#define CARD_BLOCK  4
static const uint8_t CHARGED_TAG[16] = {
  'D','A','T','A','R','U','N',':','C','H','A','R','G','E','D',0
};
static const uint8_t EMPTY[16] = {0};

// ── NVS config ────────────────────────────────────────────────────────────────
Preferences prefs;

struct Config {
  uint8_t  winScore;
  uint32_t uploadDurationMs;
};
Config cfg;

void loadConfig() {
  prefs.begin("datarun_term", true);
  cfg.winScore         = prefs.getUChar("win_score",  5);
  cfg.uploadDurationMs = prefs.getUInt("upload_ms",  5000);
  prefs.end();
}

void saveConfig() {
  prefs.begin("datarun_term", false);
  prefs.putUChar("win_score",  cfg.winScore);
  prefs.putUInt("upload_ms",   cfg.uploadDurationMs);
  prefs.end();
}

void resetConfig() {
  cfg.winScore         = 5;
  cfg.uploadDurationMs = 5000;
}

// ── Serial helpers ────────────────────────────────────────────────────────────
void flushSerial() {
  delay(5);
  while (Serial.available()) Serial.read();
}

char waitChar(uint32_t timeoutMs = 30000) {
  uint32_t deadline = millis() + timeoutMs;
  while (millis() < deadline) {
    if (Serial.available()) { char c = Serial.read(); flushSerial(); return c; }
  }
  return 0;
}

String readLine(uint32_t timeoutMs = 30000) {
  String s;
  uint32_t deadline = millis() + timeoutMs;
  while (millis() < deadline) {
    if (Serial.available()) {
      char c = Serial.read();
      if (c == '\n' || c == '\r') { if (s.length()) break; }
      else { s += c; Serial.print(c); }
    }
  }
  Serial.println();
  return s;
}

// ── Config menu ───────────────────────────────────────────────────────────────
void printConfig() {
  Serial.println();
  Serial.println("  Current configuration:");
  Serial.printf( "    Win score:        %u\n", cfg.winScore);
  Serial.printf( "    Upload duration:  %u ms\n", cfg.uploadDurationMs);
  Serial.println();
}

void configMenu() {
  while (true) {
    Serial.println("========================================");
    Serial.println("   DATA RUN — TRANSMIT TOWER CONFIG");
    Serial.println("========================================");
    printConfig();
    Serial.println("  [1]  Set win score");
    Serial.println("  [2]  Set upload duration");
    Serial.println("  [R]  Reset to defaults");
    Serial.println("  [S]  Save and continue");
    Serial.println("  [X]  Continue without saving");
    Serial.println("----------------------------------------");
    Serial.print("  Option: ");

    char opt = toupper(waitChar());
    Serial.println(opt);
    Serial.println();

    switch (opt) {
      case '1': {
        Serial.print("  Win score (1-20): ");
        int val = readLine().toInt();
        if (val >= 1 && val <= 20) cfg.winScore = (uint8_t)val;
        else Serial.println("  ! Invalid — enter 1 to 20.");
        break;
      }
      case '2': {
        Serial.print("  Upload duration in ms (5000-60000): ");
        uint32_t val = readLine().toInt();
        if (val >= 5000 && val <= 60000) cfg.uploadDurationMs = val;
        else Serial.println("  ! Invalid — enter 5000 to 60000.");
        break;
      }
      case 'R': resetConfig(); Serial.println("  Defaults restored (not yet saved)."); break;
      case 'S': saveConfig();  Serial.println("  Saved to NVS."); return;
      case 'X': Serial.println("  Exiting without saving."); return;
      default:  Serial.println("  ! Unknown option.");
    }
  }
}

// ── Hardware objects ──────────────────────────────────────────────────────────
MFRC522DriverPinSimple rfidSS(SS_PIN);
MFRC522DriverSPI       rfidDriver{rfidSS, SPI, SPISettings(1000000u, MSBFIRST, SPI_MODE0)};
MFRC522                rfid{rfidDriver};
MFRC522::MIFARE_Key    key;
Adafruit_NeoPixel      strip(NEO_COUNT, NEO_PIN, NEO_GRB + NEO_KHZ800);

uint8_t  score      = 0;
bool     gameOver   = false;
uint32_t lastStatus = 0;

// ── Pixel helpers ─────────────────────────────────────────────────────────────
void allPixels(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < NEO_COUNT; i++) strip.setPixelColor(i, r, g, b);
  strip.show();
}

void flash(uint8_t r, uint8_t g, uint8_t b, int n, int ms) {
  for (int i = 0; i < n; i++) {
    allPixels(r, g, b); delay(ms);
    allPixels(0, 0, 0); delay(ms);
  }
}

// Score bar: bright fill, dim background so the bar is always readable.
void drawScore() {
  int filled = (int)score * NEO_COUNT / cfg.winScore;
  for (int i = 0; i < NEO_COUNT; i++)
    strip.setPixelColor(i, i < filled ? strip.Color(0, 180, 255)
                                      : strip.Color(0, 3, 7));
  strip.show();
}

int lastFilled = -1;

void drawUploadProgress(uint32_t elapsed) {
  int scoreFilled    = (int)score * NEO_COUNT / cfg.winScore;
  int progressFilled = constrain((int32_t)elapsed * NEO_COUNT / (int32_t)cfg.uploadDurationMs,
                                 0, NEO_COUNT);
  if (progressFilled == lastFilled) return;
  lastFilled = progressFilled;
  for (int i = 0; i < NEO_COUNT; i++) {
    if (i < scoreFilled)
      strip.setPixelColor(i, strip.Color(0, 180, 255));   // score: bright cyan
    else if (i < progressFilled)
      strip.setPixelColor(i, strip.Color(200, 180, 0));   // upload progress: yellow
    else
      strip.setPixelColor(i, strip.Color(0, 3, 7));      // background: very dim
  }
  strip.show();
}

// ── Button reading ────────────────────────────────────────────────────────────
bool btnInitiate() { return digitalRead(BTN_INITIATE) == LOW; }
bool btnAbort()    { return digitalRead(BTN_ABORT)    == LOW; }

// ── Win animation ─────────────────────────────────────────────────────────────
void winAnimation() {
  for (int rep = 0; rep < 3; rep++) {
    for (int i = 0; i < NEO_COUNT; i++) {
      strip.setPixelColor(i, strip.Color(0, 180, 255));
      strip.show();
      tone(BUZZER_PIN, 200 + i * 40, 50);
      delay(55);
    }
    allPixels(0, 0, 0);
    delay(160);
  }
  tone(BUZZER_PIN, 523, 140); delay(155);
  tone(BUZZER_PIN, 659, 140); delay(155);
  tone(BUZZER_PIN, 784, 500); delay(520);
  drawScore();
}

// ── State machine ─────────────────────────────────────────────────────────────
enum State { IDLE, CARD_PRESENT, UPLOADING };
State    state       = IDLE;
uint32_t uploadStart = 0;
uint32_t lastCheck   = 0;
uint32_t lastBeep    = 0;

// Emit structured status line for the RPi display.
// Format: ##STATE <state> <elapsed_ms> <duration_ms> <score> <winscore>
void emitStatus(uint32_t elapsed = 0) {
  const char* s = gameOver              ? "GAMEOVER"  :
                  state == UPLOADING    ? "UPLOADING"  :
                  state == CARD_PRESENT ? "READY"      : "IDLE";
  Serial.printf("##STATE %s %u %u %u %u\n",
                s, elapsed, (unsigned)cfg.uploadDurationMs,
                (unsigned)score, (unsigned)cfg.winScore);
  lastStatus = millis();
}

// Halt card and return to IDLE, restoring the score display.
void toIdle() {
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  state     = IDLE;
  lastFilled = -1;
  drawScore();
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);
  loadConfig();

  Serial.println("\n========================================");
  Serial.println("   DATA RUN — TRANSMIT TOWER");
  Serial.println("========================================");
  printConfig();
  Serial.println("  Press 'c' within 3 seconds for config menu...");

  uint32_t deadline = millis() + 3000;
  while (millis() < deadline) {
    if (Serial.available() && tolower(Serial.read()) == 'c') {
      flushSerial();
      configMenu();
      break;
    }
  }

  pinMode(BTN_INITIATE, INPUT_PULLUP);
  pinMode(BTN_ABORT,    INPUT_PULLUP);

  SPI.begin();
  pinMode(RST_PIN, OUTPUT);
  digitalWrite(RST_PIN, HIGH);
  delay(10);
  rfid.PCD_Init();
  for (byte i = 0; i < 6; i++) key.keyByte[i] = 0xFF;

  strip.begin();
  strip.setBrightness(100);
  allPixels(0, 180, 255);
  delay(500);
  allPixels(0, 0, 0);
  delay(200);
  drawScore();

  Serial.printf("\n  Running — first to %u packets wins.\n\n", cfg.winScore);
}

// ── Main loop ─────────────────────────────────────────────────────────────────
void loop() {
  if (gameOver) {
    if (millis() - lastStatus >= 5000) emitStatus(cfg.uploadDurationMs);
    return;
  }

  uint32_t now = millis();

  // Heartbeat for IDLE / READY states so the display stays current
  if (state != UPLOADING && now - lastStatus >= 1000) emitStatus();

  // ── IDLE: show score, wait for a card ─────────────────────────────────────
  if (state == IDLE) {
    if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

    if (rfid.PCD_Authenticate(MFRC522Constants::PICC_CMD_MF_AUTH_KEY_A,
                              CARD_BLOCK, &key, &rfid.uid) != MFRC522Constants::STATUS_OK) {
      flash(200, 0, 0, 3, 100);
      tone(BUZZER_PIN, 200, 300);
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
      return;
    }

    byte buf[18]; byte sz = 18;
    if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522Constants::STATUS_OK) {
      flash(200, 0, 0, 3, 100);
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
      return;
    }

    if (memcmp(buf, CHARGED_TAG, 16) != 0) {
      Serial.println("Card not charged.");
      flash(220, 80, 0, 4, 100);
      tone(BUZZER_PIN, 350, 120); delay(140);
      tone(BUZZER_PIN, 200, 300);
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
      drawScore();
      return;
    }

    Serial.println("Charged card inserted — press INITIATE to upload.");
    tone(BUZZER_PIN, 523, 70); delay(80);
    tone(BUZZER_PIN, 784, 70); delay(80);
    tone(BUZZER_PIN, 1047, 120);
    lastCheck = now;
    state = CARD_PRESENT;
    emitStatus();
    return;
  }

  // ── CARD_PRESENT: card ready, waiting for INITIATE hold ───────────────────
  if (state == CARD_PRESENT) {
    if (btnAbort()) {
      Serial.println("Aborted.");
      flash(200, 50, 0, 2, 80);
      tone(BUZZER_PIN, 200, 200);
      toIdle();
      emitStatus();
      return;
    }

    // Verify card still in slot
    if (now - lastCheck >= 200) {
      lastCheck = now;
      byte buf[18]; byte sz = 18;
      if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522Constants::STATUS_OK) {
        Serial.println("Card removed.");
        rfid.PCD_StopCrypto1();
        state = IDLE;
        drawScore();
        emitStatus();
        return;
      }
    }

    if (btnInitiate()) {
      Serial.println("Uploading...");
      uploadStart = now;
      lastFilled  = -1;
      lastBeep    = now;
      state       = UPLOADING;
      emitStatus();
    }
    return;
  }

  // ── UPLOADING: INITIATE must stay held for the full duration ──────────────
  if (state == UPLOADING) {
    if (btnAbort()) {
      Serial.println("Upload aborted.");
      flash(200, 50, 0, 3, 80);
      tone(BUZZER_PIN, 200, 300);
      lastCheck = now;
      lastFilled = -1;
      state     = CARD_PRESENT;
      drawScore();
      emitStatus();
      return;
    }

    uint32_t elapsed = now - uploadStart;
    drawUploadProgress(elapsed);
    if (now - lastStatus >= 100) emitStatus(elapsed);

    // Beep rate increases in the final quarter
    uint32_t interval = (elapsed > cfg.uploadDurationMs * 3 / 4) ? 250 :
                        (elapsed > cfg.uploadDurationMs / 2)     ? 500 : 1000;
    if (now - lastBeep >= interval) {
      lastBeep = now;
      tone(BUZZER_PIN, 1200, 50);
    }

    // Verify card still in slot
    if (now - lastCheck >= 150) {
      lastCheck = now;
      byte buf[18]; byte sz = 18;
      if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522Constants::STATUS_OK) {
        Serial.println("Card removed during upload.");
        rfid.PCD_StopCrypto1();
        state = IDLE;
        drawScore();
        emitStatus();
        return;
      }
    }

    if (elapsed < cfg.uploadDurationMs) return;

    // Wipe card before scoring — a failed wipe must not award a phantom point
    auto status = rfid.MIFARE_Write(CARD_BLOCK, (byte*)EMPTY, 16);
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    if (status != MFRC522Constants::STATUS_OK) {
      Serial.println("Write failed.");
      flash(200, 0, 0, 4, 100);
      tone(BUZZER_PIN, 200, 400);
      state = IDLE;
      drawScore();
      emitStatus();
      return;
    }

    score++;
    Serial.printf("Packet uploaded! (%u / %u)\n", score, cfg.winScore);

    tone(BUZZER_PIN, 1800, 150); delay(170);
    tone(BUZZER_PIN, 2200, 150); delay(170);
    tone(BUZZER_PIN, 2600, 300); delay(320);

    if (score >= cfg.winScore) {
      Serial.println("\n  *** ATTACKERS WIN! ***\n");
      gameOver = true;
      emitStatus(cfg.uploadDurationMs);
      allPixels(0, 180, 255);
      delay(300);
      winAnimation();
      return;
    }

    drawScore();
    // Flash new score bar to confirm the point
    for (int i = 0; i < 3; i++) {
      allPixels(0, 180, 255); delay(100);
      drawScore();             delay(100);
    }
    state = IDLE;
    emitStatus();
  }
}
