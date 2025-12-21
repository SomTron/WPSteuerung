import pandas as pd
import logging
import os
import sys
from datetime import datetime
import pytz

# Add current dir to path to import local utils
sys.path.append(os.getcwd())
from utils import check_and_fix_csv_header, EXPECTED_CSV_HEADER

# Configure logging to see what check_and_fix_csv_header does
logging.basicConfig(level=logging.INFO)

file_path = "test_heizungsdaten.csv"

def run_test():
    # 1. Create a "corrupt" file with old/wrong header
    old_header = "Zeitstempel,T_Oben,T_Hinten,T_Boiler,T_Verd,Kompressor,ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy\n"
    data_row = "2025-12-21 21:29:28,30.5,31.2,30.8,5.5,AUS,0,0,0,50,0,0,0\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(old_header)
        f.write(data_row)
    
    print("--- Before Fix ---")
    with open(file_path, "r") as f:
        print(f.read())

    # 2. Run fix
    check_and_fix_csv_header(file_path)

    print("--- After Fix ---")
    with open(file_path, "r") as f:
        content = f.read()
        print(content)

    # 3. Verify pandas can read it (even if data row has fewer columns, pandas handles it with padding if needed, 
    # but our fix should ideally keep the data row as is)
    df = pd.read_csv(file_path)
    print("Columns in fixed DF:", df.columns.tolist())
    print("First row values:", df.iloc[0].values)
    
    # Clean up
    if os.path.exists(file_path):
        os.remove(file_path)
    if os.path.exists("backup"):
        # We don't want to spam backups during tests if we can help it, but check_and_fix does it.
        pass

if __name__ == "__main__":
    run_test()
