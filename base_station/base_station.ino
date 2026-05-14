/*
 * Data Run — Base Station
 *
 * The card must be held on the reader for the configured duration before
 * the charge tag is written. Removing it early cancels the operation.
 *
 * Configuration is stored in NVS and edited via the serial menu.
 * Open a serial monitor at 115200 baud and press 'c' within 3 seconds
 * of boot to enter the config menu.
 *
 * Libraries required: MFRC522, Adafruit NeoPixel
 */

#include <SPI.h>
#include <MFRC522.h>
#include <Adafruit_NeoPixel.h>
#include <Preferences.h>

// ── Hardware pins ─────────────────────────────────────────────────────────────
#define SS_PIN      5
#define RST_PIN     22
#define NEO_PIN     4
#define NEO_COUNT   8
#define BUZZER_PIN  15

// ── Shared card constants (must match terminal) ───────────────────────────────
#define CARD_BLOCK  4
static const uint8_t TAG_A[16] = {'D','A','T','A','R','U','N',':','T','E','A','M','_','A',0,0};
static const uint8_t TAG_B[16] = {'D','A','T','A','R','U','N',':','T','E','A','M','_','B',0,0};

// ── NVS config ────────────────────────────────────────────────────────────────
Preferences prefs;

struct Config {
  uint8_t  team;              // 0 = A (red), 1 = B (blue)
  uint32_t chargeDurationMs;  // hold time before write
};

Config cfg;

void loadConfig() {
  prefs.begin("datarun_bs", true);
  cfg.team             = prefs.getUChar("team", 0);
  cfg.chargeDurationMs = prefs.getUInt("charge_ms", 3000);
  prefs.end();
}

void saveConfig() {
  prefs.begin("datarun_bs", false);
  prefs.putUChar("team",      cfg.team);
  prefs.putUInt("charge_ms",  cfg.chargeDurationMs);
  prefs.end();
}

void resetConfig() {
  cfg.team             = 0;
  cfg.chargeDurationMs = 3000;
}

// Runtime accessors so team-dependent values track config changes.
uint8_t           tcR()   { return cfg.team == 0 ? 220 : 0;   }
uint8_t           tcG()   { return 10;                         }
uint8_t           tcB()   { return cfg.team == 0 ? 0   : 220; }
const uint8_t*    myTag() { return cfg.team == 0 ? TAG_A : TAG_B; }

// ── Serial helpers ────────────────────────────────────────────────────────────
void flushSerial() {
  delay(5);
  while (Serial.available()) Serial.read();
}

// Blocks until a character arrives or timeout expires. Returns 0 on timeout.
char waitChar(uint32_t timeoutMs = 30000) {
  uint32_t deadline = millis() + timeoutMs;
  while (millis() < deadline) {
    if (Serial.available()) {
      char c = Serial.read();
      flushSerial();
      return c;
    }
  }
  return 0;
}

// Reads a line of input with local echo. Blocks until '\n' or timeout.
String readLine(uint32_t timeoutMs = 30000) {
  String s;
  uint32_t deadline = millis() + timeoutMs;
  while (millis() < deadline) {
    if (Serial.available()) {
      char c = Serial.read();
      if (c == '\n' || c == '\r') {
        if (s.length() > 0) break;
      } else {
        s += c;
        Serial.print(c);
      }
    }
  }
  Serial.println();
  return s;
}

// ── Config menu ───────────────────────────────────────────────────────────────
void printConfig() {
  Serial.println();
  Serial.println("  Current configuration:");
  Serial.printf( "    Team:             %c (%s)\n",
                 cfg.team == 0 ? 'A' : 'B',
                 cfg.team == 0 ? "red" : "blue");
  Serial.printf( "    Charge duration:  %u ms\n", cfg.chargeDurationMs);
  Serial.println();
}

void configMenu() {
  while (true) {
    Serial.println("========================================");
    Serial.println("   DATA RUN — BASE STATION CONFIG");
    Serial.println("========================================");
    printConfig();
    Serial.println("  [1]  Set team");
    Serial.println("  [2]  Set charge duration");
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
        Serial.print("  Team (A / B): ");
        char t = toupper(waitChar());
        Serial.println(t);
        if      (t == 'A') cfg.team = 0;
        else if (t == 'B') cfg.team = 1;
        else Serial.println("  ! Invalid — enter A or B.");
        break;
      }

      case '2': {
        Serial.print("  Charge duration in ms (500–30000): ");
        uint32_t val = readLine().toInt();
        if (val >= 500 && val <= 30000) cfg.chargeDurationMs = val;
        else Serial.println("  ! Invalid — enter a value between 500 and 30000.");
        break;
      }

      case 'R':
        resetConfig();
        Serial.println("  Defaults restored (not yet saved).");
        break;

      case 'S':
        saveConfig();
        Serial.println("  Saved to NVS.");
        return;

      case 'X':
        Serial.println("  Exiting without saving.");
        return;

      default:
        Serial.println("  ! Unknown option.");
    }
  }
}

// ── Hardware objects ──────────────────────────────────────────────────────────
MFRC522            rfid(SS_PIN, RST_PIN);
MFRC522::MIFARE_Key key;
Adafruit_NeoPixel  strip(NEO_COUNT, NEO_PIN, NEO_GRB + NEO_KHZ800);

// ── Pixel helpers ─────────────────────────────────────────────────────────────
void allPixels(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < NEO_COUNT; i++) strip.setPixelColor(i, r, g, b);
  strip.show();
}

void flash(uint8_t r, uint8_t g, uint8_t b, int n, int ms) {
  for (int i = 0; i < n; i++) {
    allPixels(r, g, b); delay(ms);
    allPixels(0, 0, 0);  delay(ms);
  }
}

// Progress bar in team colour — only redraws when the filled count changes.
void drawProgress(uint32_t elapsed, int &lastFilled) {
  int filled = constrain((int32_t)elapsed * NEO_COUNT / cfg.chargeDurationMs, 0, NEO_COUNT);
  if (filled == lastFilled) return;
  lastFilled = filled;
  for (int i = 0; i < NEO_COUNT; i++)
    strip.setPixelColor(i, i < filled ? strip.Color(tcR(), tcG(), tcB()) : 0);
  strip.show();
}

// ── Idle pulse ────────────────────────────────────────────────────────────────
static uint8_t  pulseV    = 0;
static int8_t   pulseDir  = 1;
static uint32_t lastPulse = 0;

void idleAnimation() {
  uint32_t now = millis();
  if (now - lastPulse < 18) return;
  lastPulse = now;
  pulseV += pulseDir * 3;
  if (pulseV >= 80) { pulseV = 80; pulseDir = -1; }
  if (pulseV == 0)  { pulseDir = 1; }
  allPixels(
    (uint16_t)tcR() * pulseV / 80,
    (uint16_t)tcG() * pulseV / 80,
    (uint16_t)tcB() * pulseV / 80
  );
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);  // allow serial to settle before printing

  loadConfig();

  Serial.println("\n========================================");
  Serial.println("   DATA RUN — BASE STATION");
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

  SPI.begin();
  rfid.PCD_Init();
  for (byte i = 0; i < 6; i++) key.keyByte[i] = 0xFF;

  strip.begin();
  strip.setBrightness(100);
  allPixels(tcR(), tcG(), tcB());
  delay(500);
  allPixels(0, 0, 0);

  Serial.printf("\n  Running — Team %c, charge duration %u ms\n\n",
                cfg.team == 0 ? 'A' : 'B', cfg.chargeDurationMs);
}

// ── Main loop ─────────────────────────────────────────────────────────────────
enum State { IDLE, CHARGING };
State    state         = IDLE;
uint32_t chargeStartMs = 0;
uint32_t lastCheckMs   = 0;
int      lastFilled    = -1;

void loop() {
  uint32_t now = millis();

  // ── IDLE ──────────────────────────────────────────────────────────────────
  if (state == IDLE) {
    if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
      idleAnimation();
      return;
    }

    auto status = rfid.PCD_Authenticate(
      MFRC522::PICC_CMD_MF_AUTH_KEY_A, CARD_BLOCK, &key, &rfid.uid);

    if (status != MFRC522::STATUS_OK) {
      flash(200, 0, 0, 4, 100);
      tone(BUZZER_PIN, 200, 300);
      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
      return;
    }

    Serial.println("Card detected — charging...");
    chargeStartMs = now;
    lastCheckMs   = now;
    lastFilled    = -1;
    state         = CHARGING;
    return;
  }

  // ── CHARGING: verify card presence, then write when timer expires ─────────
  uint32_t elapsed = now - chargeStartMs;

  if (now - lastCheckMs >= 150) {
    lastCheckMs = now;
    byte buf[18]; byte sz = 18;
    if (rfid.MIFARE_Read(CARD_BLOCK, buf, &sz) != MFRC522::STATUS_OK) {
      Serial.println("Card removed — cancelled.");
      allPixels(0, 0, 0);
      flash(180, 0, 0, 3, 80);
      rfid.PCD_StopCrypto1();
      state = IDLE;
      return;
    }
  }

  drawProgress(elapsed, lastFilled);

  if (elapsed < cfg.chargeDurationMs) return;

  auto status = rfid.MIFARE_Write(CARD_BLOCK, myTag(), 16);
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  if (status != MFRC522::STATUS_OK) {
    Serial.println("Write failed.");
    flash(200, 0, 0, 4, 100);
    tone(BUZZER_PIN, 200, 400);
    state = IDLE;
    return;
  }

  Serial.printf("Charged for Team %c.\n", cfg.team == 0 ? 'A' : 'B');
  tone(BUZZER_PIN, 2000, 400);
  allPixels(0, 200, 0);
  delay(1500);
  allPixels(0, 0, 0);
  state = IDLE;
}
