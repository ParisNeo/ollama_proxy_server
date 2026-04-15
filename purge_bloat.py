import os
import shutil
from pathlib import Path

def clean():
    print("🚀 LoLLMs Hub Emergency Disk Recovery...")
    
    # 1. Clear Logs
    for p in Path(".").glob("lollms_hub.log*"):
        print(f"Emptying {p}...")
        with open(p, "w") as f: f.write("")

    # 2. Clear Temporary Uploads
    temp_dir = Path("app/static/uploads/temp")
    if temp_dir.exists():
        print("Clearing temporary upload artifacts...")
        shutil.rmtree(temp_dir)
        temp_dir.mkdir()

    # 3. Clear Vendor Assets Cache (They will re-download on next visit)
    vendor_dir = Path("app/static/vendor")
    if vendor_dir.exists():
        print("Clearing static library cache...")
        for f in vendor_dir.glob("*"):
            if f.is_file() and f.name != ".vendor_manifest.json":
                f.unlink()

    print("✅ Cleanup complete. Please check your C: drive free space.")

if __name__ == "__main__":
    clean()