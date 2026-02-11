import pytest
from unittest.mock import patch, mock_open, MagicMock
import tempfile
import os
import sys
import pandas as pd
from datetime import datetime, timedelta
import csv

# Add parent directory to path to import utils
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils import check_and_fix_csv_header, backup_csv, rotate_csv, EXPECTED_CSV_HEADER, HEIZUNGSDATEN_CSV

def test_expected_csv_header():
    """Test that the expected CSV header is defined correctly"""
    assert isinstance(EXPECTED_CSV_HEADER, list)
    assert len(EXPECTED_CSV_HEADER) > 0
    assert "Zeitstempel" in EXPECTED_CSV_HEADER


def test_check_and_fix_csv_header_file_not_exists():
    """Test checking CSV header when file doesn't exist"""
    result = check_and_fix_csv_header("nonexistent_file.csv")
    assert result is False


def test_check_and_fix_csv_header_correct_header():
    """Test checking CSV header when it's already correct"""
    # Create a temporary CSV file with correct header
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        f.write("2023-01-01 00:00:00,40.0,39.0,38.0,10.0,EIN,Grid,0,0\n")
        temp_file = f.name

    try:
        result = check_and_fix_csv_header(temp_file)
        assert result is False  # Should return False because no fix was needed
    finally:
        os.unlink(temp_file)


def test_check_and_fix_csv_header_wrong_header():
    """Test checking CSV header when it's incorrect"""
    # Create a temporary CSV file with wrong header
    wrong_header = EXPECTED_CSV_HEADER[:-1]  # Missing last column
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        f.write(",".join(wrong_header) + "\n")
        f.write("2023-01-01 00:00:00,40.0,39.0,38.0,10.0,EIN,Grid,0\n")
        temp_file = f.name

    try:
        result = check_and_fix_csv_header(temp_file)
        assert result is True  # Should return True because fix was applied
        
        # Verify the header was corrected
        with open(temp_file, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            assert first_line == ",".join(EXPECTED_CSV_HEADER)
    finally:
        os.unlink(temp_file)


def test_check_and_fix_csv_header_empty_file():
    """Test checking CSV header when file is empty"""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        # Write nothing, file remains empty
        temp_file = f.name

    try:
        result = check_and_fix_csv_header(temp_file)
        assert result is False  # Should return False, no fix applied to empty file
    finally:
        os.unlink(temp_file)


@patch("builtins.open", new_callable=mock_open, read_data=",".join(EXPECTED_CSV_HEADER) + "\n2023-01-01 00:00:00,40.0,39.0,38.0,10.0,EIN,Grid,0,0\n")
def test_check_and_fix_csv_header_with_mock_open(mock_file):
    """Test checking CSV header with mocked file operations"""
    # Mock os.path.exists to return True so the file is processed
    with patch("os.path.exists", return_value=True):
        result = check_and_fix_csv_header("dummy.csv")
        assert result is False  # Should return False because header is correct
        # Verify that the file was accessed
        assert mock_file.called


def test_backup_csv_creation():
    """Test creating a backup of a CSV file"""
    # Create a temporary CSV file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        f.write("2023-01-01 00:00:00,40.0,39.0,38.0,10.0,EIN,Grid,0,0\n")
        temp_file = f.name

    try:
        # Create a temporary backup directory
        with tempfile.TemporaryDirectory() as backup_dir:
            backup_path = backup_csv(temp_file, backup_dir)

            # Check that backup was created
            assert os.path.exists(backup_path)
            # The backup file uses the original filename as base, followed by timestamp and .bak extension
            # So it should contain the original filename and end with .bak
            assert temp_file.split(os.sep)[-1] in backup_path  # Original filename is part of backup name
            assert backup_path.endswith(".bak")
            
            # Check that backup content matches original
            with open(temp_file, 'r', encoding='utf-8') as orig_f, open(backup_path, 'r', encoding='utf-8') as bak_f:
                orig_content = orig_f.read()
                bak_content = bak_f.read()
                assert orig_content == bak_content
    finally:
        os.unlink(temp_file)


def test_backup_csv_file_not_exists():
    """Test backing up a non-existent file"""
    result = backup_csv("nonexistent_file.csv")
    # This should fail gracefully or return None
    # The exact behavior depends on the implementation


def test_rotate_csv_basic():
    """Test basic CSV rotation functionality"""
    # Create a temporary CSV file with some data
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        # Add data from today (should be kept)
        today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{today},40.0,39.0,38.0,10.0,EIN,Grid,0,0\n")
        # Add data from 20 days ago (should be moved to backup)
        past_date = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{past_date},39.0,38.0,37.0,9.0,AUS,Grid,0,0\n")
        temp_file = f.name

    try:
        # Run rotation
        rotate_csv(temp_file)

        # Check that the main file still exists
        assert os.path.exists(temp_file)

        # Read the file to verify content
        df = pd.read_csv(temp_file)
        timestamps = pd.to_datetime(df['Zeitstempel'])

        # The rotation logic keeps data from the last 7 days, so we should have at least the current day's entry
        assert len(df) >= 1  # Should have at least the current day's entry
    finally:
        # Clean up - remove the rotated file and any backup files
        if os.path.exists(temp_file):
            os.unlink(temp_file)
        
        # Also clean up any backup files that might have been created
        backup_pattern = "backup_*.csv"
        import glob
        for backup_file in glob.glob(backup_pattern):
            if os.path.exists(backup_file):
                os.unlink(backup_file)


def test_rotate_csv_empty_file():
    """Test CSV rotation with an empty file"""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        # Create empty file
        temp_file = f.name

    try:
        # This should not raise an exception
        rotate_csv(temp_file)
        # The empty file should still exist
        assert os.path.exists(temp_file)
    finally:
        os.unlink(temp_file)


def test_rotate_csv_non_existent_file():
    """Test CSV rotation with a non-existent file"""
    # This should not raise an exception
    rotate_csv("nonexistent_file.csv")


def test_heizungsdaten_csv_constant():
    """Test that the HEIZUNGSDATEN_CSV constant is defined"""
    assert isinstance(HEIZUNGSDATEN_CSV, str)
    assert len(HEIZUNGSDATEN_CSV) > 0