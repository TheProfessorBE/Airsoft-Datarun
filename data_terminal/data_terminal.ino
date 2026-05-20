/*
 * Data Run — Data Terminal (Source)
 *
 * Insert an empty card, then hold INITIATE for the full charge duration.
 * Releasing INITIATE early goes back to "waiting for button."
 * ABORT cancels entirely — the enemy team presses this after eliminating
 * the carrier.
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

// ── Shared card constants (must match terminal) ───────────────────────────────
#define CARD_BLOCK  4
static const uint8_t CHARGED_TAG[16] = {
  'D','A','T','A','R','U','N',':','C','H','A','R','G','E','D',0
};

// ── NVS config ────────────────────────────────────────────────────────────────
Preferences prefs;

struct Config {
  uint32_t chargeDurationMs;
};
Config cfg;

void loadConfig() {
  prefs.begin("datarun_src", true);
  cfg.chargeDurationMs = prefs.getUInt("charge_ms", 5000);
  prefs.end();
}

void saveConfig() {
  prefs.begin("datarun_src", false);
  prefs.putUInt("charge_ms", cfg.chargeDurationMs);
  prefs.end();
}

void resetConfig() {
  cfg.chargeDurationMs = 5000;
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
  Serial.printf( "    Charge duration:  %u ms\n", cfg.chargeDurationMs);
  Serial.println();
}

void configMenu() {
  while (true) {
    Serial.println("========================================");
    Serial.println("   DATA RUN — DATA TERMINAL CONFIG");
    Serial.println("========================================");
    printConfig();
    Serial.println("  [1]  Set charge duration");
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
        Serial.print("  Charge duration in ms (5000-60000): ");
        uint32_t val = readLine().toInt();
        if (val >= 5000 && val <= 60000) cfg.chargeDurationMs = val;
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

int lastFilled = -1;

void drawProgress(uint32_t elapsed) {
  int filled = constrain((int32_t)elapsed * NEO_COUNT / (int32_t)cfg.chargeDurationMs, 0, NEO_COUNT);
  if (filled == lastFilled) return;
  lastFilled = filled;
  for (int i = 0; i < NEO_COUNT; i++)
    strip.setPixelColor(i, i < filled ? strip.Color(0, 200, 50) : 0);
  strip.show();
}

// ── Idle pulse animation ──────────────────────────────────────────────────────
static uint8_t  pulseV   = 0;
static int8_t   pulseDir = 1;
static uint32_t lastPulse = 0;

void idleAnimation() {
  uint32_t now = millis();
  if (now - lastPulse < 18) return;
  lastPulse = now;
  pulseV += pulseDir * 3;
  if (pulseV >= 80) { pulseV = 80; pulseDir = -1; }
  if (pulseV == 0)  { pulseDir =  1; }
  allPixels(0, (uint16_t)200 * pulseV / 80, (uint16_t)50 * pulseV / 80);
}

// ── Button reading ────────────────────────────────────────────────────────────
bool btnInitiate() { return digitalRead(BTN_INITIATE) == LOW; }
bool btnAbort()    { return digitalRead(BTN_ABORT)    == LOW; }

// ── State machine ─────────────────────────────────────────────────────────────
enum State { IDLE, CARD_PRESENT, CHARGING };
State    state       = IDLE;
uint32_t chargeStart = 0;
uint32_t lastCheck   = 0;
uint32_t lastBeep    = 0;

// Halt card and return to IDLE.
void toIdle() {
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  state     = IDLE;
  lastFilled = -1;
}

// Show single-LED "waiting for button" indicator.
void showReady() {
  for (int i = 0; i < NEO_COUNT; i++)
    strip.setPixelColor(i, i == 0 ? strip.Color(0, 200, 50) : 0);
  strip.show();
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);
  loadConfig();

  Serial.println("\n========================================");
  Serial.println("   DATA RUN — DATA TERMINAL");
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
  allPixels(0, 200, 50);
  delay(500);
  allPixels(0, 0, 0);

  Serial.printf("\n  Running — charge duration %u ms\n\n", cfg.chargeDurationMs);
}

// ── Main loop ─────────────────────────────────────────────────────────────────
void loop() {
  uint32_t now = millis();

  // ── IDLE: wait for a card ──────────────────────────────────────────────────
  if (state == IDLE) {
    if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
      idleAnimation();
      return;
    }

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


    Serial.println("Card inserted — press INITIATE to charge.");
    tone(BUZZER_PIN, 523, 70); delay(80);
    tone(BUZZER_PIN, 784, 70); delay(80);
    tone(BUZZER_PIN, 1047, 120);
    showReady();
    lastCheck = now;
    state = CARD_PRESENT;
    return;
  }

  // ── CARD_PRESENT: card is ready, waiting for INITIATE hold ────────────────
  if (state == CARD_PRESENT) {
    if (btnAbort()) {
      Serial.println("Aborted.");
      flash(200, 50, 0, 2, 80);
      tone(BUZZER_PIN, 200, 200);
      toIdle();
      return;
    }

    // Verify card still in slot
    if (now - lastCheck >= 200) {
      lastCheck = now;
      byte buf[18]; byte sz = 18;
      if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522Constants::STATUS_OK) {
        Serial.println("Card removed.");
        allPixels(0, 0, 0);
        rfid.PCD_StopCrypto1();
        state = IDLE;
        return;
      }
    }

    if (btnInitiate()) {
      Serial.println("Charging...");
      chargeStart = now;
      lastFilled  = -1;
      lastBeep    = now;
      state       = CHARGING;
    }
    return;
  }

  // ── CHARGING: INITIATE must stay held for the full duration ───────────────
  if (state == CHARGING) {
    if (btnAbort()) {
      Serial.println("Aborted.");
      allPixels(0, 0, 0);
      flash(200, 50, 0, 3, 80);
      tone(BUZZER_PIN, 200, 300);
      toIdle();
      return;
    }

    uint32_t elapsed = now - chargeStart;
    drawProgress(elapsed);

    // Beep rate increases in the final quarter
    uint32_t interval = (elapsed > cfg.chargeDurationMs * 3 / 4) ? 250 :
                        (elapsed > cfg.chargeDurationMs / 2)     ? 500 : 1000;
    if (now - lastBeep >= interval) {
      lastBeep = now;
      tone(BUZZER_PIN, 1200, 50);
    }

    // Verify card is still present
    if (now - lastCheck >= 150) {
      lastCheck = now;
      byte buf[18]; byte sz = 18;
      if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522Constants::STATUS_OK) {
        Serial.println("Card removed during charge.");
        allPixels(0, 0, 0);
        rfid.PCD_StopCrypto1();
        state = IDLE;
        return;
      }
    }

    if (elapsed < cfg.chargeDurationMs) return;

    // Write charged tag to card
    auto status = rfid.MIFARE_Write(CARD_BLOCK, (byte*)CHARGED_TAG, 16);
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    if (status != MFRC522Constants::STATUS_OK) {
      Serial.println("Write failed.");
      flash(200, 0, 0, 4, 100);
      tone(BUZZER_PIN, 200, 400);
      state = IDLE;
      return;
    }

    Serial.println("Card charged — go!");
    tone(BUZZER_PIN, 1800, 150); delay(170);
    tone(BUZZER_PIN, 2200, 150); delay(170);
    tone(BUZZER_PIN, 2600, 300);
    allPixels(0, 200, 50);
    delay(1200);
    allPixels(0, 0, 0);
    state = IDLE;
  }
}
