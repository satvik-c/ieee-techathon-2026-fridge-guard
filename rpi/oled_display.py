import time
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306
from PIL import ImageFont

class FridgeDisplay:
    def __init__(self):
        self.UUID_MAP = {
            "ff01": "Satvik",
            "ff02": "Pranav",
            "ff03": "Ayushi"
        }
        self.last_seen_time = 0
        self.TIMEOUT_SECONDS = 2.0  

        # --- NEW: Override Flags ---
        self.is_calibrating = False
        self.flash_invert = False

        try:
            serial = i2c(port=1, address=0x3C)
            self.device = ssd1306(serial)
            self.font = ImageFont.load_default()
            
            self.last_temp = 0.0
            self.last_hum = 0.0
            self.last_user = "Guest"
            
            print("[OLED] Display ready.")
        except Exception as e:
            print(f"[OLED] Initialization failed: {e}")
            self.device = None

    def toggle_calibration_mode(self, active):
        """Turns the flashing override on or off."""
        self.is_calibrating = active
        if not active:
            # Force a normal redraw the moment calibration ends
            self.update_status()

    def draw_flash_frame(self, status, line1, line2):
        if not self.device or not self.is_calibrating:
            return

        # Screen dimensions for centering
        W = 128 

        try:
            with canvas(self.device) as draw:
                # 1. SET THE STYLE BASE
                if status == "WAITING":
                    # FLASHING (Inverting)
                    self.flash_invert = not self.flash_invert
                    bg = "white" if self.flash_invert else "black"
                    fg = "black" if self.flash_invert else "white"
                    draw.rectangle(self.device.bounding_box, outline="white", fill=bg)

                elif status == "CALIBRATING":
                    # SOLID WHITE (Pseudo-Yellow)
                    draw.rectangle(self.device.bounding_box, outline="white", fill="white")
                    fg = "black"

                elif status == "SUCCESS":
                    # THICK BORDER (Pseudo-Green)
                    draw.rectangle(self.device.bounding_box, outline="white", fill="black")
                    # Draw a second, inner rectangle to create a 3-pixel thick border
                    draw.rectangle((3, 3, 124, 60), outline="white", fill="black")
                    fg = "white"

                # 2. CALCULATE CENTER X COORDINATES
                # textlength returns the width of the string in pixels
                w1 = draw.textlength(line1, font=self.font)
                w2 = draw.textlength(line2, font=self.font)
                
                x1 = (W - w1) // 2
                x2 = (W - w2) // 2

                # 3. DRAW CENTERED TEXT
                # (x, y) coordinates
                draw.text((x1, 18), line1, font=self.font, fill=fg)
                draw.text((x2, 38), line2, font=self.font, fill=fg)

        except Exception as e:
            print(f"[OLED] Draw Error: {e}")

    def update_status(self, temp_f=None, humidity=None, roommate=None):
        if not self.device:
            return

        # 1. ALWAYS capture incoming data (even during calibration)
        if temp_f is not None: self.last_temp = float(temp_f)
        if humidity is not None: self.last_hum = float(humidity)
        
        if roommate:
            name = self.UUID_MAP.get(str(roommate).lower())
            if name:
                self.last_user = name
                self.last_seen_time = time.time()

        if time.time() - self.last_seen_time > self.TIMEOUT_SECONDS:
            self.last_user = "Guest"

        # 2. OVERRIDE: If we are calibrating, stop here. Do not draw the normal screen.
        if self.is_calibrating:
            return

        # 3. Normal Render (Only runs if calibration is false)
        try:
            with canvas(self.device) as draw:
                draw.rectangle(self.device.bounding_box, outline="white", fill="black")
                draw.text((5, 5), f"{self.last_temp:.1f} °F", font=self.font, fill="white")
                draw.text((49, 5), f"Humidity: {self.last_hum:.0f}%", font=self.font, fill="white")
                draw.line((0, 20, 128, 20), fill="white")
                draw.text((8, 30), "CURRENT USER:", font=self.font, fill="white")
                draw.text((23, 45), f"> {self.last_user}", font=self.font, fill="white")
        except Exception as e:
            print(f"[OLED] Draw Error: {e}")