import os
import shutil
from Steuerung.utils import check_and_fix_csv_header, EXPECTED_CSV_HEADER

TEST_FILE = "test_header.csv"

def create_test_file(header_cols):
    with open(TEST_FILE, "w", encoding="utf-8") as f:
        f.write(",".join(header_cols) + "\n")
        f.write("2023-01-01 00:00:00,1,2,3\n")

def test_header_fix():
    print("Testing check_and_fix_csv_header...")
    
    # 1. Create file with WRONG header
    wrong_header = EXPECTED_CSV_HEADER[:-1] # Missing last col
    create_test_file(wrong_header)
    print("Created file with wrong header.")
    
    # 2. First check - Should return True (Fixed)
    fixed = check_and_fix_csv_header(TEST_FILE)
    print(f"Run 1 (Should be True): {fixed}")
    
    if not fixed:
        print("FAIL: Did not fix wrong header!")
        return

    # 3. Second check - Should return False (Already Fixed)
    fixed_again = check_and_fix_csv_header(TEST_FILE)
    print(f"Run 2 (Should be False): {fixed_again}")
    
    if fixed_again:
        print("FAIL: Fixed already correct header! (Not idempotent)")
        
        # Check what's in the file now
        with open(TEST_FILE, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            print(f"Current Header in file: {line}")
            print(f"Expected Header:        {','.join(EXPECTED_CSV_HEADER)}")
    else:
        print("PASS: Header check is idempotent.")

    # Cleanup
    if os.path.exists(TEST_FILE):
        os.remove(TEST_FILE)
    if os.path.exists(TEST_FILE + ".tmp"):
        os.remove(TEST_FILE + ".tmp")

if __name__ == "__main__":
    test_header_fix()
