/*
 * agent-pulse - 4x MAX7219 LED matrix status display for coding agents
 *
 * Wiring (Arduino Nano, hardware SPI):
 *   MAX7219 VCC  -> 5V
 *   MAX7219 GND  -> GND
 *   MAX7219 DIN  -> D11 (MOSI)
 *   MAX7219 CS   -> D10
 *   MAX7219 CLK  -> D13 (SCK)
 *
 * Serial protocol, 115200 baud, newline terminated:
 *   #L            -> working: march the border until the next command
 *   #Q            -> waiting on you: cycle I / NEED / REPLY until answered
 *   #X            -> interrupted: flash "ABORT" twice, hold it, then dark
 *   #N<a>,<b>,<c> -> finished: blink DONE 3x, then the first field static for
 *                    HOLD_STATIC_MS and every later field scrolled once, then
 *                    go dark. Fields are optional, so "#N" shows none.
 *                    Only the static field is bound by the 5-character width;
 *                    the scrolled ones can carry a label.
 *   any text      -> scroll once, then back to idle
 *   #B<0-15>      -> set brightness (saved to EEPROM)
 *   #S<10-150>    -> set scroll frame delay in ms (lower is faster)
 *   #T            -> self test: light every LED for 1s
 *   #C            -> clear immediately
 *
 * The board keeps its own time, so the host only sends #L when a turn starts
 * and #N when it ends. Everything else - the report timing and the idle
 * heartbeat every AWAIT_MS - runs here.
 *
 * Note on orientation: MD_MAX72XX buffer column 0 sits at the right-hand end
 * of the panel for this hardware type, so a getColumn() dump reads mirrored
 * even when the display is correct. Rows are not affected: row 0 is the top,
 * which is why the HEART table is mapped to COLUMNS - 1 - p when drawn.
 *
 * If text shows up mirrored or split across the wrong modules, change
 * HARDWARE_TYPE below (FC16_HW / PAROLA_HW / GENERIC_HW / ICSTATION_HW).
 */

#include <MD_Parola.h>
#include <MD_MAX72xx.h>
#include <SPI.h>
#include <EEPROM.h>

#define HARDWARE_TYPE MD_MAX72XX::FC16_HW
#define MAX_DEVICES 4
#define CS_PIN 10
#define COLUMNS (MAX_DEVICES * 8)

// 4 modules at high brightness can pull more than the Nano's USB rail likes,
// so default low. Raise with #B if your supply can take it.
#define DEFAULT_BRIGHTNESS 1
#define DEFAULT_SPEED 30

#define BLINK_LABEL "DONE"
#define BLINK_COUNT 3
#define BLINK_ON_MS 300
#define BLINK_OFF_MS 250

// ABORT rather than CANCEL: every status word on this panel is upper case
// (DONE, I NEED REPLY) and CANCEL needs 35 columns, so it cannot be centred on
// a 32-column panel. ABORT needs 29 and keeps the convention intact.
#define CANCEL_LABEL "ABORT"
#define CANCEL_BLINKS 2
#define CANCEL_ON_MS 170
#define CANCEL_OFF_MS 130
#define CANCEL_HOLD_MS 3000

// After DONE, each field of the #N payload gets its own screen, then dark.
// The first is held still; the rest scroll past once each.
#define HOLD_SLOTS 4
#define HOLD_STATIC_MS 15000
// While nothing is happening the panel is dark, apart from one ECG beat this
// often, as a sign of life.
#define AWAIT_MS (10UL * 1000UL)

// Brightness survives a reset, which matters because opening the serial port
// resets the board on every send - without this, #B would only last until the
// next command arrived.
#define EE_MAGIC_ADDR 0
#define EE_BRIGHT_ADDR 2
#define EE_MAGIC 0xC2

// Claude is waiting on an answer. Each word is drawn static and centred, so
// each must fit 32 columns: "ANSWERS" needs 41 and cannot be used here.
#define ASK_DWELL_MS 900
#define ASK_WORDS 3

// The 5x7 font plus spacing gives 6 columns per character, so 4 modules
// (32 columns) fit 5 static characters. Longer hold text will be clipped.
#define MSG_MAX 160
#define HOLD_MAX 15

MD_Parola display = MD_Parola(HARDWARE_TYPE, CS_PIN, MAX_DEVICES);

enum Phase { P_IDLE, P_SCROLL, P_BLINK, P_HOLD, P_LOAD, P_ASK, P_PULSE };

char message[MSG_MAX + 1];
char holdText[HOLD_SLOTS][HOLD_MAX + 1];
uint8_t holdCount = 0;
uint8_t holdStep = 0;
char inbuf[MSG_MAX + 1];
uint8_t inlen = 0;
uint8_t scrollSpeed = DEFAULT_SPEED;
Phase phase = P_IDLE;
uint8_t blinkStep = 0;       // even = lit, odd = dark
uint8_t blinkSteps = 0;      // how many on/off steps this flash runs for
uint16_t blinkOnMs = BLINK_ON_MS;
uint16_t blinkOffMs = BLINK_OFF_MS;
uint16_t staticMs = HOLD_STATIC_MS;
unsigned long phaseAt = 0;   // start of the current blink step / hold / frame
unsigned long idleAt = 0;    // when the display last went idle
uint16_t frameNo = 0;
uint8_t askStep = 0;

void goIdle() {
  display.displaySuspend(false);
  display.displayShutdown(false);
  display.displayClear();
  idleAt = millis();
  phase = P_IDLE;
}

// Draw one frame of centred text and freeze it, so the panel holds the image
// without further animation calls.
void setStatic(const char *text) {
  strncpy(message, text, MSG_MAX);
  message[MSG_MAX] = '\0';
  display.displaySuspend(false);
  display.displayShutdown(false);
  display.displayClear();
  display.displayText(message, PA_CENTER, 0, 0, PA_PRINT, PA_NO_EFFECT);
  for (uint8_t i = 0; i < 20 && !display.displayAnimate(); i++) {
    ;  // PA_PRINT settles in a couple of calls; bounded so it cannot hang
  }
  display.displaySuspend(true);
}

void startScroll(const char *text) {
  strncpy(message, text, MSG_MAX);
  message[MSG_MAX] = '\0';
  Serial.print(F("SCROLL "));
  Serial.println(message);
  display.displaySuspend(false);
  display.displayShutdown(false);
  display.displayClear();
  display.displayText(message, PA_LEFT, scrollSpeed, 0, PA_SCROLL_LEFT, PA_SCROLL_LEFT);
  phase = P_SCROLL;
}

// Flash a label, then hand over to the hold sequence.
void startBlink(const char *label, uint8_t blinks, uint16_t onMs,
                uint16_t offMs, uint16_t hold) {
  blinkSteps = blinks * 2;
  blinkOnMs = onMs;
  blinkOffMs = offMs;
  staticMs = hold;
  setStatic(label);
  blinkStep = 0;
  phaseAt = millis();
  phase = P_BLINK;
}

// Split the comma-separated payload into one string per screen.
void startNotify(const char *text) {
  holdCount = 0;
  const char *p = text;
  while (*p && holdCount < HOLD_SLOTS) {
    uint8_t n = 0;
    while (*p && *p != ',' && n < HOLD_MAX) holdText[holdCount][n++] = *p++;
    holdText[holdCount][n] = '\0';
    while (*p && *p != ',') p++;        // drop anything past HOLD_MAX
    if (*p == ',') p++;
    if (n) holdCount++;
  }
  Serial.print(F("NOTIFY "));
  Serial.println(holdCount);
  startBlink(BLINK_LABEL, BLINK_COUNT, BLINK_ON_MS, BLINK_OFF_MS,
             HOLD_STATIC_MS);
}

// Interrupted: the label is both what flashes and what is then held.
void startCancel() {
  Serial.println(F("CANCEL"));
  strncpy(holdText[0], CANCEL_LABEL, HOLD_MAX);
  holdText[0][HOLD_MAX] = '\0';
  holdCount = 1;
  startBlink(CANCEL_LABEL, CANCEL_BLINKS, CANCEL_ON_MS, CANCEL_OFF_MS,
             CANCEL_HOLD_MS);
}

void showHoldStep() {
  phaseAt = millis();
  if (holdStep == 0) {
    setStatic(holdText[0]);
  } else {
    display.displaySuspend(false);
    display.displayShutdown(false);
    display.displayClear();
    display.displayText(holdText[holdStep], PA_LEFT, scrollSpeed, 0,
                        PA_SCROLL_LEFT, PA_SCROLL_LEFT);
  }
  Serial.print(F("HOLD "));
  Serial.println(holdText[holdStep]);
}

void enterHold() {
  if (holdCount == 0) {
    Serial.println(F("DONE"));
    goIdle();
    return;
  }
  holdStep = 0;
  phase = P_HOLD;
  showHoldStep();
}

// --- animations -------------------------------------------------------------
// Two of them, each with one job: the border marches while Claude works, and
// the ECG beats once every AWAIT_MS while the panel is otherwise idle. Note
// the MAX7219 multiplexes one row at a time, so average current tracks
// lit-pixel count divided by eight.

#define LOAD_FRAME_MS 50
#define HEART_FRAME_MS 25
#define HEART_CYCLE (2 * COLUMNS)

// Dashes marching around the panel edge. The perimeter is walked as one path
// of 2*COLUMNS + 12 positions, so the dashes turn the corners cleanly.
void drawBorder(MD_MAX72XX *mx) {
  uint8_t buf[COLUMNS];
  memset(buf, 0, COLUMNS);
  const uint8_t PERIM = 2 * COLUMNS + 12;
  for (uint8_t p = 0; p < PERIM; p++) {
    if ((uint8_t)((p + frameNo) % 12) >= 4) continue;
    uint8_t c, r;
    if (p < COLUMNS)                 { c = p;                      r = 0; }
    else if (p < COLUMNS + 6)        { c = COLUMNS - 1;            r = p - COLUMNS + 1; }
    else if (p < 2 * COLUMNS + 6)    { c = (2 * COLUMNS + 5) - p;  r = 7; }
    else                             { c = 0;                      r = 6 - (p - (2 * COLUMNS + 6)); }
    buf[c] |= (uint8_t)(1 << r);
  }
  for (uint8_t c = 0; c < COLUMNS; c++) mx->setColumn(c, buf[c]);
}

// An ECG beat. The waveform stays at a fixed position and the cycle has two
// halves: the trace draws in from the left until it is complete, then it is
// erased from the left until the panel is empty again.
// The table is in panel order, left to right, and the draw maps it onto the
// buffer, whose column 0 is the panel's right-hand end.
static const uint8_t HEART[COLUMNS] = {
  4, 4, 4, 4, 4, 4, 4, 4, 4, 4,          // baseline
  3, 2, 1, 0,                            // upstroke to the peak
  1, 2, 3, 4, 5, 6, 7,                   // straight down through it
  6, 5, 4,                                // recover to the baseline
  4, 4, 4, 4, 4, 4, 4, 4                 // baseline
};
void drawHeart(MD_MAX72XX *mx) {
  uint8_t t = frameNo % HEART_CYCLE;
  uint8_t from, to;
  if (t < COLUMNS) {
    from = 0;                    // drawing in: 1 column, then 2, ... then all
    to = t;
  } else {
    from = t - (COLUMNS - 1);    // rubbing out from the left, back to empty
    to = COLUMNS - 1;
  }
  if (from > to) return;         // the one blank frame that ends the cycle
  for (uint8_t p = from; p <= to; p++) {
    mx->setColumn(COLUMNS - 1 - p, (uint8_t)(1 << HEART[p]));
  }
}


// Both animations render the same way, and the phase says which is running.
void renderFrame() {
  MD_MAX72XX *mx = display.getGraphicObject();
  mx->control(MD_MAX72XX::UPDATE, MD_MAX72XX::OFF);
  mx->clear();
  if (phase == P_LOAD) drawBorder(mx);
  else drawHeart(mx);
  mx->control(MD_MAX72XX::UPDATE, MD_MAX72XX::ON);
}

void startLoad() {
  Serial.println(F("LOAD"));
  display.displaySuspend(true);   // hand the frame buffer to renderFrame()
  display.displayShutdown(false);
  frameNo = 0;
  phaseAt = millis();
  phase = P_LOAD;
  renderFrame();
}

void startPulse() {
  display.displaySuspend(true);
  display.displayShutdown(false);
  frameNo = 0;
  phaseAt = millis();
  phase = P_PULSE;
  renderFrame();
}

// --- waiting on the user ---------------------------------------------------
void applyAskStep() {
  static const char *ASK[ASK_WORDS] = { "I", "NEED", "REPLY" };
  phaseAt = millis();
  setStatic(ASK[askStep]);
}

void startAsk() {
  Serial.println(F("ASK"));
  askStep = 0;
  applyAskStep();
  phase = P_ASK;
}

void selfTest() {
  goIdle();
  display.getGraphicObject()->control(MD_MAX72XX::TEST, MD_MAX72XX::ON);
  delay(1000);
  display.getGraphicObject()->control(MD_MAX72XX::TEST, MD_MAX72XX::OFF);
  goIdle();
}

void handleCommand(char *line) {
  switch (line[1]) {
    case 'L':
      startLoad();
      break;
    case 'N':
      startNotify(line + 2);
      break;
    case 'Q':
      startAsk();
      break;
    case 'X':
      startCancel();
      break;
    case 'B': {
      int v = constrain(atoi(line + 2), 0, 15);
      display.setIntensity(v);
      EEPROM.update(EE_BRIGHT_ADDR, (uint8_t)v);
      EEPROM.update(EE_MAGIC_ADDR, EE_MAGIC);
      Serial.print(F("BRIGHT "));
      Serial.println(v);
      break;
    }
    case 'S':
      scrollSpeed = constrain(atoi(line + 2), 10, 150);
      Serial.print(F("SPEED "));
      Serial.println(scrollSpeed);
      break;
    case 'T':
      Serial.println(F("TEST"));
      selfTest();
      break;
    case 'C':
      Serial.println(F("CLEAR"));
      goIdle();
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  uint8_t brightness = DEFAULT_BRIGHTNESS;
  if (EEPROM.read(EE_MAGIC_ADDR) == EE_MAGIC) {
    uint8_t b = EEPROM.read(EE_BRIGHT_ADDR);
    if (b <= 15) brightness = b;
  }
  display.begin();
  display.setIntensity(brightness);
  goIdle();
  Serial.println(F("READY agent-pulse 4x MAX7219"));
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (inlen > 0) {
        inbuf[inlen] = '\0';
        if (inbuf[0] == '#' && inlen >= 2) {
          handleCommand(inbuf);
        } else {
          startScroll(inbuf);
        }
        inlen = 0;
      }
    } else if (inlen < MSG_MAX) {
      inbuf[inlen++] = c;
    }
  }

  unsigned long now = millis();

  switch (phase) {
    case P_IDLE:
      if (now - idleAt >= AWAIT_MS) startPulse();
      break;

    case P_SCROLL:
      if (display.displayAnimate()) {
        Serial.println(F("DONE"));
        goIdle();
      }
      break;

    case P_BLINK: {
      unsigned long step = (blinkStep % 2 == 0) ? blinkOnMs : blinkOffMs;
      if (now - phaseAt >= step) {
        blinkStep++;
        phaseAt = now;
        if (blinkStep >= blinkSteps) {
          enterHold();
        } else {
          display.displayShutdown(blinkStep % 2 == 1);
        }
      }
      break;
    }

    case P_HOLD: {
      // step 0 is held for a fixed time; the rest end when the scroll does
      bool done = (holdStep == 0) ? (now - phaseAt >= staticMs)
                                  : display.displayAnimate();
      if (done) {
        holdStep++;
        if (holdStep >= holdCount) {
          Serial.println(F("EXPIRED"));
          goIdle();
        } else {
          showHoldStep();
        }
      }
      break;
    }

    case P_ASK:
      if (now - phaseAt >= ASK_DWELL_MS) {
        askStep = (askStep + 1) % ASK_WORDS;
        applyAskStep();
      }
      break;

    case P_LOAD:
      if (now - phaseAt >= LOAD_FRAME_MS) {
        phaseAt = now;
        frameNo++;
        renderFrame();
      }
      break;

    case P_PULSE:
      if (now - phaseAt >= HEART_FRAME_MS) {
        phaseAt = now;
        frameNo++;
        if (frameNo >= HEART_CYCLE) goIdle();   // one beat, then dark again
        else renderFrame();
      }
      break;
  }
}
