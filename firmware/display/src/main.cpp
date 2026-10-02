// Landscape UI for the 1.91" T-Display-S3 AMOLED.
// The glass is 240×536; rotation 1 makes the long edge the line of text.
// Idle, with no lines, is a waiting face.
// Host protocol, one line each:
//   SHOW idle|-|-|-|line1|line2|line3|line4
//   SHOW ask|<id>|<btnA>|<btnB>|line1|line2|line3|line4
//   PING
// Device:
//   HELLO T-Display-S3-AMOLED 240 536
//   BTN <id> approve|deny
//   PONG

#include <Arduino.h>
#include <string.h>
#include <Arduino_GFX_Library.h>
#include <Wire.h>
#include "TouchDrvCSTXXX.hpp"

static const int NATIVE_W = 240;
static const int VIEW_W = 536;
static const int VIEW_H = 240;

Arduino_DataBus *bus = new Arduino_ESP32QSPI(
    6 /* cs */, 47 /* sck */, 18 /* d0 */, 7 /* d1 */, 48 /* d2 */, 5 /* d3 */);
Arduino_GFX *panel = new Arduino_RM67162(bus, 17 /* RST */, 1 /* rotation */);
Arduino_Canvas *canvas = nullptr;
TouchDrvCSTXXX touch;
bool touchOnline = false;

char mode[8] = "idle";
char proposalId[32] = "";
char buttonA[12] = "Yes";
char buttonB[12] = "No";
char lines[4][24] = {"", "", "", ""};
char lastFrame[180] = "";
volatile bool homePressed = false;
uint32_t lastTapMs = 0;
bool eyesOpen = true;
int look = 4;
uint8_t facePhase = 0;
uint32_t nextFaceMs = 0;

void setBrightness(uint8_t value) {
    bus->beginWrite();
    bus->writeC8D8(0x51, value);
    bus->endWrite();
}

void copyField(char *dest, size_t destLen, const char *src) {
    if (!src) {
        dest[0] = 0;
        return;
    }
    size_t i = 0;
    for (; src[i] && i + 1 < destLen; i++) {
        dest[i] = src[i];
    }
    dest[i] = 0;
}

bool hasText() {
    for (int i = 0; i < 4; i++) {
        if (lines[i][0]) {
            return true;
        }
    }
    return false;
}

void drawEye(int cx, int cy, int r, bool open, int pupil) {
    if (!open) {
        canvas->fillRoundRect(cx - r, cy - 2, r * 2, 4, 2, 0x6200);
        return;
    }
    canvas->fillCircle(cx, cy, r, WHITE);
    canvas->fillCircle(cx + pupil, cy + 1, r / 2, 0x3186);
    canvas->fillCircle(cx + pupil - r / 4, cy - r / 3, r / 5, WHITE);
}

void drawSmile(int cx, int cy, int w) {
    int prev = cy;
    for (int dx = -w; dx <= w; dx++) {
        int y = cy + ((w * w) - (dx * dx)) / (w * 4);
        if (dx > -w) {
            canvas->drawLine(cx + dx - 1, prev, cx + dx, y, 0x8000);
        }
        prev = y;
    }
}

void drawFace(int cx, int cy, int scale, bool open, int pupil) {
    int radius = scale == 2 ? 64 : 40;
    int eyeR = scale == 2 ? 14 : 9;
    int gap = scale == 2 ? 26 : 16;
    canvas->fillCircle(cx, cy, radius, 0xFD20);
    drawEye(cx - gap, cy - (scale == 2 ? 10 : 6), eyeR, open, pupil);
    drawEye(cx + gap, cy - (scale == 2 ? 10 : 6), eyeR, open, pupil);
    drawSmile(cx, cy + (scale == 2 ? 16 : 10), scale == 2 ? 20 : 12);
}

void draw() {
    if (!canvas) {
        return;
    }
    bool message = hasText() || strcmp(mode, "ask") == 0;
    bool open = message || eyesOpen;
    int pupil = message ? 2 : look;
    canvas->fillScreen(BLACK);
    if (message) {
        drawFace(78, 78, 1, true, 2);
        canvas->setTextSize(3);
        canvas->setTextColor(0xFFFF);
        int y = 28;
        for (int i = 0; i < 4; i++) {
            if (!lines[i][0]) {
                continue;
            }
            canvas->setCursor(148, y);
            canvas->print(lines[i]);
            y += 32;
        }
    } else {
        drawFace(VIEW_W / 2, VIEW_H / 2 - 4, 2, open, pupil);
    }
    if (strcmp(mode, "ask") == 0) {
        canvas->fillRoundRect(16, 176, 244, 50, 10, 0x0320);
        canvas->fillRoundRect(276, 176, 244, 50, 10, 0x4800);
        canvas->setTextSize(2);
        canvas->setTextColor(WHITE);
        canvas->setCursor(100, 194);
        canvas->print(buttonA);
        canvas->setCursor(360, 194);
        canvas->print(buttonB);
    }
    canvas->flush();
}

void serviceFace() {
    if (!canvas || hasText() || strcmp(mode, "ask") == 0) {
        return;
    }
    if (millis() < nextFaceMs) {
        return;
    }
    facePhase = (facePhase + 1) % 4;
    eyesOpen = (facePhase % 2) == 0;
    look = (facePhase == 2) ? -5 : 4;
    nextFaceMs = millis() + (eyesOpen ? 1700 : 130);
    draw();
}

void applyShow(char *body) {
    char *fields[8];
    int count = 0;
    fields[count++] = body;
    for (char *p = body; *p && count < 8; p++) {
        if (*p == '|') {
            *p = 0;
            fields[count++] = p + 1;
        }
    }
    if (count < 8) {
        return;
    }
    copyField(mode, sizeof(mode), fields[0]);
    copyField(proposalId, sizeof(proposalId), fields[1]);
    copyField(buttonA, sizeof(buttonA), fields[2][0] && strcmp(fields[2], "-") ? fields[2] : "Yes");
    copyField(buttonB, sizeof(buttonB), fields[3][0] && strcmp(fields[3], "-") ? fields[3] : "No");
    for (int i = 0; i < 4; i++) {
        copyField(lines[i], sizeof(lines[i]), fields[4 + i]);
    }
    if (strcmp(mode, "ask") != 0) {
        copyField(mode, sizeof(mode), "idle");
    }
    draw();
}

void handleLine(char *line) {
    if (strncmp(line, "SHOW ", 5) == 0) {
        if (strcmp(line, lastFrame) == 0) {
            return;
        }
        strncpy(lastFrame, line, sizeof(lastFrame) - 1);
        lastFrame[sizeof(lastFrame) - 1] = 0;
        applyShow(line + 5);
        return;
    }
    if (strcmp(line, "PING") == 0) {
        Serial.println("PONG");
    }
}

void readSerial() {
    static char buf[200];
    static size_t used = 0;
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\r') {
            continue;
        }
        if (c == '\n') {
            buf[used] = 0;
            if (used) {
                handleLine(buf);
            }
            used = 0;
            continue;
        }
        if (used + 1 < sizeof(buf)) {
            buf[used++] = c;
        }
    }
}

void sendButton(const char *decision) {
    if (strcmp(mode, "ask") != 0 || !proposalId[0] || strcmp(proposalId, "-") == 0) {
        return;
    }
    uint32_t now = millis();
    if (now - lastTapMs < 400) {
        return;
    }
    lastTapMs = now;
    Serial.print("BTN ");
    Serial.print(proposalId);
    Serial.print(' ');
    Serial.println(decision);
    copyField(mode, sizeof(mode), "idle");
    draw();
}

void readTouch() {
    if (homePressed) {
        homePressed = false;
        sendButton("approve");
    }
    // isPressed() drops the first pulse on the IRQ pin, so a tap never lands.
    // Read the controller directly instead.
    if (!touchOnline) {
        return;
    }
    int16_t x[1];
    int16_t y[1];
    if (!touch.getPoint(x, y)) {
        return;
    }
    int16_t vx = x[0];
    int16_t vy = y[0];
    // Portrait reports come back as x across 240 and y along 536.
    if (vy > vx && vy > NATIVE_W) {
        int16_t swap = vx;
        vx = vy;
        vy = swap;
    }
    sendButton(vx < VIEW_W / 2 ? "approve" : "deny");
}

void setup() {
    Serial.begin(115200);
    pinMode(38, OUTPUT);
    digitalWrite(38, HIGH);
    delay(50);

    const char *touchName = "none";
    touch.setPins(-1, 21);
    if (touch.begin(Wire, CST816_SLAVE_ADDRESS, 3, 2)) {
        touchOnline = true;
        touchName = touch.getModelName();
        // Same mapping LilyGO uses for this panel with the long edge horizontal.
        touch.setMaxCoordinates(VIEW_W, VIEW_H);
        touch.setSwapXY(false);
        touch.setMirrorXY(false, false);
        touch.setCenterButtonCoordinate(600, 120);
        touch.setHomeButtonCallback([](void *ptr) { *static_cast<volatile bool *>(ptr) = true; }, (void *)&homePressed);
    }

    if (!panel->begin()) {
        Serial.println("HELLO display-failed");
        return;
    }
    canvas = new Arduino_Canvas(panel->width(), panel->height(), panel, 0, 0);
    canvas->begin(GFX_SKIP_OUTPUT_BEGIN);
    setBrightness(180);
    nextFaceMs = millis() + 1700;
    draw();
    Serial.printf("HELLO T-Display-S3-AMOLED 536 240 %s\n", touchName);
}

void loop() {
    readSerial();
    readTouch();
    serviceFace();
    delay(10);
}
