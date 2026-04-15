import os
import subprocess
import threading
import time

def capture_arducam(filename):
    """Captures from the Arducam IMX708 via CSI port."""
    print("📸 Arducam: Capturing top shelf...")
    try:
        subprocess.run([
            "rpicam-still", 
            "-o", filename, 
            "--immediate", 
            "-n", 
            "--width", "1280", 
            "--height", "720"
        ], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    output_folder = "snapshots"
    os.makedirs(output_folder, exist_ok=True)
    
    # Filenames for the two different views
    top_file = os.path.join(output_folder, "top_shelf.jpg")

    print("\n--- Dual-Camera FridgeGuard System ---")
    print(f"Top View (Arducam): {top_file}")
    print("\nPress [ENTER] to capture both cameras.")
    print("Type 'q' and press [ENTER] to quit.")
    print("---------------------------------------\n")

    while True:
        user_input = input("Waiting for trigger... ")

        if user_input.lower() == 'q':
            break

        # Start both captures at the same time using threads
        t1 = threading.Thread(target=capture_arducam, args=(top_file,))

        t1.start()

        t1.join()

        print("✅ SUCCESS: Both snapshots updated.\n")

if __name__ == "__main__":
    main()