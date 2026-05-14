/*
 * Data Run — Central Terminal
 *
 * Reads the charge tag from a data core, awards the point to the matching
 * team, then wipes the block so the card is empty again.
 * Scores reset on power cycle (no EEPROM persistence).
 *
 * Configuration is stored in NVS and edited via the serial menu.
 * Open a serial monitor at 115200 baud and press 'c' within 3 seconds
 * of boot to enter the config menu.
 *
 * NeoPixel layout — two independent 16-pixel strips, one per team:
 *   Strip A (Team A, red):  NEO_PIN_A
 *   Strip B (Team B, blue): NEO_PIN_B
 *   Each strip is a progress bar: score × NEO_COUNT / win_score pixels lit,
 *   remaining pixels shown dim so the strip is always readable.
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
#define NEO_PIN_A   4
#define NEO_PIN_B   16
#define NEO_COUNT   16
#define BUZZER_PIN  15

// ── Shared card constants (must match base_station) ───────────────────────────
#define CARD_BLOCK  4
static const uint8_t TAG_A[16] = {'D','A','T','A','R','U','N',':','T','E','A','M','_','A',0,0};
static const uint8_t TAG_B[16] = {'D','A','T','A','R','U','N',':','T','E','A','M','_','B',0,0};
static const uint8_t EMPTY[16] = {0};

// ── Colours ───────────────────────────────────────────────────────────────────
#define A_R 220
#define A_G 10
#define A_B 0
#define B_R 0
#define B_G 10
#define B_B 220
#define A_R_DIM 18
#define A_G_DIM 1
#define A_B_DIM 0
#define B_R_DIM 0
#define B_G_DIM 1
#define B_B_DIM 18

// ── NVS config ────────────────────────────────────────────────────────────────
Preferences prefs;

struct Config {
  uint8_t winScore;
};

Config cfg;

void loadConfig() {
  prefs.begin("datarun_term", true);
  cfg.winScore = prefs.getUChar("win_score", 5);
  prefs.end();
}

void saveConfig() {
  prefs.begin("datarun_term", false);
  prefs.putUChar("win_score", cfg.winScore);
  prefs.end();
}

void resetConfig() {
  cfg.winScore = 5;
}

// ── Serial helpers ────────────────────────────────────────────────────────────
void flushSerial() {
  delay(5);
  while (Serial.available()) Serial.read();
}

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
  Serial.printf( "    Win score: %u\n", cfg.winScore);
  Serial.println();
}

void configMenu() {
  while (true) {
    Serial.println("========================================");
    Serial.println("   DATA RUN — TERMINAL CONFIG");
    Serial.println("========================================");
    printConfig();
    Serial.println("  [1]  Set win score");
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
        Serial.print("  Win score (1–20): ");
        int val = readLine().toInt();
        if (val >= 1 && val <= 20) cfg.winScore = (uint8_t)val;
        else Serial.println("  ! Invalid — enter a value between 1 and 20.");
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
Adafruit_NeoPixel  stripA(NEO_COUNT, NEO_PIN_A, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel  stripB(NEO_COUNT, NEO_PIN_B, NEO_GRB + NEO_KHZ800);

uint8_t scores[2] = {0, 0};
bool    gameOver  = false;

// ── Display ───────────────────────────────────────────────────────────────────
void drawStrip(Adafruit_NeoPixel &s, uint8_t score,
               uint8_t r, uint8_t g, uint8_t b,
               uint8_t rd, uint8_t gd, uint8_t bd) {
  int filled = (int)score * NEO_COUNT / cfg.winScore;
  for (int i = 0; i < NEO_COUNT; i++)
    s.setPixelColor(i, i < filled ? s.Color(r, g, b) : s.Color(rd, gd, bd));
  s.show();
}

void drawScores() {
  drawStrip(stripA, scores[0], A_R, A_G, A_B, A_R_DIM, A_G_DIM, A_B_DIM);
  drawStrip(stripB, scores[1], B_R, B_G, B_B, B_R_DIM, B_G_DIM, B_B_DIM);
}

// ── Strip helpers ─────────────────────────────────────────────────────────────
void fillStrip(Adafruit_NeoPixel &s, uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < NEO_COUNT; i++) s.setPixelColor(i, r, g, b);
  s.show();
}

void clearStrip(Adafruit_NeoPixel &s) { fillStrip(s, 0, 0, 0); }

// ── Feedback animations ───────────────────────────────────────────────────────
void scoreFlash(int team) {
  Adafruit_NeoPixel &s = team == 0 ? stripA : stripB;
  uint8_t r = team == 0 ? A_R : B_R;
  uint8_t g = team == 0 ? A_G : B_G;
  uint8_t b = team == 0 ? A_B : B_B;

  for (int i = 0; i < 3; i++) {
    fillStrip(s, r, g, b);
    tone(BUZZER_PIN, 500 + i * 150, 90);
    delay(110);
    clearStrip(s);
    delay(90);
  }
  drawScores();
}

void winAnimation(int team) {
  Adafruit_NeoPixel &s = team == 0 ? stripA : stripB;
  uint8_t r = team == 0 ? A_R : B_R;
  uint8_t g = team == 0 ? A_G : B_G;
  uint8_t b = team == 0 ? A_B : B_B;

  for (int rep = 0; rep < 3; rep++) {
    for (int i = 0; i < NEO_COUNT; i++) {
      s.setPixelColor(i, r, g, b);
      s.show();
      tone(BUZZER_PIN, 200 + i * 40, 50);
      delay(55);
    }
    clearStrip(s);
    delay(160);
  }

  tone(BUZZER_PIN, 523, 140); delay(155);
  tone(BUZZER_PIN, 659, 140); delay(155);
  tone(BUZZER_PIN, 784, 500); delay(520);

  drawScores();
}

void emptyFlash() {
  for (int i = 0; i < 4; i++) {
    fillStrip(stripA, 220, 80, 0); fillStrip(stripB, 220, 80, 0);
    delay(100);
    clearStrip(stripA);            clearStrip(stripB);
    delay(100);
  }
  tone(BUZZER_PIN, 280, 350);
  drawScores();
}

void errorFlash() {
  for (int i = 0; i < 4; i++) {
    fillStrip(stripA, 200, 0, 0); fillStrip(stripB, 200, 0, 0);
    delay(100);
    clearStrip(stripA);           clearStrip(stripB);
    delay(100);
  }
  tone(BUZZER_PIN, 200, 400);
  drawScores();
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);

  loadConfig();

  Serial.println("\n========================================");
  Serial.println("   DATA RUN — TERMINAL");
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

  stripA.begin(); stripA.setBrightness(100);
  stripB.begin(); stripB.setBrightness(100);

  fillStrip(stripA, A_R, A_G, A_B);
  fillStrip(stripB, B_R, B_G, B_B);
  delay(500);
  clearStrip(stripA); clearStrip(stripB);
  delay(200);

  drawScores();

  Serial.printf("\n  Running — first to %u points wins.\n\n", cfg.winScore);
}

// ── Main loop ─────────────────────────────────────────────────────────────────
void loop() {
  if (gameOver) return;

  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

  auto status = rfid.PCD_Authenticate(
    MFRC522::PICC_CMD_MF_AUTH_KEY_A, CARD_BLOCK, &key, &rfid.uid);

  if (status != MFRC522::STATUS_OK) {
    errorFlash();
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }

  byte buf[18]; byte sz = 18;
  status = rfid.MIFARE_Read(CARD_BLOCK, buf, &sz);

  if (status != MFRC522::STATUS_OK) {
    errorFlash();
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }

  int teamIdx = -1;
  if      (memcmp(buf, TAG_A, 16) == 0) teamIdx = 0;
  else if (memcmp(buf, TAG_B, 16) == 0) teamIdx = 1;

  if (teamIdx < 0) {
    if (memcmp(buf, EMPTY, 16) == 0) emptyFlash();
    else                             errorFlash();
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }

  // Wipe before scoring — a failed write must not award a phantom point
  status = rfid.MIFARE_Write(CARD_BLOCK, EMPTY, 16);
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  if (status != MFRC522::STATUS_OK) {
    errorFlash();
    return;
  }

  scores[teamIdx]++;
  Serial.printf("  Team %c scores! (%u / %u)\n",
                teamIdx == 0 ? 'A' : 'B', scores[teamIdx], cfg.winScore);

  scoreFlash(teamIdx);

  if (scores[teamIdx] >= cfg.winScore) {
    Serial.printf("\n  *** TEAM %c WINS! ***\n\n", teamIdx == 0 ? 'A' : 'B');
    gameOver = true;
    winAnimation(teamIdx);
  }
}
